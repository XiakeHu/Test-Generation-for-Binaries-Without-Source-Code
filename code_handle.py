import re
from collections import deque
import config


def parse_compile_errors(error_output: str) -> str:
    """
    将编译器输出清洗为简洁格式：
    l:<行号> row:<列号> error|warning: <错误信息>
    多个错误用换行分隔。
    """
    # 匹配形如： /path/to/file.c:9:22: error: something...
    pattern = re.compile(r".*?:([0-9]+):([0-9]+):\s*(error|warning):\s*(.*)")
    simplified_errors = []

    for match in pattern.finditer(error_output):
        line, col, etype, msg = match.groups()
        simplified_errors.append(f"{etype}:  line:{line} row:{col} {msg.strip()} ")

    if not simplified_errors:
        return "no compile errors detected"

    return "\n".join(simplified_errors)


def output_result(processing_order, head_file, struct_info):
    row_detail = {}
    with open(f"{config.output_dir}/{config.code_name}_test.c", "w", encoding="utf-8") as res_file:
        # 写入函数
        print("begin to write res out file.")

        for func_name in processing_order:
            with open(f"{config.output_dir}/{config.code_name}_{func_name}.out", "r", encoding="utf-8") as cur_file:
                func_code = cur_file.read()
                _, func_codes = get_struct_from_analysis(func_code)
                _, func_codes = get_typedef_from_analysis("\n".join(func_codes))
                func_codes = get_st_from_analysis("\n".join(func_codes))
                _, func_codes = split_var(func_codes, func_name)

                func_tmp = "\n".join(func_codes) + "\n"

                func_tmp = optimize_code(func_tmp)

                row_detail[func_name] = count_lines(func_tmp)
                config.func_contend += func_tmp
                print("it is over for ", func_name, row_detail[func_name])

        config.head_contend = ""
        for head in head_file:
            config.head_contend += head + "\n"
        # 开始写入
        all_row = 0
        # 计算头文件行数
        config.head_contend = optimize_code(config.head_contend)
        head_row = count_lines(config.head_contend) - 1
        row_detail["head"] = head_row + all_row
        all_row += head_row
        # print("head file", config.head_contend)
        # print(head_row)

        # 计算结构体行数
        config.return_struct = optimize_code(struct_info) + "\n"
        if config.return_struct != "":
            struct_row = count_lines(config.return_struct) - 1
            row_detail["struct"] = struct_row + all_row
            all_row += struct_row
            # print(return_struct)
            # print(struct_row)
        # 计算全局变量行数

        config.g_val = optimize_code(config.g_val)

        if config.g_val != "":
            val_row = count_lines(config.g_val) - 1
            row_detail["val"] = val_row + all_row
            all_row += val_row
            # print(g_val)
            # print(val_row)
        # 计算各个函数的行数
        for func_name in processing_order:
            row_detail[func_name] += all_row - 1
            all_row = row_detail[func_name]
        with open(f"{config.chunk_dir}/{config.code_name}_all_struct", "w", encoding="utf-8") as ff:
            ff.write(config.return_struct)
        with open(f"{config.chunk_dir}/{config.code_name}_g_val", "w", encoding="utf-8") as ff:
            ff.write(config.g_val)
        c_file = config.head_contend + config.return_struct + config.g_val + config.func_contend
        # + config.return_struct去除了加结构体
        # res_file.write(func_contend)

        global_val = config.return_struct + config.g_val

        # new_code=optimize_code(c_file)
        # print(c_file)
        res_file.write(c_file)
        # print(row_detail)
    print("c source is over.")
    return global_val, row_detail, c_file


def is_core_business_function(self, func_name):
    """
    正向筛选：仅保留核心业务函数（根据已知类名）

    Args:
        func_name: 函数名称

    Returns:
        bool: 如果是核心业务函数返回True，否则返回False
    """
    # 已知用户自定义业务类和核心函数
    core_classes = ['Graph', 'ShortestPathSystem']
    core_functions = ['main', '_start']  # 入口函数

    # 保留业务类的成员函数（名字修饰中包含类名）
    for cls in core_classes:
        if cls.lower() in func_name.lower():
            return True

    # 保留核心入口函数
    if func_name in core_functions:
        return True

    return False


def is_standard_library_or_compiler(self, func_name):
    """
    反向排除：识别标准库或编译器生成函数

    Args:
        func_name: 函数名称

    Returns:
        bool: 如果是标准库或编译器生成函数返回True，否则返回False
    """
    # 标准库命名特征
    stdlib_patterns = [
        'std::', 'stl::', '::__',  # 标准库命名空间
        'vector', 'map', 'set', 'queue', 'deque', 'string',  # STL容器
        'printf', 'scanf', 'malloc', 'free', 'memcpy', 'str'  # C标准库
    ]

    # 编译器生成函数特征
    compiler_patterns = [
        '__cxa_', '_Unwind_', '__gxx_personality',  # 异常处理
        '__static_initialization', '__do_global',  # 全局初始化
        'register_tm_clones', 'deregister_tm_clones', 'frame_dummy',  # 线程模型
        'operator new', 'operator delete',  # 内存管理
        '_ZSt', '_ZNSt', '_ZNKSt'  # 标准库名字修饰
    ]

    # 检查是否匹配标准库或编译器特征
    for pattern in stdlib_patterns + compiler_patterns:
        if pattern in func_name:
            return True
    return False


def get_struct_from_analysis(text):
    # 定义正则表达式模式来匹配结构体定义
    pattern = r'typedef struct(?:\s+(\w+))?\s*\{([^}]+)\}\s*(\w+);'

    # 查找所有匹配项
    matches = re.findall(pattern, text)

    # 提取结构体信息
    structs = deque()
    for match in matches:
        struct_name = match[2]
        members = match[1].strip().split('\n')
        members = [member.strip() for member in members if member.strip() and not member.strip().startswith('//')]
        structs.appendleft({
            'struct_name': struct_name,
            'members': members,
        })
    # 去掉结构体定义后的剩余代码
    remaining_text = re.sub(pattern, '', text)

    return structs, remaining_text.split("\n")


def get_typedef_from_analysis(text):
    # 定义正则表达式模式来匹配结构体定义
    type_list = set()
    pattern = r'typedef\s+[^\n;]+;'
    mas = re.findall(pattern, text)
    # 输出匹配到的 typedef 定义
    for ma in mas:
        type_list.add(ma)
    # 去掉结构体定义后的剩余代码
    remaining_text = re.sub(pattern, '', text)

    return type_list, remaining_text.split("\n")


def get_st_from_analysis(text: str):
    pattern = r'struct\s*\w+\s*\{(?:[^{}]*(\{[^{}]*\}[^{}]*)*)\};'
    pattern3 = r'enum\s*\w+\s*\{(?:[^{}]*(\{[^{}]*\}[^{}]*)*)\};'
    pattern2 = r'^struct.*?;$'
    pattern4 = r'typedef struct\s*.*\s*\{([^}]+)\}\s*\w+;'
    pattern5 = r'typedef\s+.*?;'
    pattern6 = r'struct\s*\w+\s*\{(?:[^{}]*(\{[^{}]*\}[^{}]*)*)\} __attribute__.*?;'
    pattern7 = r'typedef struct\s*\{(?:[^{}]*(\{[^{}]*\}[^{}]*)*)\} __attribute__.*?;'
    text = re.sub(pattern, '', text)
    text = re.sub(pattern2, '', text)
    text = re.sub(pattern3, '', text)
    text = re.sub(pattern4, '', text)
    text = re.sub(pattern6, '', text)
    text = re.sub(pattern7, '', text)
    text = re.sub(pattern5, '', text, flags=re.DOTALL)
    text_list = text.split("\n")
    f = False
    new = []
    t = 0
    for te in text_list:
        if f:
            t += te.count("{")
            t -= te.count("}")
            if t == 0:
                f = False
            continue
        if te.startswith("struct ") and "{" in te and "}" not in te and ";" not in te and "};" in text_list:
            f = True
            t += te.count("{")
            t -= te.count("}")
            continue
        new.append(te)
    return new


def extract_structures_and_typedefs(self, decompiled_code):
    """
    从反编译代码中提取结构体和typedef定义

    Args:
        decompiled_code: 反编译的代码字符串

    Returns:
        tuple: (结构体列表, typedef集合, 剩余代码行列表)
    """
    # 提取结构体定义
    struct_pattern = r'typedef struct(?:\s+(\w+))?\s*\{([^}]+)\}\s*(\w+);'
    struct_matches = re.findall(struct_pattern, decompiled_code)

    structs = deque()
    for match in struct_matches:
        struct_name = match[2]
        members = match[1].strip().split('\n')
        members = [member.strip() for member in members if member.strip() and not member.strip().startswith('//')]
        structs.appendleft({
            'struct_name': struct_name,
            'members': members,
        })

    # 移除结构体定义后的剩余代码
    remaining_code = re.sub(struct_pattern, '', decompiled_code)

    # 提取typedef定义
    typedef_pattern = r'typedef\s+[^\n;]+;'
    typedef_matches = re.findall(typedef_pattern, remaining_code)
    typedefs = set(typedef_matches)

    # 移除typedef定义后的剩余代码
    remaining_code = re.sub(typedef_pattern, '', remaining_code)

    return structs, typedefs, remaining_code.split("\n")


def get_union_from_analysis(test: str):
    test_list = test.split("\n")
    flag = False
    t = 0
    new_test = []
    for te in test_list:
        if flag:
            t += te.count("{")
            t -= te.count("}")
            if t == 0:
                flag = False
            continue
        if "union" in te and "{" in te:
            flag = True
            t += te.count("{")
            t -= te.count("}")
            continue
        new_test.append(te)
    return new_test


def extract_functions(code):
    lines = code.splitlines()
    function_start = None
    brace_count = 0
    res = []
    remain = []
    for i, line in enumerate(lines):
        if "{" in line and "= {" not in line and function_start is None:
            function_start = i
            brace_count += line.count("{")
            brace_count -= line.count("}")
        elif function_start is not None:
            brace_count += line.count("{")
            brace_count -= line.count("}")
            if brace_count == 0:
                function_end = i
                function_code = "\n".join(lines[function_start:function_end + 1])
                res.append(function_code)
                function_start = None
        else:
            remain.append(line)
    return res, remain


def extract_function(c_code, function_name):
    c_code_list = c_code.split("\n")
    # 1. 定位函数定义行（排除注释）
    func_def_start_line = -1
    for k in range(len(c_code_list)):
        line = c_code_list[k].strip()
        if line.startswith("//") or ("/*" in line and "*/" in line):
            continue  # 跳过注释行
        if f"{function_name}(" in line and ";" not in line:
            func_def_start_line = k
            break
    if func_def_start_line == -1:
        return None
    c_code = "\n".join(c_code_list[func_def_start_line:])

    # 2. 定位函数名起始位置
    func_start_index = c_code.find(f"{function_name}(")
    if func_start_index == -1:
        return None

    # 3. 定位返回类型起始位置
    type_start_index = func_start_index
    while type_start_index > 0 and c_code[type_start_index - 1] != "\n":
        type_start_index -= 1

    # 4. 匹配大括号（核心优化：排除字符串内的括号）
    brace_count = 0
    end_brace_index = -1  # 初始化，避免未赋值
    in_quote = None  # 标记是否在字符串内（None/'\''/'"'）
    len_c = len(c_code)

    for i in range(type_start_index, len_c):
        # 处理字符串引号（单引号或双引号）
        current_char = c_code[i]
        if current_char in ["'", '"']:
            if in_quote == current_char:
                in_quote = None  # 退出字符串
            elif in_quote is None:
                in_quote = current_char  # 进入字符串

        # 仅当不在字符串内时，才计数括号
        if in_quote is None:
            if current_char == "{":
                brace_count += 1
            elif current_char == "}":
                brace_count -= 1
                if brace_count == 0:
                    end_brace_index = i
                    break

    # 5. 处理未找到结束括号的情况
    if end_brace_index == -1:
        return None  # 避免引用未赋值的变量

    return c_code[type_start_index:end_brace_index + 1]


def modify(str_list):
    # 获取结构体
    text = "\n".join(str_list)
    structs, str_list = get_struct_from_analysis(text)
    headers = []
    remaining_code = []
    # 按行分割输入的代码
    for line in str_list:
        # 去除行首和行尾的空白字符
        if line.startswith('#include'):
            # 如果行以 #include 开头，将其添加到 headers 列表中
            headers.append(line)
        else:
            # 否则，将其添加到 remaining_code 列表中
            remaining_code.append(line)
    # 将 remaining_code 列表中的行重新组合成字符串
    code_without_headers = '\n'.join(remaining_code)
    return headers, code_without_headers, structs


def get_last_operand(asm_line):
    init_asm = asm_line.split()
    characters_to_remove = " :,;"
    tmp = []
    for line in init_asm:
        cleaned_code = ''.join([char for char in line if char not in characters_to_remove])
        tmp.append(cleaned_code)
    if tmp:
        first_operand = tmp[0]
        other_operand = tmp[1:]
        return first_operand, other_operand
    return None


def split_var(codes, func_name):
    string_codes = "\n".join(codes)
    func_codes = extract_function(string_codes + "\n```", func_name)
    if func_codes:
        for func_code in func_codes.split("\n"):
            if not func_code or func_code not in codes:
                continue
            codes.remove(func_code)
        return codes, func_codes.split("\n")
    else:
        return codes, []


def count_lines(text):
    # 统计换行符的数量
    line_count = text.count('\n')
    # 如果字符串不为空，行数需要加 1
    if text:
        line_count += 1
    return line_count


def cut(func_code: str, func_name):
    code_list = func_code.split("\n")
    res = []
    flag = False
    tmp = []
    all_struct = ""
    for i, c in enumerate(code_list):
        if flag:
            tmp.append(c)
            if c.startswith("}"):
                flag = False
                if ";" in tmp[-1]:
                    res.append("\n".join(tmp))
                tmp = []
        elif "{" not in c and (c.startswith("typedef") or c.startswith("struct")) and ";" in c:
            res.append(c)

        elif (c.startswith("typedef") or c.startswith("struct") or c.startswith("union") or c.startswith(
                "enum")) and "{" in c:
            flag = True
            tmp.append(c)
        elif "{" not in c and (c.startswith("typedef") or c.startswith("struct")) and ";" not in c:
            res.append(c)
            k = i + 1
            while ";" not in code_list[k] and k < len(code_list):
                res.append(code_list[k])
                k += 1
            res.append(code_list[k])
    if res:
        all_struct = "\n".join(res)
    # ---------------------------------------
    for struct_row in all_struct.split("\n"):
        if not struct_row:
            continue
        code_list.remove(struct_row)
    func_codes = code_list
    var_list, func_codes = split_var(func_codes, func_name)
    g_val = ""
    head_file = []
    for val in var_list:
        if val.strip().startswith("#include") or val.strip().startswith("#define"):
            if val.strip() not in head_file:
                head_file.append(val.strip())
            continue
        if val == "" or "#if" in val or "#endif" in val or "#else" in val:
            continue

        g_val += val + "\n"
    return head_file, all_struct, g_val, "\n".join(func_codes)


def calculate_processing_order(self, dependencies):
    """
    生成从底层到顶层的处理顺序（拓扑排序）

    Args:
        dependencies: 函数依赖关系字典

    Returns:
        list: 按处理顺序排列的函数列表
    """
    call_graph = {func: data['calls'] for func, data in dependencies.items()}

    # 计算每个节点的入度
    in_degree = {func: 0 for func in call_graph}
    for func in call_graph:
        for call in call_graph[func]:
            if call in in_degree:
                in_degree[call] += 1

    # 使用队列进行拓扑排序
    queue = deque([func for func in in_degree if in_degree[func] == 0])
    order = []

    while queue:
        current = queue.popleft()
        order.append(current)
        for call in call_graph[current]:
            if call in in_degree:
                in_degree[call] -= 1
                if in_degree[call] == 0:
                    queue.append(call)

    # 处理可能的循环依赖
    for func in in_degree:
        if in_degree[func] != 0 and func not in order:
            order.append(func)

    return order[::-1]  # 反转列表，从底层到顶层


def calculate_order(deps):
    """生成从底层到顶层的处理顺序"""
    call_graph = {func: data['calls'] for func, data in deps.items()}
    # 计算每个节点的入度
    in_degree = {func: 0 for func in call_graph}
    for func in call_graph:
        for call in call_graph[func]:
            in_degree[call] += 1

    queue = deque([func for func in in_degree if in_degree[func] == 0])
    order = []
    # 拓扑排序
    while queue:
        current = queue.popleft()
        order.append(current)
        for call in call_graph[current]:
            in_degree[call] -= 1
            if in_degree[call] == 0:
                queue.append(call)
    for call in in_degree:
        if in_degree[call] != 0:
            order.append(call)

    return order[::-1]


def optimize_code(code: str):
    # 打开输入文件以读取内容
    codes = code.split("\n")
    new_code = []
    for line in codes:
        t = line.strip()
        if t.startswith(',') or t.startswith('、') or t.startswith('.') or t.startswith('`') or t.startswith('·'):
            continue
        if starts_with_chinese(t):
            continue
        line = line.replace("__int64", "int")
        if t.startswith("//"):
            continue
        new_code.append(line)
    return "\n".join(new_code)


def extract_errors_row(error_report):
    error_pattern = r'^([^:]+):(\d+):(\d+):\s*error:\s*(.*)$'
    errors = []
    lines = error_report.split('\n')
    i = 0
    while i < len(lines):
        match = re.match(error_pattern, lines[i])
        if match:
            file_path = match.group(1)
            line_number = int(match.group(2))
            error_message = "error:" + match.group(4)
            # 检查后续行是否为错误信息的延续
            j = i + 1
            while j < len(lines) and not re.match(error_pattern, lines[j]) and "In function" not in lines[j]:
                error_message += '\n' + lines[j].strip()
                j += 1
            errors.append((line_number, error_message))
            i = j
        else:
            i += 1
    return errors


def starts_with_chinese(s):
    if not s:
        return False
    first_char = s[0]
    return '\u4e00' <= first_char <= '\u9fff'
