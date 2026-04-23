# Binary Test Generation with VarTree + LLM

---

当前实验更准确地说是“针对反编译修复后的代码”，而不是直接对原始二进制文件一键完成全部预处理。如需预处理时处理反编译代码请联系补充

## 1. 项目简介

本项目面向“无源码二进制程序测试生成”场景，核心思路是：

1. 以反编译并修复后的 `C/C++` 代码作为输入；
2. 用 `AST + VarTree` 提取输入变量、变量依赖、边界约束和关键常数；
3. 将约束传给大模型，让大模型生成可执行的 Python 测试数据生成脚本；
4. 本地执行脚本得到测试用例；
5. 用 `gcov/lcov` 统计覆盖率，并将未覆盖代码反馈给大模型继续迭代。

当前仓库中的主入口已经实现了“约束提取 -> 用例生成 -> 覆盖率反馈 -> 迭代优化”的完整闭环。

## 2. 当前代码能做什么

2.1 核心功能

- 基于 `pycparser` 对输入 `C/C++` 文件做静态分析；
- 构建 VarTree，提取变量依赖、循环/数组边界、路径约束、magic values；
- 让 LLM 生成 `run_gen()` Python 函数，而不是直接吐原始测试数据；
- 自动执行生成脚本，捕获运行错误并做脚本自修复；
- 基于 `gcov/lcov` 做覆盖率统计；
- 基于未覆盖代码继续做定向反馈生成；
- 当覆盖率计算失败或未产生有效覆盖记录时，回退为“将全部代码视为未覆盖代码”，保证反馈阶段不中断。

2.2 适用输入

主流程 `main2.py` 当前接收的是：

- 单个 `.c/.cpp` 文件
- 或一个包含多个 `.c/.cpp` 文件的目录




## 3. 主要文件说明

- `main2.py`
  主实验入口。负责编译插桩、调用测试生成、执行用例、统计覆盖率、进行反馈迭代，并把结果保存到 `coverage_report.csv`。

- `generate_testsets.py`
  约束感知测试集生成逻辑。包括：
  - VarTree 结果整合
  - Prompt 构造
  - Python 生成脚本执行与报错修复
  - 覆盖率反馈下的定向再生成

- `var_test2.py`
  AST 静态分析器与 VarTree 构建器。负责从代码中提取：
  - 用户输入变量
  - 变量赋值依赖
  - 条件约束
  - magic values
  - 变量树结构

- `judge.py`
  负责加载测试用例并运行目标程序。

- `call_llm.py`
  统一封装大模型调用。

- `config.py`
  全局路径、日志、模型名等基础配置。

- `test.json`
  路径配置和模型 API 配置。

- `fake_libc_include/`
  `pycparser` 解析系统头文件时使用的 fake libc 头文件目录。


## 4. 环境依赖

### 4.1 Python 依赖

建议 Python 3.8 及以上。

项目代码中直接使用到了以下库：

- `openai`
- `requests`
- `langchain-openai`
- `colorlog`
- `pycparser`
- `json_repair`

可参考安装：

```bash
pip install openai requests langchain-openai colorlog pycparser json_repair
```

### 4.2 系统依赖

需要以下命令行工具在环境变量中可用：

- `gcc` 或 `g++`
- `gcov`
- `lcov`
- `cpp`

其中：

- `var_test2.py` 使用 `cpp + fake_libc_include` 做 AST 预处理；
- `main2.py` 使用 `gcc/g++ + gcov/lcov` 做覆盖率插桩与统计。

### 4.3 可选依赖

- `IDA Pro`

论文中的预处理阶段会先由 IDA Pro 生成初始伪代码，再交给 LLM 修复；不过当前 `main2.py` 不直接调用 IDA，它默认接收的是已经整理好的 `.c/.cpp` 输入。


## 5. 配置说明

### 5.1 修改 `test.json`

运行前需要先检查 `test.json`：

- 路径配置是否与你当前环境一致；
- 模型 `base_url / api_key / model / max_tokens` 是否可用。

### 5.2 默认模型

真正运行时，`main2.py` 会使用命令行传入的模型名覆盖它。

## 6. 用法

### 6.1 主实验入口

对单个文件运行：
```bash
python main2.py path/to/file.c deepseek
```
对整个目录运行：
```bash
python main2.py path/to/corpus deepseek
```

### 6.2 单独运行 VarTree 分析

如果只想看静态分析结果，可以单独运行：

```bash
python var_test2.py path/to/file.c analysis_result.json
```
这会输出该文件的输入变量、约束和 VarTree 结果。

## 7. 主流程说明

`main2.py` 的工作流程如下：

1. 读取输入文件或目录；
2. 对每个目标文件做带覆盖率插桩的编译；
3. 调用 `generate_testsets.process_file()` 生成初始测试集；
4. 执行测试集；
5. 用 `lcov` 统计行/函数/分支覆盖率；
6. 提取未覆盖代码并调用 `generate_feedback_cases()` 做定向反馈；
7. 达到目标分支覆盖率或迭代上限后结束；
8. 将结果写入 `<模型目录>/coverage_report.csv`。


## 8. 输出结果说明

运行 `main2.py` 后，主结果会写到：

```text
<repo>/<model_name>/
```

每个程序会生成一个独立工作目录：

```text
<repo>/<model_name>/<program_name>/
```

常见输出包括：

- `<program_name>.txt`
  首轮生成的测试用例

- `<program_name>_iter_<n>.txt`
  第 `n` 轮反馈生成的测试用例

- `debug_scripts/`
  保存大模型生成的 Python 脚本

- `segment_outputs/`
  保存不同分段生成结果

- `<program_name>_var_info.json`
  完整的 VarTree 与静态分析结果

- `coverage.info`
  lcov 覆盖率明细

- `<repo>/<model_name>/coverage_report.csv`
  全部程序的覆盖率汇总


## 9. 边界测试 `min/max` 开关说明

### 9.1 当前默认状态

当前代码默认只启用 `random` 组生成。

位置：`generate_testsets.py` 的 `process_file()` 函数。

当前默认代码是：

```python
# meta_data = ask_variables_and_segments(analyzed_vars_str, test_size)
# segments = meta_data.get("segments", []) if isinstance(meta_data, dict) else []
# if not segments: segments = [{"id": 0, "type": "random", "constraints": {}}]
segments = [{
    "id": 0,
    "type": "random",
    "num_cases": test_size,
    "constraints": {}
}]
```

也就是说，虽然 `generate_segment_cases()` 已经实现了：

- `min`
- `random`
- `max`

三种策略的 Prompt 规则，但默认实验只打开了 `random`。

### 9.2 如何打开 `min/max`

把上面这段默认 `random` 代码改回分组模式即可。

直接启用已经写好的：

```python
meta_data = ask_variables_and_segments(analyzed_vars_str, test_size)
segments = meta_data.get("segments", []) if isinstance(meta_data, dict) else []
if not segments:
    segments = [{"id": 0, "type": "random", "constraints": {}}]
```

### 9.3 `min/max` 的作用位置

在 `generate_testsets.py` 的 `generate_segment_cases()` 中已经内置了三类策略：

- `min`
  最小边界测试，如 `0 / 1 / 空字符串 / 最小循环次数`

- `max`
  最大边界测试，如 `数组上限 / 大整数 / 最大循环次数`

- `random`
  满足约束下的随机探索

注意：

- magic values 只会在 `random` 组里暴露给大模型；
- 覆盖率反馈迭代阶段 `generate_feedback_cases()` 当前也只会继续修正 `random` 组脚本。


## 10. 消融实验开关说明

本项目代码里已经保留了用于论文消融实验的注释式开关。

### 10.1 去掉 Magic Value

位置：`generate_testsets.py` 的 `get_analysis_vars()` 末尾。

原代码中保留了这段注释：

```python
# #消融测试
# for item in raw_vars:
#     item['magic_values'] = []
```

如果要做 `w/o Magic Value Extraction`，直接取消注释即可

### 10.2 去掉 VarTree Tracking

位置：`var_test2.py` 的 `_handle_assignment()` 函数。

原代码中保留了这段注释：

```python
def _handle_assignment(self, lhs_name, rhs_node, lineno, code_str=None):
    """处理 y = x + 1 这种派生关系"""
    #消融实验
    # return
    rhs_vars = self._extract_vars(rhs_node)
```
如果要做 `w/o VarTree Tracking`，直接把 `return` 打开


## 11. 覆盖率反馈说明

`main2.py` 现在已经加入了覆盖率失败回退逻辑：

- 正常情况下：从 `coverage.info` 提取未覆盖行；
- `lcov` 失败、`coverage.info` 缺失或没有有效记录时：
  直接把整份源文件的有效代码行当作“未覆盖代码”传给反馈生成。

## 12. 复现实验建议

### 12.1 模型对比实验

直接更换命令行模型名即可，例如：

```bash
python main2.py ./dataset deepseek
python main2.py ./dataset chatgpt
python main2.py ./dataset gemini
python main2.py ./dataset doubao
```

最终对比：

- `<repo>/<model_name>/coverage_report.csv`

### 12.2 边界测试实验

启用 `min/random/max` 分组后重新运行。

### 12.3 消融实验

分别打开：

- `generate_testsets.py` 中的 `magic_values = []`
- `var_test2.py` 中的 `_handle_assignment()` 里的 `return`

然后重新运行主实验。
