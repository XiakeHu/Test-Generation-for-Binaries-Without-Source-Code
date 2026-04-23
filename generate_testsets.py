import os
import re
import json
import sys
import time
import traceback
import subprocess
import hashlib
from typing import Any, Dict, List, Optional, Tuple, Set

# 尝试加载容错解析库
try:
    import json_repair
    HAS_JSON_REPAIR = True
except ImportError:
    HAS_JSON_REPAIR = False

from call_llm import llm_request  
import config

# [修改点 1] 导入新的 AST 提取器
# 假设你将新提取器保存为 var_test2.py
from var_test2 import CUserInputExtractorAST


logger = config.get_logger()

# ================= 配置常量 =================
DEFAULT_TEST_SIZE = 100          
DEFAULT_MAX_INPUT_COUNT = 500   
MAX_ATTEMPTS_PER_SEGMENT = 3    
PYTHON_TIMEOUT = 30 

# 目录全局变量
SOURCE_DIR = ""
OUTPUT_DIR = ""
SCRIPT_SAVE_DIR = ""  
SEGMENT_SAVE_DIR = "" 

def init_dir():
    global SOURCE_DIR, OUTPUT_DIR, SCRIPT_SAVE_DIR, SEGMENT_SAVE_DIR
    SOURCE_DIR = f"{config.decompiled_code_dir}/{config.code_name}"
    OUTPUT_DIR = f"{config.decompiled_code_dir}/{config.code_name}"
    
    SCRIPT_SAVE_DIR = os.path.join(OUTPUT_DIR, "debug_scripts")
    SEGMENT_SAVE_DIR = os.path.join(OUTPUT_DIR, "segment_outputs")
    
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(SCRIPT_SAVE_DIR, exist_ok=True)
    os.makedirs(SEGMENT_SAVE_DIR, exist_ok=True)

def write_text(path: str, content: str) -> None:
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except Exception as e:
        logger.error(f"Write failed: {path} - {e}")

def strip_code_fences(s: str) -> str:
    s = s.strip()
    s = re.sub(r"^```[a-zA-Z0-9]*\s*", "", s)
    s = re.sub(r"\s*```$", "", s)
    return s.strip()

def get_cases(text: str) -> List[str]:
    if not text.strip(): return []
    # 使用正则处理连续换行符，兼容 \n\n 分隔
    return [b.strip() for b in re.split(r"\n\s*\n+", text.strip()) if b.strip()]

# ==========================================
# 核心组件 1：Python 执行器
# ==========================================
def expand_python_generation(llm_output: str, script_filename: str) -> Tuple[str, str]:
    """
    执行生成的 Python 代码。
    返回: (标准输出内容, 错误信息字符串)
    """
    code_body = strip_code_fences(llm_output)
    
    lines = code_body.split('\n')
    clean_lines = []
    for line in lines:
        if "if __name__" in line: break 
        clean_lines.append(line)
    code_body = "\n".join(clean_lines)

    wrapper_code = f"""
import sys, random, string, math, itertools, struct, re

class SafeStdout:
    def __init__(self, limit_bytes=10*1024*1024): 
        self.limit = limit_bytes
        self.written = 0
        self.original_stdout = sys.stdout

    def write(self, s):
        if self.written + len(s) > self.limit: pass 
        else:
            self.original_stdout.write(s)
            self.written += len(s)

    def flush(self): self.original_stdout.flush()

# --- [AI Generated Code Start] ---
{code_body}
# --- [AI Generated Code End] ---

if __name__ == '__main__':
    sys.stdout = SafeStdout()
    try:
        sys.setrecursionlimit(3000)
        if 'run_gen' in globals():
            run_gen()
        else:
            sys.stderr.write("GEN_ERR: function run_gen not found in AI output\\n")
            sys.exit(1)
    except Exception as e:
        import traceback
        traceback.print_exc(file=sys.stderr)
        sys.exit(1)
    """
    
    script_path = os.path.join(SCRIPT_SAVE_DIR, script_filename)
    try:
        write_text(script_path, wrapper_code)
        
        cmd = [sys.executable, script_path]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=PYTHON_TIMEOUT)
        
        if result.returncode == 0:
            return result.stdout.strip(), ""
        else:
            error_msg = result.stderr.strip()
            return "", error_msg 
            
    except subprocess.TimeoutExpired:
        msg = f"Script timeout ({script_filename}) after {PYTHON_TIMEOUT}s"
        return "", msg
    except Exception as e:
        msg = f"Wrapper execution system error: {str(e)}"
        return "", msg

# ==========================================
# 核心组件 2：VarTree 深度分析 (适配 AST 新版本)
# ==========================================

def _analyze_variable_tree(node: Dict, constraints_by_line: Dict[str, List[Dict[str, Any]]]) -> Tuple[List[str], List[str], List[int]]:
    """
    [新增逻辑] 深度遍历变量树，提取：
    1. 所有的路径约束 (从 constraints 字段和 kind='constraint' 节点)
    2. 所有的魔法值 (magic_values)
    """
    constraints = set()
    constraint_lines = set()
    magic_values = set()

    def traverse(n):
        if not n: return
        
        # 1. 提取魔法值
        mvs = n.get('magic_values', [])
        for mv in mvs:
            magic_values.add(str(mv))
            
        # 2. Resolve shared constraints from referenced source lines.
        path_cons = n.get('constraints', [])
        for line in path_cons:
            try:
                lineno = int(line)
            except (TypeError, ValueError):
                continue
            constraint_lines.add(lineno)
            for c in constraints_by_line.get(str(lineno), []):
                if isinstance(c, dict) and 'stmt' in c:
                    constraints.add(c['stmt'])
                
        # 3. 提取结构化约束 (兼容旧逻辑或处理直接的 constraint 节点)
        kind = n.get('kind')
        code = n.get('code', '').strip()
        if kind == 'constraint' and code:
            constraints.add(code)
        
        # 4. 提取赋值逻辑 (Derived Var)
        if kind == 'assignment' and code:
            # 赋值本身也是一种逻辑关系
            pass 

        # 递归子节点
        for child in n.get('children', []):
            traverse(child)

    traverse(node)
    
    # 排序以保持确定性
    return list(sorted(constraints)), list(sorted(magic_values)), list(sorted(constraint_lines))

def extract_dependencies(results: List[Dict]) -> Dict[str, List[str]]:
    """提取变量依赖"""
    dependencies = {}
    var_names = {item['name'] for item in results}
    
    for item in results:
        name = item['name']
        deps = set()

        meta = item.get('input_metadata', {})
        loop_struct = meta.get('loop_structure', [])
        if loop_struct:
            # 简单的正则提取循环条件中的变量
            loop_str = " ".join(loop_struct)
            refs = re.findall(r'(?:<=|<|>=|>|==|!=)\s*([a-zA-Z_]\w*)', loop_str)
            for r in refs:
                if r in var_names and r != name:
                    deps.add(r)
        
        # AST 提取器可能直接分析出的依赖
        raw_deps = item.get('dependencies', []) # var_test2 可能不直接填充这个，但保留兼容
        for d in raw_deps:
            if d in var_names and d != name:
                deps.add(d)

        # 如果 tree_constraints 里包含其他变量，也可以视为依赖
        tree_cons = item.get('tree_constraints', [])
        for cons in tree_cons:
            for potential_var in var_names:
                if potential_var != name and potential_var in cons:
                    deps.add(potential_var)

        if deps:
            dependencies[name] = list(deps)
            
    return dependencies

def _get_coverage_hint(var: Dict, seg_type: str) -> str:
    """生成中文覆盖率提示"""
    hints = []
    constraints = var.get('tree_constraints', [])
    meta = var.get('input_metadata', {})
    loop_struct = meta.get('loop_structure', [])
    
    # 简单的 heuristic 判断
    is_loop_controller = False
    for c in constraints:
        # 如果约束里包含 i, j, k 且 loop_structure 存在
        if loop_struct and any(x in c for x in ['i', 'j', 'k', '++']):
            pass # 这种通常是位于循环内
    
    # 判断是否是控制循环次数的变量 (例如 n)
    # 通常如果别的变量依赖它，或者它出现在 loop_context 里
    # 这里主要依赖 generate_segment_cases 里的 deps 判断
            
    if loop_struct:
        depth = len(loop_struct)
        if depth >= 2:
            hints.append(f"位于 {depth} 层嵌套循环内，生成数量需匹配 N*N")
        else:
            hints.append("位于循环内，生成数量必须严格匹配控制变量")

    desc = var.get('description', '').lower()
    if 'switch' in desc or 'case' in desc:
        hints.append("覆盖不同的 case 分支")
    
    return " | ".join(hints)

def get_analysis_vars(file_path: str) -> Tuple[str, List[Dict], Dict[str, List[str]], Dict[str, Any]]:
    """
    [修改] 适配 AST 提取器
    使用 parse_file 而不是 parse_code，并深度聚合树信息
    """
    try:
        # 实例化新的 AST 提取器
        extractor = CUserInputExtractorAST()
        # AST 模式需要文件路径来运行 cpp
        analysis_result = extractor.parse_file(file_path)
    except Exception as e:
        logger.error(f"AST Analysis failed: {e}")
        return "分析失败", [], {}, {}

    raw_vars = analysis_result.get("inputs", [])
    var_trees = analysis_result.get("variable_trees", {})
    constraints_by_line = analysis_result.get("constraints_by_line", {})
    
    if not raw_vars: return "无明显输入变量", [], {}, analysis_result

    # 增强 raw_vars 信息
    for item in raw_vars:
        name = item['name']
        
        # 兼容旧逻辑的 loop_context
        if 'loop_context' in item and item['loop_context']:
            item['loop_condition'] = item['loop_context']
        
        meta = item.get('input_metadata', {})
        if meta and meta.get('array_limit'):
            item['array_limit'] = meta['array_limit']
            
        # [关键] 从变量树中聚合 Constraints 和 Magic Values
        # var_test2 的 input 列表里没有 magic_values，必须从树里拿
        if name in var_trees:
            constraints, tree_magic, constraint_lines = _analyze_variable_tree(var_trees[name], constraints_by_line)
            
            # 合并逻辑约束
            item['tree_constraints'] = constraints
            item['constraint_lines'] = constraint_lines
            
            # 合并魔法值
            existing_magic = set(item.get('magic_values', []))
            existing_magic.update(tree_magic)
            # 简单的过滤：排除太短的或者无关的
            filtered_magic = [m for m in existing_magic if len(m) > 0 and m not in ['0', '1']]
            item['magic_values'] = sorted(filtered_magic, key=lambda x: (len(x), x))

    deps = extract_dependencies(raw_vars)
    
    desc_lines = []
    for item in raw_vars:
        v_name = item['name']
        v_type = item['type']
        
        line = f"- 变量 `{v_name}` ({v_type})"
        meta_info = []
        
        if v_name in deps:
            params = ", ".join(deps[v_name])
            meta_info.append(f"大小受控于: [{params}]")
        
        magic_vals = item.get('magic_values', [])
        if magic_vals:
             shown_magic = magic_vals[:10]
             meta_info.append(f"关键分支值(Magic): {shown_magic}")
        
        meta = item.get('input_metadata', {})
        fields = meta.get('fields_accessed', [])
        if fields:
            meta_info.append(f"访问结构体字段: {fields}")

        loop_struct = meta.get('loop_structure', [])
        if loop_struct:
            meta_info.append(f"循环结构: {'->'.join(loop_struct)}")
            
        if item.get('tree_constraints'):
            shown = "; ".join(item['tree_constraints'][:5])
            meta_info.append(f"逻辑约束: {shown}")
        if item.get('constraint_lines'):
            meta_info.append(f"约束行: {item['constraint_lines'][:8]}")
            
        if meta_info: line += " | " + " | ".join(meta_info)
        desc_lines.append(line)
    # #消融测试    
    # for item in raw_vars:
    #     item['magic_values'] = []
        
    return "\n".join(desc_lines), raw_vars, deps, analysis_result

# ==========================================
# Prompt 生成 (中文版)
# ==========================================

def generate_segment_cases(
    all_vars: List[Dict],  
    seg: Dict[str, Any],   
    num_cases: int,
    model_index: int,
    global_deps: Dict[str, List[str]]
) -> str:
    
    seg_id = seg.get("id", 0) 
    constraints = seg.get("constraints", {}) 
    seg_type = seg.get("type", "random").lower()
    
    var_prompt_list = []
    
    for var in all_vars:
        v_name = var['name']
        v_type = var['type']
        v_desc = var.get('description', '') 
        
        tree_constraints = var.get('tree_constraints', [])
        array_limit = var.get('array_limit')
        magic_vals = var.get('magic_values', [])
        
        metadata = var.get('input_metadata', {})
        loop_struct = metadata.get('loop_structure', [])
        loop_depth = metadata.get('loop_depth', 0)
        fields_accessed = metadata.get('fields_accessed', [])
        
        deps = global_deps.get(v_name, [])
        
        line = f"   {len(var_prompt_list)+1}. `{v_name}` (类型: {v_type})"
        
        meta_infos = []
        if v_desc: meta_infos.append(f"含义: {v_desc}")
        if deps: meta_infos.append(f"⚠️ 数量/大小受控于: {', '.join(deps)} (生成数量必须与控制变量一致)")
        
        if fields_accessed:
            meta_infos.append(f"🏗️ 结构体: 必须按顺序生成字段 {fields_accessed}")

        # [修改点 1]：仅在 random 组别中向大模型暴露 Magic Values
        if magic_vals and seg_type == "random":
            meta_infos.append(f"约束相关常数参考: {magic_vals}")
        
        coverage_hint = _get_coverage_hint(var, seg_type)
        if coverage_hint:
            meta_infos.append(f"🎯 覆盖目标: {coverage_hint}")

        if tree_constraints:
            # 简单清洗一下约束字符串，让 Prompt 更易读
            readable_cons = [c.replace('self', v_name) for c in tree_constraints]
            # 限制长度防止 Prompt 过长
            shown_cons = readable_cons[:10]
            meta_infos.append(f"逻辑约束: [{'; '.join(shown_cons)}] (需覆盖满足与不满足的情况)")
        
        if array_limit:
            meta_infos.append(f"数组上限: {array_limit}")
            
        if loop_struct:
            struct_desc = " -> ".join(loop_struct)
            meta_infos.append(f"上下文: {struct_desc}")
            if loop_depth >= 2:
                meta_infos.append(f"【矩阵/多维模式】: 请使用 {loop_depth} 层嵌套循环生成数据")
        elif var.get('loop_condition'):
             meta_infos.append(f"循环条件: {var.get('loop_condition')}")

        special_constraint = constraints.get(v_name, {})
        if isinstance(special_constraint, dict):
            constraint_desc = special_constraint.get("desc", "")
        else:
            constraint_desc = str(special_constraint)

        if constraint_desc:
            meta_infos.append(f"**本组特殊要求**: {constraint_desc}")
        
        if meta_infos:
            line += " | " + " | ".join(meta_infos)
        
        var_prompt_list.append(line)

    var_str = "\n".join(var_prompt_list)
    
    strategy_map = {
        "min": "当前策略: 【最小边界测试】。重点测试 0 次循环、1 次循环、空字符串、极小整数。",
        "max": "当前策略: 【最大边界测试】。重点测试 数组上限、大整数、最大循环次数（缓冲区溢出检测）。",
        "random": "当前策略: 【随机模糊测试】。优先覆盖约束与分支，并参考约束中的常数值。"
    }
    strategy_prompt = strategy_map.get(seg_type, strategy_map["random"])

    # [修改点 2]：动态生成核心规则，彻底隔离策略意图
    if seg_type == "random":
        rule_1 = (
            "1. **关注约束中的常数值** :\n"
            "   - 变量描述中若包含 `约束相关常数参考`，请把这些值视为帮助理解分支条件的线索，而不是必须高频生成的目标。\n"
            "   - 只有当这些常数确实有助于覆盖 `if/switch` 等约束时，再自然地纳入部分用例；不要让所有样例都围绕这些值构造。"
        )
    elif seg_type == "min":
        rule_1 = (
            "1. **专注最小边界** :\n"
            "   - 请确保生成的数值严格集中在极小值（如 0, 1, -1, 空字符串, 最小循环次数）附近。\n"
            "   - 不要为了凑覆盖率去生成特定常数或魔法值，保持数据的纯粹性。"
        )
    elif seg_type == "max":
        rule_1 = (
            "1. **专注最大边界** :\n"
            "   - 请确保生成的数值严格集中在极大值（如大整数、数组上限、最大循环次数）附近，用于测试溢出。\n"
            "   - 不要为了凑覆盖率去生成特定常数或魔法值，保持数据的纯粹性。"
        )
    else:
        rule_1 = "1. **遵循常规生成**：根据变量类型生成合理的随机数据。"

    base_prompt = (
        f"你是一个专门用于做代码覆盖率测试的数据生成器。请编写 Python 代码生成符合 C 程序的标准输入 (stdin) 数据。\n"
        f"目标：生成 {num_cases} 组测试用例。\n"
        f"{strategy_prompt}\n"
        "\n"
        f"### 变量输出列表 (严格遵守顺序):\n"
        f"{var_str}\n"
        "\n"
        "### 核心规则 (Critical Rules):\n"
        f"{rule_1}\n"
        "2. **逻辑约束覆盖**：\n"
        "   - 如果存在 `逻辑约束` (如 `x < 10`)，请分别生成 **满足** 和 **违反** 该约束的值，以覆盖 if 和 else 分支。\n"
        "3. **循环与依赖**：\n"
        "   - 如果变量在循环内且受 `N` 控制，你必须生成 **严格等于 N** 个数据。\n"
        "   - 嵌套循环请务必正确处理层级。\n"
        "4. **结构体顺序**：\n"
        "   - 如果提示访问了字段 `[x, y]`，对于每个结构体实例，必须先打印 x，再打印 y。\n"
        "5. **格式纯净与分隔**：\n"
        "   - 只输出数据，禁止输出调试信息。\n"
        "   - **【重要】每组测试用例生成完毕后，必须输出两个空行（即调用 `print()` 两次），以便分割用例。**\n"
        "\n"
        "#### 代码模板参考:\n"
        "```python\n"
        "import random\n"
        "\n"
        "def run_gen():\n"
        "    # 请根据当前策略定义你的数据池和边界值\n"
        "    \n"
        "    for _ in range(10):\n"
        "       # 针对循环控制变量 N\n"
        "       N = random.choice([0, 1, random.randint(2, 20)])\n"
        "       print(N)\n"
        "       \n"
        "       for _ in range(N):\n"
        "           # 针对变量 x，根据当前策略和约束生成数据\n"
        "           x = random.randint(0, 100)\n"
        "           print(x)\n"
        "       \n"
        "       # 输出两个空行作为分隔符\n"
        "       print()\n"
        "       print()\n"
        "```\n"
        "请直接提供包含函数run_gen() Python 代码块："
    )

    curr_model = config.model_name

    last_error_msg = ""
    last_generated_code = ""

    for attempt in range(MAX_ATTEMPTS_PER_SEGMENT):
        try:
            current_prompt = base_prompt
            if last_error_msg:
                current_prompt += "\n\n" + "="*40 + "\n"
                current_prompt += "### 🚫 上次代码错误，请修正：\n"
                current_prompt += f"代码:\n{last_generated_code}\n"
                current_prompt += f"错误:\n{last_error_msg}\n"
            
            content = llm_request(current_prompt, curr_model, temperature=0.1)
            last_generated_code = strip_code_fences(content)
            
            script_name = f"seg_{seg_id}_{seg_type}_attempt_{attempt+1}.py"
            output, error_output = expand_python_generation(content, script_name)
            
            if not error_output:
                cases = get_cases(output)
                if len(cases) > 0:
                    return output
                else:
                    last_error_msg = "脚本运行成功但 Output 为空。请检查循环逻辑。"
            else:
                last_error_msg = error_output
            
        except Exception as e:
            logger.warning(f"Generation exception: {e}")
            last_error_msg = str(e)
            time.sleep(1)
            continue
    return ""
def generate_feedback_cases(file_path: str, uncovered_lines: List[int], iteration: int, test_size: int = 20) -> bool:
    """
    [修改] 迭代反馈生成：专门基于 random 组的脚本进行定向修改，利用 Magic Values 爆破未覆盖的行。
    """
    base = os.path.splitext(os.path.basename(file_path))[0]
    json_path = os.path.join(OUTPUT_DIR, f"{base}_var_info.json")
    
    target_hints = []
    uncovered_set = set(uncovered_lines)
    
    # 1. 尝试从 AST 树中寻找输入变量与未覆盖行的绑定关系
    var_desc_lines = []
    if os.path.exists(json_path):
        try:
            with open(json_path, 'r', encoding='utf-8') as f:
                full_analysis = json.load(f)
            
            for var in full_analysis.get("inputs", []):
                v_name = var.get('name', 'unknown')
                v_type = var.get('type', 'unknown')
                magic = var.get('magic_values', [])
                line = f"- 变量 `{v_name}` (类型: {v_type})"
                if magic: 
                    line += f" | 已知 Magic Values: {magic[:10]}"
                var_desc_lines.append(line)
            
            var_trees = full_analysis.get("variable_trees", {})
            for var_name, tree in var_trees.items():
                for child in tree.get("children", []):
                    lineno = child.get("lineno")
                    if lineno in uncovered_set:
                        stmt = child.get("code", "")
                        if stmt:
                            target_hints.append(f" - [变量约束] 变量 `{var_name}` 位于未覆盖行 {lineno}，触发逻辑: `{stmt}`")
        except Exception as e:
            logger.error(f"读取 var_info.json 失败: {e}")

    var_list_str = "\n".join(var_desc_lines) if var_desc_lines else "未能提取输入变量"

    # 2. 直接读取 C 源码补充上下文
    try:
        with open(file_path, 'r', encoding='utf-8') as sf:
            source_lines = sf.readlines()
            
        for lineno in sorted(list(uncovered_set))[:10]: 
            if 0 < lineno <= len(source_lines):
                raw_code = source_lines[lineno - 1].strip()
                if raw_code and raw_code not in ["{", "}", "};", "break;", "continue;"]: 
                    target_hints.append(f" - [源码片段] 行 {lineno} 未覆盖: `{raw_code}`")
    except Exception as e:
        logger.warning(f"读取源码补充上下文失败: {e}")

    target_hints = list(dict.fromkeys(target_hints))[:15]
    if not target_hints:
        target_hints.append(" - 未能提取到具体的未覆盖源码细节，请参考已知约束和相关常数值，补充更容易触发分支的输入。")

    # 3. 【核心修改】精准获取上一轮的 random 组脚本
    previous_code = "# 未能找到上一轮的 random 生成脚本，请直接参考代码模板重新编写。"
    import glob
    
    # 优先寻找包含 _random_ 的脚本
    possible_scripts = glob.glob(os.path.join(SCRIPT_SAVE_DIR, "*_random_attempt_*.py"))
    
    # 如果极端情况下没有 random 脚本，再退而求其次
    if not possible_scripts:
        possible_scripts = glob.glob(os.path.join(SCRIPT_SAVE_DIR, "*.py"))
        
    if possible_scripts:
        latest_script = max(possible_scripts, key=os.path.getmtime)
        logger.info(f"迭代反馈选取了基础脚本: {latest_script}")
        try:
            with open(latest_script, "r", encoding="utf-8") as f:
                script_content = f.read()
                import re
                match = re.search(r"# --- \[AI Generated Code Start\] ---\n(.*?)\n# --- \[AI Generated Code End\] ---", script_content, re.DOTALL)
                if match:
                    previous_code = match.group(1).strip()
        except Exception as e:
            logger.warning(f"读取上一轮脚本失败: {e}")

    # 4. 【核心修改】组装强调 Random 与 Magic Value 的终极 Prompt
    feedback_prompt = (
        "你是一个专门用于提升 C 程序测试覆盖率的动态数据生成器。\n"
        "**当前阶段：【定向反馈与 Random 策略进阶】**\n"
        "你之前在 Random 组生成的测试用例已经跑通，但根据 LCOV 的探针反馈，程序中仍有部分深层分支未被触发。\n\n"
        "### 1. 程序的全局输入变量定义（包含 Magic Values）如下：\n"
        f"{var_list_str}\n\n"
        "### 2. 你上一轮在 Random 组的基础代码如下（它未能打通所有分支）：\n"
        "```python\n"
        f"{previous_code}\n"
        "```\n\n"
        "### 3. 本轮攻坚的【未覆盖目标】反馈：\n"
        + "\n".join(target_hints) + "\n\n"
        "### 你的任务:\n"
        "1. **定向爆破**：仔细分析 [未覆盖目标] 中的逻辑，逆向推导需要什么样的数据才能让代码跳入这些分支。\n"
        "2. **利用 Magic Values 理解约束**：如果目标中包含 `x == 100` 这类条件，请把 `100` 视为帮助理解分支的线索。只有当它确实有助于命中未覆盖逻辑时，再自然加入部分用例；不要把生成策略固定在这些值上。\n"
        "3. **修改并完善代码**：请在上一轮 random 代码的基础上进行修改，增加特制的数据生成逻辑。目标生成 50 组用例。\n"
        "4. **格式纯净与分隔**：\n"
        "   - 只输出数据，禁止输出调试信息。\n"
        "   - **【重要】每组测试用例生成完毕后，必须输出两个空行（即调用 `print()` 两次），以便分割用例。**\n"
        "\n"
        "不要携带任何非代码格式文字，请直接提供 Python 代码块："
    )

    curr_model = config.model_name
    logger.info(f"向大模型发送 Random 定向爆破 Prompt，包含 {len(target_hints)} 条未覆盖线索...")
    
    last_error_msg = ""
    last_generated_code = ""
    MAX_FEEDBACK_ATTEMPTS = 3
    
    for attempt in range(MAX_FEEDBACK_ATTEMPTS):
        try:
            current_prompt = feedback_prompt
            if last_error_msg:
                current_prompt += "\n\n" + "="*40 + "\n"
                current_prompt += "### 🚫 上次代码错误，请修正：\n"
                current_prompt += f"代码:\n{last_generated_code}\n"
                current_prompt += f"错误:\n{last_error_msg}\n"
                
            content = llm_request(current_prompt, curr_model, temperature=0.6) 
            last_generated_code = strip_code_fences(content)
            
            # 反馈脚本命名也加上 _feedback_random_ 标识
            script_name = f"{base}_feedback_random_iter_{iteration}_att_{attempt+1}.py"
            
            output, error_output = expand_python_generation(content, script_name)
            
            if not error_output and output:
                cases = get_cases(output)
                valid_cases = [c for c in cases if c.strip()]
                
                if valid_cases:
                    out_txt = os.path.join(OUTPUT_DIR, f"{base}_iter_{iteration}.txt")
                    write_text(out_txt, "\n\n".join(valid_cases))
                    logger.info(f"定向生成成功，新增用例 {len(valid_cases)} 个。")
                    return True
                else:
                    last_error_msg = "脚本运行成功但 Output 为空。请检查输出打印逻辑。"
            else:
                last_error_msg = error_output
                logger.warning(f"反馈生成尝试 {attempt+1} 失败: {error_output}")
                
        except Exception as e:
            logger.warning(f"反馈生成异常: {e}")
            last_error_msg = str(e)
            import time
            time.sleep(1)

    return False
def ask_variables_and_segments(var_list_str: str, test_size: int) -> Dict[str, Any]:
    prompt = (
        "基于以下C代码的输入变量列表，设计测试用例分组策略。\n"
        f"变量列表:\n{var_list_str}\n\n"
        "请返回 JSON 格式，包含 3 个分段 (min, random, max)。\n"
        "JSON 格式示例:\n"
        "{\n"
        '  "segments": [\n'
        '    {\n'
        '      "id": 1, "type": "min", "num_cases": 10,\n'
        '      "constraints": { "n": {"desc": "min value 0"} }\n'
        '    },\n'
        '    {\n'
        '      "id": 2, "type": "max", "num_cases": 10,\n'
        '      "constraints": { "n": {"desc": "max value"} }\n'
        '    }\n'
        '  ]\n'
        '}'
    )
    
    try:
        resp = llm_request(prompt)
        raw = strip_code_fences(resp)
        data = json_repair.loads(raw) if HAS_JSON_REPAIR else json.loads(raw)
        return data
    except Exception as e:
        logger.error(f"Meta-data generation failed: {e}")
        return {"segments": []}

# ==========================================
# 主流程
# ==========================================

def process_file(file_path: str, test_size: int) -> bool:
    base = os.path.splitext(os.path.basename(file_path))[0]
    out_txt = os.path.join(OUTPUT_DIR, f"{base}.txt")
    
    # [修改点] AST 模式需要传入文件路径，而非代码字符串
    # 之前是 code = f.read() -> get_analysis_vars(code)
    # 现在是 -> get_analysis_vars(file_path)
    
    analyzed_vars_str, raw_vars, global_deps, full_analysis = get_analysis_vars(file_path)

    # 保存完整的 var_tree 结果
    json_output_path = os.path.join(OUTPUT_DIR, f"{base}_var_info.json")
    try:
        def set_default(obj):
            if isinstance(obj, set):
                return list(obj)
            raise TypeError

        full_analysis['global_dependencies'] = global_deps
        with open(json_output_path, "w", encoding="utf-8") as jf:
            json.dump(full_analysis, jf, indent=4, ensure_ascii=False, default=set_default)
        logger.info(f"Saved COMPLETE variable info to: {json_output_path}")
    except Exception as e:
        logger.error(f"Failed to save var_info.json: {e}")

    if not raw_vars:
        write_text(out_txt, "") 
        return True 

    # meta_data = ask_variables_and_segments(analyzed_vars_str, test_size)
    # segments = meta_data.get("segments", []) if isinstance(meta_data, dict) else []
    # if not segments: segments = [{"id": 0, "type": "random", "constraints": {}}]
    segments = [{
        "id": 0, 
        "type": "random", 
        "num_cases": test_size, # 将所有的生成需求都交给 random
        "constraints": {}
    }]


    unique_cases_list = []      
    seen_cases_hashes = set()
    
    for idx, seg in enumerate(segments):
        if len(unique_cases_list) >= test_size: 
            break
            
        needed = max(5, int(test_size / len(segments)) + 2)
        
        cases_str = generate_segment_cases(raw_vars, seg, needed, idx, global_deps)
        
        if cases_str:
            seg_type = seg.get("type", "unknown")
            seg_filename = f"{base}_seg_{idx}_{seg_type}.txt"
            seg_path = os.path.join(SEGMENT_SAVE_DIR, seg_filename)
            write_text(seg_path, cases_str)
            logger.info(f"Saved segment output: {seg_path}")           
            
            current_cases = get_cases(cases_str)

            for c in current_cases:
                stripped = c.strip()
                if not stripped:
                    continue
                case_hash = hashlib.md5(stripped.encode()).hexdigest()
                if case_hash not in seen_cases_hashes:
                    seen_cases_hashes.add(case_hash)
                    unique_cases_list.append(stripped)
                if len(unique_cases_list) >= test_size:
                    break

    if unique_cases_list:
        full_content = "\n\n".join(unique_cases_list)
        write_text(out_txt, full_content)
        logger.info(f"Completed {base}. Unique: {len(unique_cases_list)}")
        return False 
    else:
        write_text(out_txt, "")
        return True

def main(TEST_SIZE=DEFAULT_TEST_SIZE):
    init_dir()
    if not os.path.exists(SOURCE_DIR): return
    files = [f for f in os.listdir(SOURCE_DIR) if f.endswith((".c", ".cpp"))]
    for f in files:
        try: process_file(os.path.join(SOURCE_DIR, f), TEST_SIZE)
        except: traceback.print_exc()

if __name__ == "__main__":
    main()
