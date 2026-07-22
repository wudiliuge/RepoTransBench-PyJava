# RepoTransBench-PyJava：Windows 运行指南

这是 [RepoTransBench](https://github.com/DeepSoftwareAnalytics/RepoTransBench) 的 Windows + Docker + Python→Java 运行版本。

本仓库已经把数据下载、Linux 解压、Docker 镜像构建、单项目运行和结果导出封装成 PowerShell 脚本。使用者不需要手工输入很长的 `docker run` 命令，也不需要进入 Ubuntu 操作。

## 最快运行方式

新电脑安装 Git、WSL 2 和 Docker Desktop 后，在 PowerShell 中执行：

```powershell
git clone https://github.com/wudiliuge/RepoTransBench-PyJava.git
Set-Location ".\RepoTransBench-PyJava"

Set-ExecutionPolicy -Scope Process Bypass

# 第一次使用：构建环境并准备数据
.\scripts\setup_windows.ps1

# 运行一个 Python→Java 项目
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20

# 将所有生成项目和日志导出到 Windows
.\scripts\export_run.ps1 -Mode Results
```

运行时出现 `Enter DeepSeek API Key` 后，直接输入真实的 DeepSeek API Key 并按 Enter。输入内容不会显示在屏幕上。

下面是完整操作说明。

## 1. 这个仓库能做什么

RepoTransBench 是一个仓库级代码翻译基准。本仓库当前专门运行其中的 Python→Java 部分：

- 输入：Python 源项目；
- 目标环境：带有 Java 构建配置和测试的目标工程；
- 翻译工具：RepoTransAgent；
- 模型：默认使用 DeepSeek 的 `deepseek-v4-flash`；
- 输出：模型生成或修改后的 Java 项目、逐轮运行日志和最终摘要。

官方元数据包含 171 条 Python→Java 记录。其中 2 条缺少 Java 目标目录，因此脚本会自动准备 169 个可运行项目。

## 2. 仓库中与运行有关的文件

```text
RepoTransBench-PyJava/
├── Dockerfile.pyjava
├── README.md
├── RepoTransAgent/
│   ├── run.py
│   ├── generator.py
│   ├── test_analyzer.py
│   └── ...
└── scripts/
    ├── setup_windows.ps1
    ├── run_single.ps1
    └── export_run.ps1
```

各文件作用：

- `Dockerfile.pyjava`：安装 Python、OpenJDK 17、Maven 和其他依赖；
- `scripts/setup_windows.ps1`：首次准备 Docker 镜像和 Python→Java 数据；
- `scripts/run_single.ps1`：运行一个指定项目；
- `scripts/export_run.ps1`：把 Docker 中的数据或结果导出到 Windows；
- `RepoTransAgent/`：论文提供的翻译代理代码及本仓库的运行兼容修改。

## 3. Windows 环境要求

支持 Windows 10 或 Windows 11。需要安装：

1. Git for Windows；
2. WSL 2；
3. Docker Desktop；
4. PowerShell 5.1 或 PowerShell 7。

Windows 本机不要求额外安装 Java、Maven 或指定版本的 Python，因为这些依赖都安装在 Docker 镜像中。

### 3.1 检查 Git

```powershell
git --version
```

应显示 Git 版本号。

### 3.2 检查 WSL 2

```powershell
wsl --status
```

默认版本应为 2。

### 3.3 检查 Docker Desktop

先启动 Docker Desktop，等待状态变为 Running，然后执行：

```powershell
docker version
docker info
```

`docker version` 应同时显示 `Client` 和 `Server`，并且 Server 应为 Linux。`docker info` 中的 blkio 或 cgroup 警告通常不影响运行。

建议 Docker Desktop 至少分配 8 GB 内存。资源不足会让 Maven 构建或模型运行明显变慢。

## 4. 下载仓库

选择一个用于存放项目的目录，例如：

```powershell
New-Item -ItemType Directory -Path "D:\archtrans" -Force | Out-Null
Set-Location "D:\archtrans"

git clone https://github.com/wudiliuge/RepoTransBench-PyJava.git
Set-Location ".\RepoTransBench-PyJava"
```

如果已经下载过仓库，只需进入仓库根目录：

```powershell
Set-Location "D:\archtrans\RepoTransBench-PyJava"
```

建议仓库路径只使用常规中英文字符，不要包含逗号。Docker 的 `--mount` 参数会把逗号识别为分隔符。

## 5. 允许当前 PowerShell 运行脚本

如果系统阻止执行 `.ps1`，在当前 PowerShell 窗口运行：

```powershell
Set-ExecutionPolicy -Scope Process Bypass
```

该设置只对当前 PowerShell 进程有效，关闭窗口后自动失效，不需要管理员权限。

也可以始终使用下面的形式执行脚本：

```powershell
powershell -ExecutionPolicy Bypass -File ".\scripts\setup_windows.ps1"
```

## 6. 首次初始化

确保 Docker Desktop 正在运行，然后在仓库根目录执行：

```powershell
.\scripts\setup_windows.ps1
```

脚本会自动完成：

1. 检查 Docker Desktop 和 Linux 引擎；
2. 根据 `Dockerfile.pyjava` 构建 `repotransbench-pyjava:local` 镜像；
3. 创建 Docker 数据卷；
4. 从官方 GitHub Release 下载或断点续传数据集；
5. 在 Linux 容器中验证并解压数据集；
6. 提取 169 个可运行的 Python→Java 项目；
7. 验证源项目、目标项目和任务清单。

第一次运行需要下载 Docker 基础镜像、Debian 软件包和约 375 MB 的数据集，可能耗时较长。只要下载进度仍在变化，通常就是正常的。

成功结束时会看到：

```text
Manifest runnable tasks: 169
Python source projects : 169
Python->Java targets   : 169
SETUP_VERIFICATION_OK

Setup completed successfully.
```

以后通常不需要再次初始化。脚本检测到镜像和数据已经存在时会自动复用。

### 6.1 网络中断怎么办

重新运行同一命令：

```powershell
.\scripts\setup_windows.ps1
```

数据集下载使用断点续传，不需要手工删除已经下载的部分。

### 6.2 强制重新构建镜像

修改 Dockerfile 或依赖后，可以运行：

```powershell
.\scripts\setup_windows.ps1 -RebuildImage
```

## 7. 准备 DeepSeek API Key

请先在 DeepSeek 开放平台申请 API Key。

最简单的使用方式是不把 Key 写入任何文件。运行项目时，脚本会提示：

```text
Enter DeepSeek API Key:
```

直接输入真实 Key 并按 Enter。输入时屏幕没有任何字符或星号也属于正常现象。

不要把真实 API Key 写入：

- `README.md`；
- PowerShell 脚本；
- `.env`；
- 仓库里的任何 `API_KEY.txt`；
- Git commit。

如果希望在当前 PowerShell 窗口重复运行多个项目时不反复输入，可以临时设置：

```powershell
$env:LLM_API_KEY = "你的真实 DeepSeek API Key"
```

完成实验后清除：

```powershell
Remove-Item Env:LLM_API_KEY
```

## 8. 运行一个 Python→Java 项目

标准命令：

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20
```

参数说明：

| 参数 | 是否必需 | 默认值 | 说明 |
|---|---:|---|---|
| `ProjectName` | 是 | 无 | 要运行的 Python→Java 项目名 |
| `ModelName` | 否 | `deepseek-v4-flash` | 模型 API 中的模型名 |
| `MaxIterations` | 否 | `20` | 最大交互轮数，范围为 1–100 |
| `BaseUrl` | 否 | `https://api.deepseek.com` | OpenAI 兼容 API 地址 |

### 8.1 先运行一轮检查环境

第一次可以先使用一轮，验证数据挂载和 API 是否正常：

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 1
```

出现下面内容表示项目数据、Docker 和模型 API 已进入正常运行流程：

```text
PROJECT_DATA_OK: OilerNetwork_fossil_cairo0
RepoTransBench single-project run
Using base url: https://api.deepseek.com
```

一轮通常不足以完成翻译，因此最后显示 `maximum iterations reached` 属于预期现象。

### 8.2 正式运行 20 轮

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20
```

运行期间不要关闭 PowerShell 或 Docker Desktop。

### 8.3 换一个项目

只需要修改 `ProjectName`：

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "realpython_codetiming" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20
```

也可以尝试：

```text
wbolster_jsonlines
skorokithakis_shortuuid
rgri_tex2nix
```

## 9. 如何查看可运行项目名

初始化完成后执行：

```powershell
docker run --rm `
  --mount "type=volume,source=rtb_pyjava_data_v1,target=/data,readonly" `
  alpine:3.22 `
  sh -c "find /data/target_projects/Python/Java -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | sort"
```

输出的每一行都是可以传给 `-ProjectName` 的项目名。

## 10. 理解运行结束状态

脚本结束时会显示：

```text
Run finished
Project        : OilerNetwork_fossil_cairo0
Model          : deepseek-v4-flash
Status         : ...
Exit code      : ...
Results volume : rtb_pyjava_results_v1
Logs volume    : rtb_pyjava_run_v1
```

退出码含义：

| 退出码 | 含义 |
|---:|---|
| 0 | 代理主动完成 |
| 1 | 代理报告失败 |
| 2 | 已达到最大轮数，结果仍然保存，并不表示 Docker 出错 |
| 3 | 运行被中断 |
| 4 | Docker、挂载或其他运行环境错误 |

如果结果显示 `Compilation: FAILED` 和 `0/0`，通常表示 Java 项目在测试开始前就编译失败，不一定表示目标工程没有测试。先导出结果，再查看 `final_summary.txt` 和生成工程。

## 11. 数据和结果保存在哪里

默认情况下，大体积内容保存在 Docker named volumes 中：

| Docker 卷 | 保存内容 |
|---|---|
| `rtb_pyjava_raw_v1` | 官方完整数据集的 Linux 解压内容 |
| `rtb_pyjava_data_v1` | 169 个 Python→Java 任务 |
| `rtb_pyjava_results_v1` | 模型生成或修改后的 Java 项目 |
| `rtb_pyjava_run_v1` | system prompt、逐轮交互和最终摘要 |
| `rtb_maven_cache_v1` | Maven 下载缓存 |

关闭 PowerShell 或删除一次性容器不会删除这些数据卷。可以使用下面命令查看：

```powershell
docker volume ls
```

## 12. 把全部结果导出到 Windows

运行：

```powershell
.\scripts\export_run.ps1 -Mode Results
```

成功后会显示实际导出目录，默认位于仓库同级目录，例如：

```text
D:\archtrans\RepoTransBench-exports\results_20260722_120000\
```

目录结构：

```text
results_时间戳/
├── generated-results/
├── run-records/
├── generated_result_files.txt
├── run_record_files.txt
└── export_manifest.txt
```

- `generated-results`：模型生成或修改后的 Java 工程；
- `run-records`：每次运行的 system prompt、每轮日志和最终摘要；
- `export_manifest.txt`：导出时间、Docker 卷名称和文件统计。

生成的 Java 业务代码通常位于：

```text
generated-results\模型名\Python\Java\项目名\src\main\java\
```

最终摘要通常位于：

```text
run-records\logs\模型名\项目名_Python_to_Java_时间戳\final_summary.txt
```

如果需要指定导出位置，目标目录必须为空：

```powershell
.\scripts\export_run.ps1 `
  -Mode Results `
  -Destination "D:\exports\RepoTransBench-results"
```

## 13. 把全部 Python→Java 数据导出到 Windows

运行：

```powershell
.\scripts\export_run.ps1 -Mode Dataset
```

默认输出类似：

```text
D:\archtrans\RepoTransBench-exports\dataset_20260722_120000\
```

其中包含：

```text
python-java-dataset.tar.gz
subset_manifest.json
projects_summary.jsonl
python_source_projects.txt
python_java_target_projects.txt
export_manifest.txt
```

完整项目保存在 `python-java-dataset.tar.gz` 中。官方数据里可能包含 Windows 不支持的文件名，因此不要直接使用 Windows `tar` 完整解压这个文件。日常运行不需要手工解压，Docker 数据卷已经包含可用数据。

## 14. 常见问题

### 14.1 PowerShell 找不到 docker

错误类似：

```text
docker : 无法将“docker”项识别为 cmdlet...
```

解决方法：

1. 安装并启动 Docker Desktop；
2. 等待 Docker Desktop 完全启动；
3. 关闭并重新打开 PowerShell；
4. 执行 `docker version`。

### 14.2 Docker 只有 Client，没有 Server

说明 Docker Desktop 或 Linux 引擎尚未正常运行。打开 Docker Desktop，等待其进入 Running 状态后重试。

### 14.3 镜像不存在

错误类似：

```text
No such image: repotransbench-pyjava:local
```

运行：

```powershell
.\scripts\setup_windows.ps1
```

如果需要完全重建：

```powershell
.\scripts\setup_windows.ps1 -RebuildImage
```

### 14.4 数据集下载失败

保持已经下载的文件，直接重新执行：

```powershell
.\scripts\setup_windows.ps1
```

脚本会继续下载并重新验证归档。

### 14.5 Maven 下载依赖很慢或出现 TLS 错误

首次运行需要下载 Maven 插件和依赖，可能耗时较长。临时网络错误时可以重新运行相同项目。下载成功的依赖会保存在 `rtb_maven_cache_v1` 中。

### 14.6 出现 Docker 的 “What's next”

这是 Docker Desktop 自动附加的提示，不等于程序一定出错。请查看前面的 RepoTransAgent 日志以及最后的 `Status` 和 `Exit code`。

### 14.7 找不到项目

确认项目名大小写和字符完全一致。使用“如何查看可运行项目名”中的命令列出全部 169 个项目。

## 15. 日常使用命令

环境已经初始化后，每次使用通常只需要：

```powershell
Set-Location "D:\archtrans\RepoTransBench-PyJava"
Set-ExecutionPolicy -Scope Process Bypass

.\scripts\run_single.ps1 `
  -ProjectName "项目名" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20

.\scripts\export_run.ps1 -Mode Results
```

## 16. 更新仓库代码

如果仓库后续有更新：

```powershell
Set-Location "D:\archtrans\RepoTransBench-PyJava"
git pull
```

如果 `Dockerfile.pyjava` 或初始化脚本发生变化，更新后运行：

```powershell
.\scripts\setup_windows.ps1 -RebuildImage
```

Docker 数据卷不会因为 `git pull` 自动删除。

## 17. 项目来源

本仓库基于：

- 官方仓库：[DeepSoftwareAnalytics/RepoTransBench](https://github.com/DeepSoftwareAnalytics/RepoTransBench)
- 论文：[RepoTransBench: A Real-World Benchmark for Repository-Level Code Translation](https://arxiv.org/abs/2412.17744)

本仓库主要增加 Windows、Docker Desktop、DeepSeek 和 Python→Java 子集的一键运行流程。
