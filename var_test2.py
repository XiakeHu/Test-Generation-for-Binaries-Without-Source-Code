
import os
import sys
import json
import re
from typing import List, Dict, Any, Set, Optional
from dataclasses import dataclass, field
from collections import defaultdict

# ==========================================
# 0. 配置与依赖检查
# ==========================================
from pathlib import Path

# 动态获取当前脚本所在的项目根目录，并定位到 fake_libc_include
PROJECT_BASE = Path(__file__).resolve().parent
FAKE_LIBC_PATH = str(PROJECT_BASE / "fake_libc_include")

# 检查 pycparser
try:
    from pycparser import parse_file, c_ast, c_generator
except ImportError:
    print("[-] 错误: 未找到 pycparser。")
    sys.exit(1)

if not os.path.exists(FAKE_LIBC_PATH):
    print(f"[!] 警告: 未找到 fake_libc_include: {FAKE_LIBC_PATH}")
    print("[!] 解析包含系统头文件的代码可能会失败。")

# ==========================================
# 1. 数据结构定义 (严格保留原版定义)
# ==========================================

@dataclass
class VNode:
    kind: str          # 'var' | 'expr' | 'stmt' | 'call' | 'assignment' | 'constraint'
    name: str
    lineno: int
    code: str = ""
    # 保留原版的 List[Dict] 结构，不简化
    constraints: List[int] = field(default_factory=list)
    children: List['VNode'] = field(default_factory=list)
    derived_var: str = None 
    # 新增：方便调试的字段，记录该节点下的魔法值
    magic_values: List[str] = field(default_factory=list)

    def to_dict(self):
        return {
            "kind": self.kind,
            "name": self.name,
            "lineno": self.lineno,
            "code": self.code,
            "constraints": self.constraints,
            "derived_var": self.derived_var,
            "magic_values": self.magic_values,
            "children": [c.to_dict() for c in self.children]
        }

# ==========================================
# 2. AST 分析访问器 (逻辑完整复刻)
# ==========================================

class FullLogicVisitor(c_ast.NodeVisitor):
    def __init__(self):
        self.generator = c_generator.CGenerator()
        
        # 结果容器
        self.user_inputs = []
        self.var_declarations = {}  # {name: type_str}
        self.var_array_sizes = {}   # {name: size_str}
        self.use_sites = defaultdict(list)
        self.current_func = "global"
        self.constraints_by_line = defaultdict(list)
        self._constraint_keys = set()
        
        # 状态栈 (复刻原脚本的逻辑)
        self.loop_stack = []        # ["while(n>0)", "for(...)"]
        self.path_constraints = []  # 当前路径的约束节点信息
        self.switch_stack = []      # [新增] 存储当前 switch 的条件变量信息
        # 输入函数配置
        self.input_funcs = {
            'scanf': {'arg_idx': 1, 'fmt_idx': 0},
            'fscanf': {'arg_idx': 2, 'fmt_idx': 1},
            'sscanf': {'arg_idx': 2, 'fmt_idx': 1},
            'read': {'arg_idx': 1, 'len_idx': 2},
            'fread': {'arg_idx': 0, 'len_idx': 2},
            'fgets': {'arg_idx': 0, 'len_idx': 1},
            'gets': {'arg_idx': 0},
            'getchar': {'return_val': True},
            'getline': {'arg_idx': 0}
        }

    def _get_code(self, node):
        """将 AST 节点转回 C 代码字符串，用于 display"""
        if node is None: return ""
        try:
            return self.generator.visit(node)
        except:
            return ""

    def _register_constraint(self, stmt: str, lineno: int, kind: str = 'path_context'):
        key = (lineno, stmt, kind)
        if key in self._constraint_keys:
            return
        self._constraint_keys.add(key)
        self.constraints_by_line[lineno].append({
            'stmt': stmt,
            'lineno': lineno,
            'kind': kind
        })

    # --- 1. 变量声明提取 (visit_Decl) ---
    def visit_Decl(self, node):
        """对应原脚本的 _extract_vars_from_block"""
        if node.name:
            # 获取类型字符串
            try:
                # 处理数组 array[100]
                if isinstance(node.type, c_ast.ArrayDecl):
                    type_name = self._get_type_name(node.type.type)
                    dim = self._get_code(node.type.dim)
                    self.var_declarations[node.name] = f"{type_name}[{dim}]"
                    self.var_array_sizes[node.name] = dim
                # 处理指针 *p
                elif isinstance(node.type, c_ast.PtrDecl):
                    type_name = self._get_type_name(node.type.type)
                    self.var_declarations[node.name] = f"{type_name}*"
                # 普通变量 int a
                else:
                    type_name = self._get_type_name(node.type)
                    self.var_declarations[node.name] = type_name
            except:
                self.var_declarations[node.name] = "unknown"
        
        # 如果声明时有初始化赋值 int a = b + 1;
        if node.init:
            self._handle_assignment(node.name, node.init, node.coord.line if node.coord else 0)

    def _get_type_name(self, type_node):
        """递归获取类型名称"""
        if isinstance(type_node, c_ast.TypeDecl):
            return self._get_type_name(type_node.type)
        if isinstance(type_node, c_ast.IdentifierType):
            return " ".join(type_node.names)
        if isinstance(type_node, c_ast.PtrDecl):
            return self._get_type_name(type_node.type) + "*"
        return "unknown"

    # --- 2. 函数定义与控制流 (Loop Stack) ---
    def visit_FuncDef(self, node):
        self.current_func = node.decl.name
        self.visit(node.body)
        self.current_func = "global"

    def visit_If(self, node):
        cond_str = self._get_code(node.cond)
        lineno = node.coord.line if node.coord else 0
        
        self._record_condition(node.cond, cond_str, lineno, "if")
        
        # 【新增】：遍历 if 的条件节点
        if node.cond:
            self.visit(node.cond)
            
        # 递归 True 分支
        true_stmt = f"if({cond_str})"
        self._register_constraint(true_stmt, lineno)
        self.path_constraints.append({'code': true_stmt, 'lineno': lineno})
        if node.iftrue: self.visit(node.iftrue)
        self.path_constraints.pop()
        
        # 递归 False 分支
        if node.iffalse:
            false_stmt = f"if(!({cond_str}))"
            self._register_constraint(false_stmt, lineno)
            self.path_constraints.append({'code': false_stmt, 'lineno': lineno})
            self.visit(node.iffalse)
            self.path_constraints.pop()

    def visit_While(self, node):
        cond_str = self._get_code(node.cond)
        lineno = node.coord.line if node.coord else 0
        
        self._record_condition(node.cond, cond_str, lineno, "while")
        
        # 【新增】：显式遍历条件节点，捕获条件中的 FuncCall（如 scanf）
        if node.cond: 
            self.visit(node.cond)
        
        loop_sig = f"while({cond_str})"
        self.loop_stack.append(loop_sig)
        self._register_constraint(loop_sig, lineno)
        self.path_constraints.append({'code': loop_sig, 'lineno': lineno})
        
        if node.stmt: self.visit(node.stmt)
        
        self.path_constraints.pop()
        self.loop_stack.pop()

    def visit_For(self, node):
        init_str = self._get_code(node.init) if node.init else ""
        cond_str = self._get_code(node.cond) if node.cond else ""
        next_str = self._get_code(node.next) if node.next else ""
        lineno = node.coord.line if node.coord else 0
        
        if node.cond:
            self._record_condition(node.cond, cond_str, lineno, "for")
        
        # 【新增】：遍历 for 的初始化、条件和迭代表达式
        if node.init: self.visit(node.init)
        if node.cond: self.visit(node.cond)
        if node.next: self.visit(node.next)
        
        loop_sig = f"for({init_str}; {cond_str}; {next_str})"
        self.loop_stack.append(loop_sig)
        self._register_constraint(loop_sig, lineno)
        self.path_constraints.append({'code': loop_sig, 'lineno': lineno})
        
        if node.stmt: self.visit(node.stmt)
        
        self.path_constraints.pop()
        self.loop_stack.pop()
    def visit_Switch(self, node):
            cond_str = self._get_code(node.cond)
            lineno = node.coord.line if node.coord else 0
            
            # 提取 switch(var) 中的变量
            switch_vars = self._extract_vars(node.cond)
            
            # 将当前 switch 的变量和代码推入栈中，供 case 节点读取
            self.switch_stack.append({
                'vars': switch_vars,
                'cond_str': cond_str
            })
            # 【新增】：遍历 switch 的条件节点
            if node.cond:
                self.visit(node.cond)
            # 将 switch 加入路径约束
            switch_stmt = f"switch({cond_str})"
            self._register_constraint(switch_stmt, lineno)
            self.path_constraints.append({'code': switch_stmt, 'lineno': lineno})
            
            # 遍历内部的 case 语句
            if node.stmt: 
                self.visit(node.stmt)
                
            # 恢复现场
            self.path_constraints.pop()
            self.switch_stack.pop()

    def visit_Case(self, node):
        case_val_str = self._get_code(node.expr)
        lineno = node.coord.line if node.coord else 0
        
        # 1. 为 switch 条件中的变量记录这个 case 带来的 constraint 和 magic value
        if self.switch_stack:
            current_switch = self.switch_stack[-1]
            magic_vals = self._extract_constants(node.expr)
            
            for var in current_switch['vars']:
                self.use_sites[var].append({
                    'kind': 'constraint',
                    'stmt': f"case {case_val_str}",
                    'lineno': lineno,
                    'derived_var': None,
                    'constraints': self._get_current_constraint_lines(),
                    'magic_values': magic_vals
                })
        
        # 2. 将当前的 case 加入路径约束栈 (这样 case 内部的赋值也能带上这个约束)
        case_stmt = f"case {case_val_str}:"
        self._register_constraint(case_stmt, lineno)
        self.path_constraints.append({'code': case_stmt, 'lineno': lineno})
        
        # 3. 遍历 case 下面的语句块
        if node.stmts:
            for stmt in node.stmts:
                self.visit(stmt)
                
        # 恢复现场
        self.path_constraints.pop()
    # --- 3. 核心：输入函数提取 (visit_FuncCall) ---
    def visit_FuncCall(self, node):
        func_name = self._get_code(node.name)
        lineno = node.coord.line if node.coord else 0
        
        if func_name in self.input_funcs:
            config = self.input_funcs[func_name]
            
            # 提取元数据 (input_metadata)
            metadata = {
                "format_string": None,
                "length_arg": None,
                "fields_accessed": [],
                "array_limit": None,
                "loop_depth": len(self.loop_stack),
                "loop_structure": list(self.loop_stack) # 完整保留循环栈
            }
            
            # 尝试提取格式化字符串
            if 'fmt_idx' in config and node.args and len(node.args.exprs) > config['fmt_idx']:
                fmt_arg = node.args.exprs[config['fmt_idx']]
                if isinstance(fmt_arg, c_ast.Constant) and fmt_arg.type == 'string':
                    metadata['format_string'] = fmt_arg.value

            # 提取输入参数
            if 'arg_idx' in config and node.args:
                start_idx = config['arg_idx']
                if len(node.args.exprs) > start_idx:
                    for i in range(start_idx, len(node.args.exprs)):
                        arg = node.args.exprs[i]
                        self._process_input_arg(arg, func_name, lineno, self._get_code(node), metadata)
        
        # 继续遍历参数
        if node.args: self.visit(node.args)

    def _process_input_arg(self, arg_node, func_name, lineno, full_code, metadata):
        # 剥离 & 符号
        target_node = arg_node
        if isinstance(arg_node, c_ast.UnaryOp) and arg_node.op == '&':
            target_node = arg_node.expr
        
        var_name = self._get_code(target_node)
        
        # 过滤常量
        if '"' in var_name or "'" in var_name: return

        # 检查结构体字段访问 (a.b or a->b)
        if '.' in var_name or '->' in var_name:
            field_acc = re.split(r'\.|->', var_name)[-1]
            metadata['fields_accessed'].append(field_acc)
        
        # 关联数组大小限制
        base_name = re.match(r'([a-zA-Z_]\w*)', var_name)
        if base_name:
            bn = base_name.group(1)
            if bn in self.var_array_sizes:
                metadata['array_limit'] = self.var_array_sizes[bn]
        
        self._add_input(var_name, lineno, func_name, full_code, metadata)

    # --- 4. 赋值与派生追踪 (visit_Assignment) ---
    def visit_Assignment(self, node):
        lhs = self._get_code(node.lvalue)
        rhs_node = node.rvalue
        lineno = node.coord.line if node.coord else 0
        
        # [新增] 检查右值是否为带有 return_val 标记的输入函数调用
        if isinstance(rhs_node, c_ast.FuncCall):
            func_name = self._get_code(rhs_node.name)
            if func_name in self.input_funcs and self.input_funcs[func_name].get('return_val'):
                # 如果是 input = getchar()，将 LHS (lhs) 标记为输入变量
                metadata = {
                    "format_string": None,
                    "length_arg": None,
                    "fields_accessed": [],
                    "array_limit": None,
                    "loop_depth": len(self.loop_stack),
                    "loop_structure": list(self.loop_stack)
                }
                self._add_input(lhs, lineno, func_name, f"{lhs} = {self._get_code(rhs_node)}", metadata)

        # 原有逻辑保持不变
        rhs = self._get_code(node.rvalue)
        self._handle_assignment(lhs, node.rvalue, lineno, f"{lhs} = {rhs}")
        self.visit(node.rvalue)

    def _handle_assignment(self, lhs_name, rhs_node, lineno, code_str=None):
        """处理 y = x + 1 这种派生关系"""
        #消融实验
        # return
        rhs_vars = self._extract_vars(rhs_node)
        
        for v in rhs_vars:
            # 记录 v 被用于计算 lhs
            # 这里的格式严格对齐 VNode 的要求
            self.use_sites[v].append({
                'kind': 'assignment',
                'stmt': code_str if code_str else f"{lhs_name} = ...",
                'lineno': lineno,
                'derived_var': lhs_name,
                # 保留完整的约束对象
                'constraints': self._get_current_constraint_lines(),
                'magic_values': self._extract_constants(rhs_node)
            })

    # --- 辅助方法 ---

    def _record_condition(self, cond_node, cond_str, lineno, kind):
        """记录变量出现在 if/while 条件中"""
        vars_in_cond = self._extract_vars(cond_node)
        magic_vals = self._extract_constants(cond_node)
        
        for var in vars_in_cond:
            self.use_sites[var].append({
                'kind': 'constraint',
                'stmt': f"{kind}({cond_str})",
                'lineno': lineno,
                'derived_var': None,
                'constraints': self._get_current_constraint_lines(),
                'magic_values': magic_vals
            })

    def _get_current_constraints_obj(self):
        """将当前的 path_constraints 转换为原脚本风格的 Dict 列表"""
        # 原脚本 constraints 是 List[Dict]，包含 stmt, lineno 等
        # 这里的 path_constraints 已经是这个结构了
        return [
            {
                'stmt': c['code'],
                'lineno': c['lineno'],
                'kind': 'path_context' 
            } for c in self.path_constraints
        ]

    def _get_current_constraint_lines(self):
        ordered_lines = []
        seen = set()
        for c in self.path_constraints:
            lineno = c['lineno']
            if lineno not in seen:
                seen.add(lineno)
                ordered_lines.append(lineno)
        return ordered_lines

    def _extract_vars(self, node) -> Set[str]:
        found = set()
        class VFinder(c_ast.NodeVisitor):
            def visit_ID(self, n): found.add(n.name)
            def visit_ArrayRef(self, n):
                if isinstance(n.name, c_ast.ID): found.add(n.name.name)
                self.visit(n.subscript) # 递归下标
        VFinder().visit(node)
        return found

    def _extract_constants(self, node) -> List[str]:
        consts = []
        class CFinder(c_ast.NodeVisitor):
            def visit_Constant(self, n): consts.append(n.value)
        CFinder().visit(node)
        return consts

    def _add_input(self, name, lineno, func, code, metadata):
        # 去重逻辑
        for item in self.user_inputs:
            if item['lineno'] == lineno and item['name'] == name:
                return

        # 获取变量类型
        base_name = re.match(r'([a-zA-Z_]\w*)', name)
        vtype = "unknown"
        if base_name:
            bn = base_name.group(1)
            vtype = self.var_declarations.get(bn, "unknown")

        self.user_inputs.append({
            'execution_order': len(self.user_inputs) + 1,
            'name': name,
            'type': vtype,
            'lineno': lineno,
            'input_function': func,
            'source_code': code,
            'context_function': self.current_func,
            'loop_context': " -> ".join(metadata['loop_structure']), # 兼容原输出
            'input_metadata': metadata
        })

# ==========================================
# 3. 主逻辑
# ==========================================

class CUserInputExtractorAST:
    def __init__(self):
        self.visitor = FullLogicVisitor()

    def parse_file(self, filename: str, output_json: str = None) -> Dict[str, Any]:
        if not os.path.exists(filename):
            print(f"[-] 文件不存在: {filename}")
            return {}

        print(f"[+] 正在分析文件 (AST模式): {filename}")
        
        # 调用 pycparser，使用 cpp 和 fake_libc
        # 这是相比原脚本最大的升级：使用真正的 C 预处理器
        try:
            ast = parse_file(
                filename,
                use_cpp=True,
                cpp_path='cpp',
                cpp_args=[
                    '-nostdinc',
                    f'-I{FAKE_LIBC_PATH}',
                    '-D__attribute__(x)=',
                    '-D__extension__=',
                    '-D__restrict=',
                    '-D__inline='
                ]
            )
        except Exception as e:
            print(f"[-] AST 解析失败: {e}")
            return {}

        # 遍历
        self.visitor.visit(ast)
        
        # 构建 VTree
        vtree_report = self._build_vtrees()
        
        result = {
            "filename": filename,
            "inputs": self.visitor.user_inputs,
            "constraints_by_line": {str(k): v for k, v in sorted(self.visitor.constraints_by_line.items())},
            "variable_trees": {k: v.to_dict() for k, v in vtree_report.items()}
        }

        if output_json:
            with open(output_json, 'w', encoding='utf-8') as f:
                json.dump(result, f, indent=2, ensure_ascii=False)
            print(f"[+] 分析结果已保存: {output_json}")
            
        return result

    def _build_vtrees(self):
        report = {}
        processed_vars = set()
        for item in self.visitor.user_inputs:
            var_name = item['name']
            if var_name in processed_vars: continue
            
            report[var_name] = self._build_single_tree(var_name, visited=set())
            processed_vars.add(var_name)
        return report

    def _build_single_tree(self, var_name: str, visited: Set[str]) -> VNode:
        if var_name in visited: return VNode('recursion_limit', var_name, 0)
        
        root = VNode('var', var_name, 0)
        current_visited = visited.copy()
        current_visited.add(var_name)
        
        # 查找该变量的所有使用点（约束、赋值）
        # 注意：这里的 var_name 可能包含数组下标，需要模糊匹配或提取基名
        # 为了简单，这里匹配精确名称或基名
        base_name = re.match(r'([a-zA-Z_]\w*)', var_name)
        target_name = base_name.group(1) if base_name else var_name

        if target_name in self.visitor.use_sites:
            sites = sorted(self.visitor.use_sites[target_name], key=lambda x: x['lineno'])
            for s in sites:
                node = VNode(
                    kind=s['kind'],
                    name=target_name,
                    lineno=s['lineno'],
                    code=s['stmt'],
                    constraints=s['constraints'], # 完整的约束列表
                    magic_values=s['magic_values'],
                    derived_var=s['derived_var']
                )
                
                if node.derived_var:
                    child = self._build_single_tree(node.derived_var, current_visited)
                    node.children.append(child)
                
                root.children.append(node)
        return root

def main():
    if len(sys.argv) < 2:
        print("Usage: python var_tree_final.py <c_file> [output.json]")
        return
    
    c_file = sys.argv[1]
    json_out = sys.argv[2] if len(sys.argv) > 2 else "analysis_result.json"
    
    extractor = CUserInputExtractorAST()
    extractor.parse_file(c_file, json_out)

if __name__ == "__main__":
    main()
