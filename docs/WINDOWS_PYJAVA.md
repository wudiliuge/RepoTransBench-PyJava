# Windows Python→Java 命令速查

完整说明请阅读仓库根目录的 [README.md](../README.md)。

## 新电脑首次使用

安装 Git、WSL 2 和 Docker Desktop，启动 Docker Desktop 后执行：

```powershell
git clone https://github.com/wudiliuge/RepoTransBench-PyJava.git
Set-Location ".\RepoTransBench-PyJava"
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\setup_windows.ps1
```

初始化成功标志：

```text
Manifest runnable tasks: 169
Python source projects : 169
Python->Java targets   : 169
SETUP_VERIFICATION_OK
Setup completed successfully.
```

## 运行一个项目

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "OilerNetwork_fossil_cairo0" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20
```

看到 `Enter DeepSeek API Key` 后输入真实 Key 并按 Enter。不要把 Key 写入仓库文件。

更换项目时只修改 `ProjectName`，例如：

```powershell
.\scripts\run_single.ps1 `
  -ProjectName "realpython_codetiming" `
  -ModelName "deepseek-v4-flash" `
  -MaxIterations 20
```

## 导出全部结果

```powershell
.\scripts\export_run.ps1 -Mode Results
```

默认输出到仓库同级的：

```text
RepoTransBench-exports\results_时间戳\
```

其中：

- `generated-results` 保存生成的 Java 项目；
- `run-records` 保存逐轮日志和最终摘要。

## 导出全部数据

```powershell
.\scripts\export_run.ps1 -Mode Dataset
```

## 常用检查

```powershell
git --version
wsl --status
docker version
docker info
docker volume ls
```

如果镜像或数据不存在，重新运行：

```powershell
.\scripts\setup_windows.ps1
```

如果运行结果是 `Exit code 2`，表示达到最大迭代轮数，结果仍然已经保存，不代表 Docker 出错。
