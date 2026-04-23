import json
import os
from datetime import datetime
from pathlib import Path
import requests
import config
from openai import OpenAI
from langchain_openai import OpenAIEmbeddings
from code_handle import modify
from code_handle import optimize_code

project_base = Path(__file__).resolve().parent
with open(f'{project_base}/test.json', 'r', encoding='utf-8') as f:
    data = json.load(f)
logger = config.get_logger(__name__)


def init_embeddings(model_name="gitee"):
    api_key = data['Models'][model_name]['api_key'][0]
    base_url = data['Models'][model_name]['base_url']
    model = data['Models'][model_name]['model']
    return api_key, base_url, model

def llm_request(message, model_name="deepseek", temperature=0.1):
    client = OpenAI(
        api_key=data['Models'][model_name]['api_key'][0],
        base_url=data['Models'][model_name]['base_url'],
    )
    # 构造初始对话消息（用户输入+空助手回复前缀）
    messages = [
        {'role': 'user', 'content': message},
        {'role': 'assistant', 'content': "", "prefix": True}
    ]
    
    # 发送首次请求
    completion = client.chat.completions.create(
        model=data['Models'][model_name]['model'],
        messages=messages,
        max_tokens=data['Models'][model_name]['max_tokens'],
        temperature=temperature,
    )
    
    # 若回复因长度限制截断，则抛弃本次结果并重新请求
    while completion.choices[0].finish_reason == "length":
        print("回复因长度限制被截断，抛弃本次结果并重新发送请求...")
        # 重新发送请求（使用初始messages，不保留上次截断内容）
        completion = client.chat.completions.create(
            model=data['Models'][model_name]['model'],
            messages=messages,
            max_tokens=data['Models'][model_name]['max_tokens'],
            temperature=temperature,  # 保持随机性参数一致
        )
    
    # 返回最终未被截断的回复
    llm_output = completion.choices[0].message.content
    return llm_output





def llm_restore(model_name="deepseek"):
    pre = data['Prompts']['correct']['pre']
    suffix = data['Prompts']['correct']['suffix']
    all_struct = config.get_all_struct()
    all_deps, processing_order = config.get_processing_order()
    start_time = datetime.now()
    exclaim = {}
    new_code = ""
    # 对每个函数进行修正
    for func_name in processing_order:
        logger.info(f"funname:{func_name}")
        func_dep = ""
        for dep_name in all_deps[func_name]["calls"]:
            if os.path.exists(f"{config.output_dir}/{config.code_name}_{dep_name}.out"):
                with open(f"{config.output_dir}/{config.code_name}_{dep_name}.out", "r", encoding="utf-8") as f:
                    dep_code = f.read()
            else:
                with open(f"{config.chunk_dir}/{config.code_name}_{dep_name}", "r", encoding="utf-8") as f:
                    dep_code = f.read()
            func_dep = func_dep + dep_code + "\n"
        with open(f"{config.chunk_dir}/{config.code_name}_{func_name}", "r", encoding="utf-8") as file:
            origin_code = file.read()
        message = f"""
/* 代码修正任务 */

1. **核心要求**：
    - 根据全局架构上下文，重构以下函数代码。仅修正该函数本身，不要添加其他任何函数。
    - 移除所有IDA特有语法（如 __fastcall、[ebp+X]、__int64、__assert_fail 等），并转换为符合标准C语言或C++代码。
    - 确保功能一致性：转换后的代码必须与原始代码在逻辑和功能上完全一致，保留所有计算、条件判断、内存操作等细节，不得修改或省略任何重要功能。
    - 确保所有函数参数使用 typedef 定义的类型，不要随意更改函数签名，函数返回值必须与声明一致。
    - 标准C或C++代码格式：使用标准的C或C++数据类型和库函数，避免使用IDA伪代码中的寄存器操作（如 xmm0、eax 等）以及特定平台的低级指令。对于C++，应使用标准C++语言特性，如类、对象、构造函数等。
    - 不接收任何命令行参数或外部输入，函数应独立执行并根据提供的依赖函数进行调用。
    - 局部变量命名要求：根据上下文推测每个局部变量的用途，并使用具有描述性和语义性的名称。避免使用寄存器名或无意义的 a1, a2 等命名。应根据变量的作用，使用如 counter, temp_result, input_value 等命名，以提高代码可读性和理解性。

2. **已定义的结构体和类型**：
{all_struct}

3. **依赖的函数定义**：
{func_dep}

4. **输入代码**：
{origin_code}

5. **输出要求**：
    - 仅返回标准C或C++代码，不允许包含任何解释性文字。
    - 仅修正该函数本身，不要修改函数名，确保函数体内部的调用依赖函数使用传入参数与依赖函数定义一致。
    - 不要重复声明任何已给出的依赖函数或全局变量，确保代码简洁，避免重复定义。
    - 所有已定义的全局变量均已提供，避免在代码中重新定义或声明。
    - 依赖的函数已给出，调用时根据已提供代码规范进行调用，参数类型与返回值应与定义一致。
    - 局部变量命名规范：对于每个局部变量，根据其作用和上下文推测合适的名称。避免使用IDA伪代码中的寄存器名或无意义的 a1, a2 等变量名。每个局部变量的名称应有助于理解其功能，例如使用 counter, temp_result, value 等具有描述性的命名。
"""

        # print(message)
        check_path = Path(f"{config.output_dir}/{config.code_name}_{func_name}.out")
        try:
            if not check_path.exists():
                new_code = llm_request(message, model_name)
            else:
                logger.warning(f"funname:{func_name} done")
                with open(f"{config.output_dir}/{config.code_name}_{func_name}.out", "r", encoding="utf-8") as f:
                    new_code = f.read()
        except (ConnectionError, TimeoutError, requests.exceptions.RequestException) as e:
            # 网络相关异常
            logger.error(f"LLM网络请求失败: {e}")
            return False, f"网络请求失败: {e}"
        except Exception as e:
            logger.error(f"调用LLM失败: {e}")
            return False, f"LLM调用失败: {e}"

        # print(new_code)
        with open(f"{config.output_dir}/{config.code_name}_{func_name}.out", "w", encoding='utf-8') as file:
            str_list = new_code.split('\n')
            head, code, structs = modify(str_list)
            # print("modify")
            code = optimize_code(code)
            # print("optimze_coda1")
            # 获取声明
            tmp = ""
            for tm in code:
                if tm == "{":
                    tmp += ";"
                    break
                elif tm == "\n":
                    tmp += " "
                else:
                    tmp += tm
            exclaim[func_name] = tmp
            logger.info(f"{func_name} file is writing")
            file.write(code)

    for ex in exclaim:
        config.g_val += exclaim[ex] + "\n"
    return True, f"得到还原代码:{new_code}"


# 编译部分
def llm_correct(temp_c_file, error_info, c_code, rule_prompt=None, model_name="deepseek"):
    prompt = f"""
            以下C代码编译失败，请修复错误并返回可编译的完整代码：
            不要以markdown形式回复，不要在文件首尾添加任何符号而导致不能直接编译。
            请只返回修复后的完整可编译C代码，不要包含任何其他解释、符号或注释。
            错误信息：
            {error_info}

            需要修复的代码：
            {c_code}
            
            修复规则参考:
            {rule_prompt}
            
            """

    try:
        # 调用LLM获取修复后的代码
        fixed_code = llm_request(prompt, model_name)
        c_code = fixed_code.strip()
        with open(temp_c_file, "w", encoding="utf-8") as f:
            f.write(c_code)
    except (ConnectionError, TimeoutError, requests.exceptions.RequestException) as e:
        # 网络相关异常
        logger.error(f"LLM网络请求失败: {e}")
        return False, f"网络请求失败: {e}"
    except Exception as e:
        logger.error(f"调用LLM失败: {e}")
        return False, f"LLM调用失败: {e}"
    return True, c_code


def get_code_change_reason(original_code, fixed_code, error_feedback):
    """获取大模型对代码改动的解释原因，并返回错误分类信息"""
    suffix = data['Prompts']['change']['suffix']
    prompt = f"""请解释你对以下代码的修改原因，基于之前的错误反馈：
    
    错误反馈：
    {error_feedback}

    原始代码：
    {original_code}

    修改后的代码：
    {fixed_code}

    {suffix}
    """
    try:
        response = llm_request(prompt)
        return response
    except Exception as e:
        print(f"获取代码改动原因失败: {e}")
        return None
# print(llm_request("test"))
