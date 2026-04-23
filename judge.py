import re
import json
import time
import os
import sys
import subprocess
import logging
from typing import List, Dict, Tuple
from pathlib import Path
from openai import OpenAI
from openai import APIError, APIConnectionError, Timeout


def run_program(program_path, input_data, timeout=30):
    """运行程序并返回输出内容（字符串）"""
    try:
        process = subprocess.Popen(
            str(program_path),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            shell=False,
            text=True
        )

        try:
            stdout, stderr = process.communicate(
                input=input_data,
                timeout=timeout
            )
            if process.returncode != 0:
                error_msg = stderr if stderr else f"Exit code {process.returncode}"
                return False, None, f"Program exited with error: {error_msg}"
            return True, stdout, None
        except subprocess.TimeoutExpired:
            process.kill()
            return False, None, "Execution timed out"
    except Exception as e:
        return False, None, f"Execution failed: {str(e)}"


def load_test_cases(file_path):
    """从文件加载测试用例，使用空行分割"""
    if not os.path.exists(file_path):
        logging.error(f"测试用例文件不存在: {file_path}")
        sys.exit(1)
    
    with open(file_path, 'r', encoding='utf-8') as f:
        content = f.read()
    # 检查文件内容是否为空
    if content.strip() == "":  # 如果文件为空或仅包含空白字符
        logging.warning(f"测试用例文件 {file_path} 为空，返回空测试用例列表。")
        return [""]
    # 使用空行分割测试用例，过滤空测试用例
    test_cases = [case.strip() for case in content.split('\n\n') if case.strip()]
    return test_cases
