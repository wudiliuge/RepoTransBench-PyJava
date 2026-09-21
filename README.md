# RepoTransBench-PyJava

RepoTransBench-PyJava 是 [RepoTransBench](https://github.com/DeepSoftwareAnalytics/RepoTransBench) 的 Windows + Docker + Python→Java 运行版本。本仓库封装了数据准备、模型调用、仓库翻译、最终编译和测试、指标计算、token/耗时统计、分批保存及人工选择合并。

推荐使用 `scripts/experiment.ps1`。它会固定实验条件、在调用模型前检查测试清单、逐项目保存证据，并让使用者决定何时合并批次。旧的 `run_single.ps1` 仍可用于调试，但不建议用它生成正式论文指标。

## 目录

- [运行流程概览](#运行流程概览)
- [环境要求](#环境要求)
- [首次安装](#首次安装)
- [创建实验](#创建实验)
- [运行项目和批次](#运行项目和批次)
- [测试清单](#测试清单)
- [测试用例数如何计算](#测试用例数如何计算)
- [指标定义](#指标定义)
- [最终编译和测试流程](#最终编译和测试流程)
- [token统计](#token统计)
- [耗时统计](#耗时统计)
- [查看结果](#查看结果)
- [合并批次](#合并批次)
- [停止和重跑](#停止和重跑)
- [脚本说明](#脚本说明)
- [常见问题](#常见问题)

## 运行流程概览

一次正式实验的流程如下：

```text
首次安装
  ↓
创建实验，固定模型、接口、迭代次数和超时
  ↓
提交一批项目
  ↓
检查项目、数据指纹和测试清单（不调用模型）
  ├─ 检查成功：提示输入API Key并开始运行
  └─ 无法可靠识别：生成“待补清单.json”，不会调用模型
  ↓
每个项目依次执行RepoTransAgent翻译
  ↓
复制生成工程，恢复数据集中的权威测试
  ↓
执行最终Maven编译和测试
  ↓
生成项目指标、测试用例明细、token和耗时
  ↓
批次独立保存
  ↓
由使用者选择需要合并的批次
```

一个实验固定一套条件。更换模型、API地址、生成参数、最大迭代次数或超时，应创建新的实验名称。每批结果独立保存，不会自动合并，也不会污染以前的批次。

## 环境要求

支持 Windows 10 或 Windows 11，需要：

1. Git for Windows；
2. WSL 2；
3. Docker Desktop，使用 Linux 容器；
4. PowerShell 5.1 或 PowerShell 7；
5. 本机 Python 3.10 或更高版本，用于实验状态和合并管理。

Java、Maven和代理运行依赖安装在Docker镜像中。建议为Docker Desktop分配至少8 GB内存。

检查环境：

```powershell
git --version
wsl --status
docker version
docker info
python --version
```

`docker version`需要同时显示Client和Server，Server应为Linux。

如果PowerShell不允许执行脚本，只对当前窗口临时放行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

后续命令均在仓库根目录执行：

```powershell
Set-Location "C:\Users\29133\Desktop\trans\repotransagent\RepoTransBench-PyJava"
```

路径可以换成实际仓库位置。仓库路径、输出路径和测试清单路径不要包含英文逗号，因为Docker会把逗号解释为挂载参数分隔符。

## 首次安装

启动Docker Desktop，然后执行：

```powershell
.\scripts\setup_windows.ps1
```

脚本会：

1. 构建 `repotransbench-pyjava:local` 镜像；
2. 下载并验证官方数据集；
3. 准备Python→Java子集；
4. 创建默认Docker数据卷；
5. 验证源项目和目标项目。

默认数据卷：

|数据卷|内容|
|---|---|
|`rtb_pyjava_raw_v1`|官方数据集原始内容|
|`rtb_pyjava_data_v1`|可运行的Python→Java任务|
|`rtb_pyjava_results_v1`|模型生成的Java工程和评测副本|
|`rtb_pyjava_run_v1`|旧入口的运行记录|
|`rtb_maven_cache_v1`|Maven依赖缓存|

本工具的实验入口固定从 `rtb_pyjava_data_v1` 读取数据。官方元数据有171条Python→Java记录，其中2条没有Java目标目录，因此默认卷中通常有169个配对项目。

下载中断时直接重新运行安装脚本。修改Dockerfile或依赖后可强制重建：

```powershell
.\scripts\setup_windows.ps1 -RebuildImage
```

列出默认数据卷中的项目：

```powershell
.\scripts\experiment.ps1 -ListProjects
```

项目名大小写和标点必须与输出完全一致。

## 创建实验

每个实验只执行一次 `-Init`。实验名称会成为Windows输出目录名，不能含有 `\ / : * ? " < > |`。

### OpenAI兼容服务示例

```powershell
.\scripts\experiment.ps1 -Init `
  -Name "模型A实验01" `
  -ModelName "服务商给出的实际模型名" `
  -BaseUrl "https://服务商地址/v1" `
  -MaxIterations 20 `
  -AgentTimeoutSeconds 3600 `
  -EvaluationTimeoutSeconds 600
```

`BaseUrl`可以填写：

- 服务根地址，例如 `https://api.example.com`；
- 以 `/v1` 结尾的地址；
- 完整的 `/chat/completions` 地址。填写完整地址时增加 `-EndpointKind full`。

不要把API Key放进URL或配置文件。运行或接口测试时脚本会安全提示输入。

### DeepSeek示例

```powershell
.\scripts\experiment.ps1 -Init `
  -Name "deepseek实验01" `
  -ModelName "deepseek-flash" `
  -BaseUrl "https://api.deepseek.com" `
  -MaxIterations 20
```

模型名称必须以服务端实际支持的名称为准。

### 硅基流动示例

```powershell
.\scripts\experiment.ps1 -Init `
  -Name "硅基流动_GLM实验01" `
  -ModelName "THUDM/GLM-Z1-9B-0414" `
  -BaseUrl "https://api.siliconflow.cn/v1" `
  -MaxIterations 20
```

### 本地Ollama示例

```powershell
.\scripts\experiment.ps1 -Init `
  -Name "本地Qwen实验01" `
  -ModelName "qwen2.5:7b" `
  -BaseUrl "http://host.docker.internal:11434" `
  -MaxIterations 20
```

本地无需认证的服务在提示API Key时可输入 `local`。Docker容器访问Windows宿主机服务时应使用 `host.docker.internal`，不能使用容器自己的 `localhost`。

### 额外生成参数

新建UTF-8 JSON文件，例如：

```json
{
  "temperature": 0.2,
  "top_p": 0.9,
  "max_tokens": 8192,
  "enable_thinking": false
}
```

创建实验时添加：

```powershell
-ModelOptionsFile "C:\实际路径\model_options.json"
```

支持的参数包括 `temperature`、`top_p`、`max_tokens`、`max_completion_tokens`、`seed`、`stop`、`presence_penalty`、`frequency_penalty`、`reasoning_effort` 和 `enable_thinking`。`max_tokens` 与 `max_completion_tokens` 不能同时填写。

### 可选接口测试

```powershell
.\scripts\experiment.ps1 -Probe -Name "模型A实验01"
```

这会发送一次真实短请求，可能产生少量费用。它只验证接口地址、鉴权和文本响应，不能证明仓库翻译一定成功。接口测试的token和耗时会计入整个实验的累计尝试消耗。

## 运行项目和批次

### 直接填写项目名

```powershell
.\scripts\experiment.ps1 -Run `
  -Name "模型A实验01" `
  -Projects "项目A","项目B","项目C"
```

每批项目数不固定，可以运行1个、2个、10个或更多。为了便于检查和失败隔离，建议每批不超过10个。

### 从文本文件读取项目

创建UTF-8文本文件，每行一个项目名：

```text
项目A
项目B
项目C
```

运行：

```powershell
.\scripts\experiment.ps1 -Run `
  -Name "模型A实验01" `
  -ProjectList "C:\实际路径\批次001_项目.txt"
```

### 使用显式测试清单

已准备好测试清单时：

```powershell
.\scripts\experiment.ps1 -Run `
  -Name "模型A实验01" `
  -ProjectList "C:\实际路径\批次001_项目.txt" `
  -InventoryFile "C:\实际路径\批次001_测试清单.json"
```

运行首先执行预检查，不调用模型。只有项目、数据指纹和测试清单全部通过后，脚本才提示输入API Key并开始翻译。项目按名单顺序串行运行，每个项目单独保存。

正常日志类似：

```text
批次001：正在检查项目和测试清单，不调用模型。
批次001：检查通过。
请输入服务商 API Key...
批次001：正在运行 项目A...
```

查看状态：

```powershell
.\scripts\experiment.ps1 -Status -Name "模型A实验01"
```

## 测试清单

测试清单是评测前固定的“规定测试集合”。它决定每个项目的测试用例总数，防止模型删除测试、只运行一部分测试或改变分母。

### 自动识别

工具会保守识别标准 `src/test/java` 中的普通JUnit `@Test` 方法。出现以下情况时会停止，不调用模型：

- 嵌套测试类；
- 测试继承；
- 自定义注解；
- JUnit 3测试；
- `@ParameterizedTest`、`@RepeatedTest`、`@TestFactory`或其他动态测试；
- 非标准测试目录；
- 无法确定完整类名或方法名。

停止是为了避免测试总数被静默少算。

### 补充显式清单

识别失败时会生成：

```text
批次\批次NNN\待补清单.json
批次\批次NNN\清单补充说明.md
批次\批次NNN\检查_...\inventory_audit.json
批次\批次NNN\检查_...\test_sources\项目名\...
```

显式清单格式：

```json
{
  "项目A": {
    "tests": {
      "com.example.original.ParserTest": [
        "testSimpleInput",
        "testInvalidInput"
      ],
      "com.example.publictests.PublicParserTest": [
        "testUnicodeInput"
      ]
    },
    "module_map": {},
    "_needs_review": false
  }
}
```

嵌套类使用JUnit XML中的二进制类名，例如：

```json
"com.example.ParserTest$NestedCases": ["testNestedInput"]
```

填写原则：

1. 使用完整Java测试类名；
2. 列出全部规定测试方法，不能只列本次通过的方法；
3. 同一个类中的方法名不能重复；
4. `_needs_review` 完成人工核对后改为 `false`；
5. `module_map`可以省略或留空；
6. 参数化和动态测试必须根据实际JUnit XML身份核对，不能仅凭Java方法名猜测。

校验JSON结构：

```powershell
.\scripts\experiment.ps1 -ValidateInventory `
  -Name "模型A实验01" `
  -InventoryFile "C:\实际路径\完整测试清单.json"
```

校验通过后，使用原项目名单和同一个实验名重新提交：

```powershell
.\scripts\experiment.ps1 -Run `
  -Name "模型A实验01" `
  -ProjectList "C:\实际路径\批次项目.txt" `
  -InventoryFile "C:\实际路径\完整测试清单.json"
```

如果原批次尚未开始任何项目，工具会继续使用这个“待补清单”批次，不需要 `-Retry`。结构校验只能证明JSON格式和基本对应关系正确，清单是否完整仍需人工核对。

## 测试用例数如何计算

### 一个测试用例是什么

在当前评测器中，一个测试用例由下面这对身份唯一确定：

```text
完整测试类名 + JUnit测试名称
```

对于普通JUnit测试，一个 `@Test` 方法计为一个测试用例。一个Java测试文件可以包含多个测试类，一个测试类也可以包含多个测试方法，因此“测试文件数”不等于“测试用例数”。

以下内容不单独计为测试用例：

- `@BeforeEach`、`@AfterEach`、`@BeforeAll`等生命周期方法；
- 普通辅助方法；
- 测试数据文件；
- Java源文件数量；
- 断言数量。一个测试方法包含多个断言时仍只算一个测试用例。

### 项目总测试数

对项目 `i`：

```text
N_i = 测试清单中该项目所有测试类的方法数量之和
```

例如：

```json
"tests": {
  "com.example.ParserTest": ["testA", "testB"],
  "com.example.PublicTest": ["testC"]
}
```

则该项目测试总数为3，而不是2个测试文件或测试类。

### 通过数

最终评测读取新生成的Maven Surefire/Failsafe `TEST-*.xml`。只有清单中的“测试类名 + 测试名称”在XML中存在且状态为passed，才计为通过。

- `failure`：不通过；
- `error`：不通过；
- `skipped`：不通过；
- 清单要求但XML缺失：不通过；
- 编译明确失败且没有测试报告：全部规定用例记为“因编译失败未运行”；
- 测试报告缺失、损坏或身份冲突：指标记为未知，不擅自按0处理；
- XML出现清单外测试：视为清单/发现不一致，项目测试指标记为未知。

测试用例逐条证据写入 `测试用例明细.csv`。

### 参数化、重复和动态测试

这类测试可能让一个Java方法在JUnit XML中产生多个名称不同的执行实例。当前工具要求清单与XML身份精确对应，无法确认时应补清单或适配评测器。不要把一次参数化方法简单当成一个普通 `@Test`，也不要根据某一次运行结果临时选择分母。

## 指标定义

设共有 `R` 个计划项目。项目 `i` 的通过测试数为 `T_i`，规定测试总数为 `N_i`。

### SR：任务成功率

仓库的全部规定测试通过才算项目成功：

```text
S_i = 1，当 T_i = N_i
S_i = 0，其他情况

SR = (Σ S_i) / R
```

### CR：编译通过率

最终权威评测中完整编译成功的项目占比：

```text
CR = 编译成功项目数 / R
```

CR使用最终独立评测结果，不使用代理某一轮日志中的临时编译结果。

### APR：平均测试用例通过率

先计算每个项目的测试通过率，再对项目做宏平均：

```text
APR = (1/R) × Σ(T_i / N_i)
```

每个项目权重相同，不受项目测试数量多少影响。

### AMPR：平均模块通过率

一个模块内全部规定测试通过，该模块才算通过。先计算每个项目的模块通过率，再对项目做宏平均。

默认情况下，一个Java测试类是一个模块。可通过 `module_map` 把多个测试类归到同一模块：

```json
"module_map": {
  "com.example.ParserTest": "parser",
  "com.example.PublicParserTest": "parser"
}
```

### 整体测试通过率

结果中还会显示按测试用例数量加权的整体通过率：

```text
整体测试通过率 = ΣT_i / ΣN_i
```

它与APR不同。测试多的项目对整体测试通过率影响更大；APR中每个项目权重相同。

### 未知指标

计划项目不会因为失败、缺少结果或证据不完整而从分母消失。如果任一项目无法得到可靠指标，批次精确值显示“未知”，并保存：

- 已知项目数量；
- 未知项目数量；
- 已知部分；
- 指标上下界。

工具不会把未知值自动当成0，也不会悄悄删除失败项目。

## 最终编译和测试流程

RepoTransAgent在翻译过程中可能多轮编译和测试。这些操作属于代理工作过程。代理结束后，指标工具还会执行一次独立最终评测：

1. 把生成工程复制到新的 `evaluation` 目录；
2. 删除复制工程中的旧 `target` 构建产物；
3. 删除模型可能生成或修改的 `src/test`；
4. 从数据集目标项目恢复权威测试；
5. 记录测试文件SHA256指纹；
6. 执行 `mvn ... test-compile`；
7. 检查标准Maven源码是否生成有效class文件；
8. 执行 `mvn ... test`；
9. 解析新的JUnit XML报告；
10. 再次校验测试文件指纹，防止构建过程改写测试。

如果完整编译失败，CR为0。工具仍会尝试执行Maven测试，使多模块工程中已经编译的模块可以提供部分APR/AMPR证据；如果没有生成测试报告，则该项目规定测试均记为未运行，SR、APR和AMPR为0。依赖下载、工具异常或证据不足导致的结果可能是未知，而不是编译失败。

当前评测主要支持标准Maven/JUnit工程。Gradle、非标准源码目录、自定义测试脚本或需要 `verify` 生命周期的集成测试需要额外适配。

## 轮数与API调用次数

`MaxIterations`是每个项目允许进入代理循环的最大轮数。结果中的“总设定轮数”在项目明细里就是该项目的`MaxIterations`，在批次或汇总里是纳入成本范围的各次项目尝试之和。

“实际轮数”在代理每次进入循环时加1，并通过独立的机器可读文件实时保存。正常结束、达到上限、模型返回格式错误，以及项目被外部超时终止前已经进入的轮次都会保留。它不通过日志行数或API请求数推算。

“API调用次数”统计实际发起并写入`requests.jsonl`的HTTP请求。一次实际轮次可能因为超时或请求异常触发重试，所以API调用次数可能大于实际轮数；代理初始化失败或某轮未能发起请求时，也可能小于实际轮数。

项目明细的主要字段按以下顺序输出：编译通过率、测试通过率、测试用例数、通过测试用例数、总设定轮数、实际轮数、总token消耗、API调用次数、总耗时。后面继续保留输入/输出token、翻译时间、评测时间、SR、APR、AMPR和问题说明等诊断字段。

## token统计

每次模型请求都会写入 `requests.jsonl`。工具读取服务端响应中的 `usage`：

- 输入token：`prompt_tokens`或`input_tokens`；
- 输出token：`completion_tokens`或`output_tokens`；
- 总token：输入与输出之和；
- API调用次数：已发起并记录的HTTP请求数量，包括成功、超时和失败调用；
- 缺少usage的请求数：没有返回有效usage的请求数量。

只有全部请求都返回一致、有效的usage时，输入、输出和总token才标记为“完整”。只要有一次请求缺少usage，精确总token显示“未知”，同时保留“已知输入token、已知输出token、已知总token”。工具不会根据文本长度估算缺失token。

常见缺失原因：

- 服务商没有在响应中返回usage；
- 请求超时或连接中断，没有完整响应；
- 服务商的usage字段格式与兼容协议不同；
- 流式或错误响应没有最终usage。

因此不同服务商即使使用相近模型，token完整率也可能不同。

批次成本包含该来源中的实际尝试。实验概览还包含未选入汇总的失败、重跑和真实接口测试，避免只统计成功项目造成成本偏差。

## 耗时统计

`项目明细.csv`中的主要时间：

|字段|范围|
|---|---|
|翻译时间|RepoTransAgent子进程，包括模型请求、重试、多轮修改以及代理内部编译/测试|
|评测时间|复制评测工程、恢复测试、最终编译、最终测试和解析报告|
|详细记录中的项目总时间|容器内该项目从任务建立到评测结束的时间|
|批次/汇总中的项目总时间|优先使用宿主机从启动Docker到Docker返回的端到端时间|

项目总时间通常不严格等于“翻译时间 + 评测时间”，差值包括：

- Docker启动和退出；
- Python模块加载；
- 数据指纹和清单检查；
- 创建目录、日志和符号链接；
- 读取请求记录；
- 生成指标文件；
- 压缩生成工程。

批次真实经过时间是该批运行的宿主机墙钟时间。各项目时间之和适合串行实验；如果以后改为并行运行，它不等于批次墙钟时间。跨批次累计耗时是各次已测运行时间相加，不包含批次之间的等待时间。

API Key输入、首次安装、模型下载以及批次运行前的独立清单检查不计入正式运行时间。

## 查看结果

默认输出位于仓库同级目录：

```text
RepoTransBench-exports\实验\实验名称\
├── 实验概览.md
├── experiment.json
├── 批次\
│   └── 批次001\
│       ├── 结果.md
│       ├── 项目明细.csv
│       ├── 项目明细.json
│       ├── 测试用例明细.csv
│       └── source.json
├── 汇总\
│   └── 汇总001\
│       ├── 结果.md
│       ├── 项目明细.csv
│       ├── 项目明细.json
│       ├── 测试用例明细.csv
│       └── source.json
└── 详细记录\
    └── 批次001\项目名\
        ├── launcher_timing.json
        ├── summary.json
        ├── 项目明细.csv
        └── tasks\项目名\
            ├── agent.log
            ├── agent_run_metrics.json
            ├── compile.log
            ├── test.log
            ├── requests.jsonl
            ├── evaluation.json
            └── result.json
```

常用文件：

|文件|用途|
|---|---|
|`实验概览.md`|所有批次状态和整个实验的累计尝试成本|
|`结果.md`|某个批次或汇总的主要指标|
|`项目明细.csv`|每个项目的编译、测试、token、时间和问题说明|
|`测试用例明细.csv`|每个规定测试用例的通过、失败、错误、跳过、缺失或未知状态|
|`项目明细.json`|项目级机器可读完整数据|
|`source.json`|不可变批次或汇总快照，用于安全合并|
|`agent.log`|代理翻译过程|
|`agent_run_metrics.json`|代理实时写入的最大轮数、实际轮数和结束状态|
|`compile.log`|最终编译日志|
|`test.log`|最终测试日志|
|`requests.jsonl`|每次模型请求的时间和usage证据|

不要手工修改 `experiment.json`、`source.json` 或详细记录。快照摘要用于检测来源是否被修改。

## 合并批次

批次不会自动合并。确认批次结果有效后再执行：

```powershell
.\scripts\experiment.ps1 -Merge `
  -Name "模型A实验01" `
  -Sources "批次001","批次002"
```

得到 `汇总001`。以后可以把新批次与已有汇总继续合并：

```powershell
.\scripts\experiment.ps1 -Merge `
  -Name "模型A实验01" `
  -Sources "汇总001","批次003"
```

每次汇总都是新的不可变快照，不覆盖原批次或旧汇总。同一个原始尝试即使通过多个来源间接出现，也只计算一次。

如果多个来源包含同一项目的不同尝试，工具不会自动选择最新或最好结果。使用 `-Choose` 明确选择性能结果：

```powershell
.\scripts\experiment.ps1 -Merge `
  -Name "模型A实验01" `
  -Sources "汇总001","批次004" `
  -Choose @{项目A="批次004"}
```

未被选作性能结果的真实尝试仍保留在成本统计中。

只有实验配置完全相同的来源才能合并。不同模型、接口、参数、迭代次数、超时或评测版本不能混在同一个正式汇总中。

## 停止和重跑

在另一个PowerShell窗口请求停止：

```powershell
.\scripts\experiment.ps1 -Stop -Name "模型A实验01"
```

工具会尝试停止当前项目，后续项目不再启动。然后检查状态：

```powershell
.\scripts\experiment.ps1 -Status -Name "模型A实验01"
```

只有明确需要产生一次新尝试时才使用 `-Retry`：

```powershell
.\scripts\experiment.ps1 -Run `
  -Name "模型A实验01" `
  -Projects "项目A" `
  -Retry
```

重跑会再次调用模型并产生费用。同一项目有多个尝试时，合并阶段必须使用 `-Choose`。如果只是“待补清单”且尚未开始运行，不要添加 `-Retry`，补齐清单后重新提交原名单即可。

鉴权失败、余额不足、模型不存在或请求参数被服务端拒绝时，当前批次会停止后续项目。暂时性网络错误仍按代理已有逻辑有限重试。

## 脚本说明

|脚本|定位|建议用途|
|---|---|---|
|`scripts/setup_windows.ps1`|安装入口|构建镜像并准备默认数据卷|
|`scripts/experiment.ps1`|推荐实验入口|创建实验、分批运行、状态、停止、重跑、清单校验和自主合并|
|`scripts/run_metrics.ps1`|底层单次测量入口|调试一个独立批次；调用者自行管理实验条件和输出|
|`scripts/run_project_list.ps1`|旧版项目列表入口|兼容早期自动分批和合并流程；新实验优先使用 `experiment.ps1`|
|`scripts/merge_metrics.ps1`|旧版离线合并入口|查看旧格式结果；不要与新版实验快照混用|
|`scripts/run_single.ps1`|旧版单项目翻译入口|调试原代理；不作为当前正式指标入口|
|`scripts/export_run.ps1`|旧数据卷导出|导出旧入口生成的数据或结果；新版实验报告已直接写到Windows|

底层 `run_metrics.ps1` 示例：

```powershell
.\scripts\run_metrics.ps1 `
  -ProjectName "项目A" `
  -ModelName "模型名" `
  -BaseUrl "https://api.example.com" `
  -MaxIterations 20
```

正式实验不建议混用底层入口，因为 `experiment.ps1` 额外负责固定配置、项目尝试身份、数据指纹、不可变快照和冲突选择。

## 常见问题

### 检查阶段显示“待补清单”

这不是模型失败，也没有调用API。根据[测试清单](#测试清单)章节补全JSON，然后用同一实验名、原项目名单和 `-InventoryFile` 重新运行。

### 编译失败后测试用例是否还会运行

最终评测会先执行 `test-compile`，随后仍尝试执行 `mvn test`。单模块项目编译失败时通常不会产生测试报告，此时规定用例全部记为“因编译失败未运行”。多模块工程已经编译的模块可能仍产生部分测试证据，但CR仍为0。

### 为什么测试通过数是0/0或未知

- 没有权威测试清单；
- 最终测试没有生成可解析的JUnit XML；
- 测试类名或方法名与清单不一致；
- 编译、依赖或工具结果无法明确分类；
- 测试文件指纹变化。

查看 `问题说明`、`compile.log`、`test.log`、`evaluation.json` 和 `inventory.json`。

### 为什么token显示未知

至少一个模型响应没有有效usage。查看“缺少usage的请求数”和“已知总token”。这通常由服务商响应格式、超时或中断造成，不代表请求没有消耗token。

### 为什么项目总时间不等于翻译时间加评测时间

项目总时间还包含Docker、清单与指纹、日志、报告和压缩等开销，详见[耗时统计](#耗时统计)。

### 请求很慢或频繁超时

区分三种超时：

- 单次模型HTTP请求当前为180秒；
- `AgentTimeoutSeconds`限制一个项目的完整代理阶段，默认3600秒；
- `EvaluationTimeoutSeconds`分别用于最终Maven命令，默认600秒。

把代理超时调大不能让单次模型请求更快，只会允许项目等待更久。检查服务商负载、模型推理速度、代理或VPN、网络线路、上下文长度以及服务商并发限制。

### 达到最大迭代次数是否等于运行环境错误

不等于。它表示代理在预算内没有主动完成，生成内容、日志、token和最终评测仍会保存。最终指标以独立评测为准。

### Maven首次运行很慢

首次需要下载插件和依赖。成功下载后会保存在 `rtb_maven_cache_v1`。网络错误可重新运行，但正式实验的重跑会产生新的模型费用，应先确认是依赖问题还是模型过程已经执行。

### Docker提示“What's next”

这是Docker Desktop附加提示，不等于容器失败。应查看其前面的退出码、工具日志和实验状态。

### 可以把不同模型的批次合并吗

不能作为同一正式实验合并。不同模型必须使用不同实验名，分别汇总，最后在外部表格中对比。

## 项目来源

- 官方仓库：[DeepSoftwareAnalytics/RepoTransBench](https://github.com/DeepSoftwareAnalytics/RepoTransBench)
- 论文：[RepoTransBench: A Real-World Benchmark for Repository-Level Code Translation](https://arxiv.org/abs/2412.17744)

本仓库增加了Windows/Docker运行封装、OpenAI兼容模型接口、权威测试清单、最终隔离评测、SR/CR/APR/AMPR、测试用例级证据、token/耗时统计以及可审计的分批合并流程。
