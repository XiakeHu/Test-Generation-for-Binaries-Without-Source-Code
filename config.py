import json
import logging
import sys
from pathlib import Path
import os
import colorlog
import code_handle

project_base = Path(__file__).resolve().parent

with open(f'{project_base}/test.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
    idat_root = data['Path']['base']['ida'] + "/idat"
    log_root = data['Path']['log']['linux']
    input_root = data['Path']['base']['input']
input_path = f"{input_root}/hello"  # 测试使用指定文件
input_file_name = os.path.basename(input_path)#test.c
code_name = os.path.splitext(input_file_name)[0]#test
exec_path = f"{input_root}/{code_name}"
chunk_dir = f"{project_base}/chunk"
output_dir = f"{project_base}/new_out"
state_dir = f"{project_base}/state"
error_dir = f"{project_base}/error"
decompiled_code_dir = f"{project_base}/decompiled_code"

Path(log_root).mkdir(parents=True, exist_ok=True)
Path(input_root).mkdir(parents=True, exist_ok=True)
Path(output_dir).mkdir(parents=True, exist_ok=True)
Path(state_dir).mkdir(parents=True, exist_ok=True)
Path(decompiled_code_dir).mkdir(parents=True, exist_ok=True)

# 配置日志
# 配置日志
# 定义彩色日志格式
log_colors = {
    'DEBUG': 'cyan',
    'INFO': 'green',
    'WARNING': 'yellow',
    'ERROR': 'red',
    'CRITICAL': 'bold_red',
}
formatter = colorlog.ColoredFormatter(
    '%(log_color)s%(asctime)s - %(levelname)s - %(message)s',
    log_colors=log_colors,
    datefmt='%Y-%m-%d %H:%M:%S'
)

# 创建一个 StreamHandler 用于控制台输出
console_handler = logging.StreamHandler(stream=sys.stderr)
console_handler.setFormatter(formatter)  # 将彩色格式器应用于控制台处理器

# 创建一个 FileHandler 用于文件输出 (文件不需要彩色)
file_handler = logging.FileHandler(f"{log_root}/{code_name}_main.log", encoding='utf-8')
file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))  # 文件使用普通格式

# 获取根 logger 并添加处理器
root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)  # 设置最低日志级别为INFO
root_logger.addHandler(file_handler)
root_logger.addHandler(console_handler)

head_contend = ""
return_struct = ""
g_val = ""
func_contend = ""
inputToken = []
outputToken = []
DEBUG = True
model_name = "deepseek"
def init_config():
    global head_contend,return_struct,g_val,func_contend,inputToken,outputToken,DEBUG
    head_contend = ""
    return_struct = ""
    g_val = ""
    func_contend = ""
    inputToken = []
    outputToken = []
    DEBUG = True


def get_logger(name=None):
    """返回指定名字的 logger"""
    return logging.getLogger(name)


def get_all_struct():
    with open(r"" + chunk_dir + f"/{code_name}_all_struct") as f:
        all_struct = f.read()
    return all_struct


def get_processing_order(is_first=True):
    with open(r"" + chunk_dir + f"/{code_name}_deps.json") as f:
        all_deps = json.load(f)
    logging.info(f"the dependency fun:, {all_deps}, {len(all_deps)}")
    processing_order = code_handle.calculate_order(all_deps)
    if not processing_order:
        logging.warning("无函数")
        return
    if is_first:
        logging.info(f"得到函数调用拓扑顺序：{processing_order}")
    return all_deps, processing_order
