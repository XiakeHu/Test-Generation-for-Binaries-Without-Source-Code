import os
import sys
import shutil
import traceback
import logging
import csv
import re
import subprocess
from pathlib import Path
from typing import List, Tuple

# 导入你的自定义模块
import config
import judge
import generate_testsets

logger = config.get_logger(__name__)


def get_all_source_lines(source_path: str) -> List[int]:
    """Fallback targets when coverage feedback is unavailable."""
    all_lines = []

    try:
        with open(source_path, "r", encoding="utf-8") as f:
            for lineno, line in enumerate(f, start=1):
                if line.strip():
                    all_lines.append(lineno)
    except Exception as e:
        logger.error(f"读取源代码失败，无法构造全量未覆盖行: {e}")

    return all_lines


def get_uncovered_targets_with_fallback(work_dir: str, source_path: str) -> List[int]:
    """Parse coverage feedback and fall back to all source lines on failure."""
    info_file = os.path.join(work_dir, "coverage.info")
    uncovered_lines = []
    has_da_records = False

    if not os.path.exists(info_file):
        fallback_lines = get_all_source_lines(source_path)
        logger.warning(
            f"coverage.info 不存在，回退为将全部代码视为未覆盖代码，共 {len(fallback_lines)} 行。"
        )
        return fallback_lines

    try:
        with open(info_file, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line.startswith("DA:"):
                    has_da_records = True
                    parts = line.split(":")[-1].split(",")
                    if len(parts) >= 2 and parts[1] == "0":
                        uncovered_lines.append(int(parts[0]))
    except Exception as e:
        fallback_lines = get_all_source_lines(source_path)
        logger.error(f"解析 coverage.info 失败: {e}")
        logger.warning(
            f"覆盖率反馈不可用，回退为将全部代码视为未覆盖代码，共 {len(fallback_lines)} 行。"
        )
        return fallback_lines

    if not has_da_records:
        fallback_lines = get_all_source_lines(source_path)
        logger.warning(
            f"coverage.info 未包含有效的覆盖率行记录，回退为将全部代码视为未覆盖代码，共 {len(fallback_lines)} 行。"
        )
        return fallback_lines

    return sorted(list(set(uncovered_lines)))

def get_uncovered_targets(work_dir: str) -> list:
    """解析 LCOV info 文件，提取未覆盖的行号"""
    info_file = os.path.join(work_dir, "coverage.info")
    uncovered_lines = []
    
    if not os.path.exists(info_file):
        return uncovered_lines
        
    try:
        with open(info_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                # 解析 DA:<行号>,<执行次数>
                if line.startswith("DA:"):
                    parts = line.split(":")[-1].split(",")
                    if len(parts) >= 2 and parts[1] == "0":
                        uncovered_lines.append(int(parts[0]))
    except Exception as e:
        logger.error(f"解析 coverage.info 失败: {e}")
        
    return sorted(list(set(uncovered_lines)))

def compile_for_coverage(source_path: str, work_dir: str, code_name: str) -> Tuple[bool, str, str]:
    """
    极简编译：调用 GCC/G++ 打上 GCOV 探针。
    【核心改动】：通过设置 cwd=work_dir，让生成的 .gcno 探针文件直接落在模型专属目录下。
    """
    compiler = "g++" if source_path.endswith(('.cpp', '.cc', '.cxx', '.c++')) else "gcc"
    binary_path = os.path.join(work_dir, f"{code_name}")
    
    # 构建带 coverage 探针的编译命令
    cmd = [
        compiler,
        "-fprofile-arcs", "-ftest-coverage",  # 开启覆盖率插桩
        source_path,
        "-o", binary_path,
        "-lm", "-pthread", "-lgcov"           # 链接库
    ]
    
    try:
        # 执行编译，指定工作目录为大模型专属目录
        subprocess.run(cmd, capture_output=True, text=True, check=True, cwd=work_dir)
        logger.info(f"编译成功，产物已放入: {work_dir}")
        return True, "Success", binary_path
    except subprocess.CalledProcessError as e:
        err_msg = f"编译失败:\n{e.stderr}"
        logger.error(err_msg)
        return False, err_msg, ""
    except FileNotFoundError:
        err_msg = f"未找到编译器 {compiler}"
        logger.error(err_msg)
        return False, err_msg, ""

def calculate_full_coverage(work_dir: str, source_filename: str):
    """
    执行 LCOV 计算，提取 行、函数、分支 覆盖率
    """
    info_file = os.path.join(work_dir, "coverage.info")
    line_cov, func_cov, branch_cov = 0.0, 0.0, 0.0
    coverage_ok = False

    try:
        if False:
            pass
    except OSError as e:
        logger.warning(f"清理旧 coverage.info 失败: {e}")
    
    try:
        # 1. 捕获 work_dir 中的覆盖率数据 (.gcda 和 .gcno)
        cmd_capture = [
            "lcov", "--capture", "--directory", work_dir,
            "--output-file", info_file, "--quiet", "--rc", "lcov_branch_coverage=1"
        ]
        subprocess.run(cmd_capture, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # 2. 提取特定文件的覆盖率 (源码文件名)
        cmd_extract = [
            "lcov", "--extract", info_file, f"*{source_filename}*",
            "--output-file", info_file, "--quiet", "--rc", "lcov_branch_coverage=1"
        ]
        subprocess.run(cmd_extract, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)

        # 3. 解析结果
        cmd_summary = [
            "lcov", "--summary", info_file, "--rc", "lcov_branch_coverage=1"
        ]
        result = subprocess.run(cmd_summary, capture_output=True, text=True)
        
        match_line = re.search(r"lines\.*:\s+([\d\.]+)%", result.stdout)
        match_func = re.search(r"functions\.*:\s+([\d\.]+)%", result.stdout)
        match_branch = re.search(r"branches\.*:\s+([\d\.]+)%", result.stdout)
        
        if match_line: line_cov = float(match_line.group(1))
        if match_func: func_cov = float(match_func.group(1))
        if match_branch: branch_cov = float(match_branch.group(1))
        coverage_ok = True
        
        logger.info(f"覆盖率统计 -> 行: {line_cov}%, 函数: {func_cov}%, 分支: {branch_cov}%")
        
    except Exception as e:
        logger.error(f"覆盖率计算失败 (可能未触发覆盖): {e}")
        
    return line_cov, func_cov, branch_cov, coverage_ok

def save_experiment_data(base_dir, model_name, index, name, is_no_input, line_cov, func_cov, branch_cov):
    """保存包含多维覆盖率的 CSV 报告"""
    csv_path = os.path.join(base_dir, model_name, "coverage_report.csv")
    file_exists = os.path.isfile(csv_path)

    try:
        with open(csv_path, 'a', newline='', encoding='utf-8') as f:
            writer = csv.writer(f)
            if not file_exists:
                writer.writerow([
                    "ID", "Filename", "NoInput", 
                    "LineCoverage(%)", "FuncCoverage(%)", "BranchCoverage(%)", 
                    "Model"
                ])
            
            writer.writerow([
                index, name, is_no_input, 
                f"{line_cov:.2f}", f"{func_cov:.2f}", f"{branch_cov:.2f}", 
                model_name
            ])
    except Exception as e:
        logger.error(f"保存覆盖率数据失败: {e}")

def process_single_file(file_path, base_dir, model_name, file_index, total_files):
    logger.info(f"=== 处理进度: {file_index}/{total_files} | 文件: {os.path.basename(file_path)} ===")
    
    source_name = os.path.basename(file_path)
    code_name = os.path.splitext(source_name)[0]
    
    model_root_dir = os.path.join(base_dir, model_name)
    work_dir = os.path.join(model_root_dir, code_name)
    os.makedirs(work_dir, exist_ok=True)

    config.code_name = code_name
    config.input_path = file_path
    config.decompiled_code_dir = model_root_dir 

    # 1. 编译打桩 (仅需一次)
    success, msg, binary_path = compile_for_coverage(file_path, work_dir, code_name)
    if not success:
        save_experiment_data(base_dir, model_name, file_index, code_name, False, 0, 0, 0)
        return

    generate_testsets.init_dir()
    
    MAX_ITERATIONS = 3
    TARGET_BRANCH_COV = 95.0
    
    best_line_cov, best_func_cov, best_branch_cov = 0.0, 0.0, 0.0
    is_no_input_global = False
    coverage_feedback_ready = True

    for iteration in range(MAX_ITERATIONS):
        logger.info(f"--- [{code_name}] 开始第 {iteration + 1}/{MAX_ITERATIONS} 轮迭代生成 ---")
        
        # 2. 生成测试用例
        if iteration == 0:
            # 第一轮：全局盲测
            try:
                is_no_input = generate_testsets.process_file(file_path, 100)
                is_no_input_global = is_no_input
            except Exception as e:
                logger.error(f"[{code_name}] 首轮生成出错:\n{traceback.format_exc()}")
                break
            current_txt_path = os.path.join(work_dir, f"{code_name}.txt")
        else:
            # 第 N 轮：定向爆破未覆盖分支
            if coverage_feedback_ready:
                uncovered_lines = get_uncovered_targets_with_fallback(work_dir, file_path)
            else:
                uncovered_lines = get_all_source_lines(file_path)
                logger.warning(
                    f"[{code_name}] 上一轮覆盖率计算失败，当前迭代将全部代码视为未覆盖代码。"
                )
            if not uncovered_lines:
                logger.info(f"[{code_name}] 无明确未覆盖行，提前结束迭代。")
                break
                
            logger.info(f"[{code_name}] 发现未覆盖行: {uncovered_lines[:20]}... 尝试定向生成")
            try:
                # 调用新增的反馈生成接口
                has_new = generate_testsets.generate_feedback_cases(file_path, uncovered_lines, iteration, 20)
                if not has_new:
                    logger.info(f"[{code_name}] 定向生成未能产出新用例，结束迭代。")
                    break
            except Exception as e:
                logger.error(f"[{code_name}] 定向生成出错:\n{traceback.format_exc()}")
                break
            
            current_txt_path = os.path.join(work_dir, f"{code_name}_iter_{iteration}.txt")

        # 3. 运行测试并收集覆盖率
        if not is_no_input_global:
            if os.path.exists(current_txt_path):
                cases = judge.load_test_cases(current_txt_path)
                logger.info(f"[{code_name}] 第 {iteration + 1} 轮读取到 {len(cases)} 个新用例，跑测中...")
                
                original_cwd = os.getcwd()
                os.chdir(work_dir)
                try:
                    for case in cases:
                        judge.run_program(binary_path, case, timeout=1)
                finally:
                    os.chdir(original_cwd)
        else:
            if iteration == 0:
                logger.info(f"[{code_name}] 无需输入，直接运行...")
                original_cwd = os.getcwd()
                os.chdir(work_dir)
                try:
                    judge.run_program(binary_path, "", timeout=1)
                finally:
                    os.chdir(original_cwd)
                break # 无输入程序不需要迭代

        # 4. 计算累加覆盖率
        line_cov, func_cov, branch_cov, coverage_feedback_ready = calculate_full_coverage(work_dir, source_name)
        best_line_cov, best_func_cov, best_branch_cov = line_cov, func_cov, branch_cov
        
        if branch_cov >= TARGET_BRANCH_COV:
            logger.info(f"[{code_name}] 分支覆盖率已达标 ({branch_cov}%)，提前结束迭代。")
            break

    # 5. 保存最终最高结果
    save_experiment_data(base_dir, model_name, file_index, code_name, is_no_input_global, best_line_cov, best_func_cov, best_branch_cov)
def main():
    if len(sys.argv) < 3:
        print("用法: python main2.py <C代码文件或文件夹路径> <大模型名字>")
        sys.exit(1)

    input_path = os.path.abspath(sys.argv[1])
    model_name = sys.argv[2]
    # 固定 base_dir 路径，与之前保持一致
    base_dir = Path(__file__).resolve().parent
    
    if not os.path.exists(input_path):
        print(f"输入路径不存在: {input_path}")
        return

    config.init_config()
    config.model_name = model_name 

    logger.info(f"🚀 启动 - 模型: {model_name} | 输入: {input_path}")

    # 创建模型根目录
    os.makedirs(os.path.join(base_dir, model_name), exist_ok=True)

    target_files = []
    if os.path.isfile(input_path) and input_path.endswith(('.c', '.cpp')):
        target_files.append(input_path)
    elif os.path.isdir(input_path):
        for file_name in os.listdir(input_path):
            file_path = os.path.join(input_path, file_name)
            if os.path.isfile(file_path) and file_name.endswith(('.c', '.cpp')):
                target_files.append(file_path)
                
    target_files.sort()
    total_files = len(target_files)

    for i, file_path in enumerate(target_files, 1):
        try:
            process_single_file(file_path, base_dir, model_name, i, total_files)
        except Exception:
            logger.error(f"处理 {file_path} 异常:\n{traceback.format_exc()}")

if __name__ == "__main__":
    main()
