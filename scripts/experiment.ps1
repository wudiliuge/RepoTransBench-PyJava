[CmdletBinding()]
param(
    [switch]$Init,[switch]$Run,[switch]$Status,[switch]$ListProjects,
    [switch]$Merge,[switch]$Probe,[switch]$ValidateInventory,[switch]$Stop,
    [string]$ImportBatch,
    [string]$Name,[string[]]$Projects,[string]$ProjectList,
    [string[]]$Sources,[hashtable]$Choose=@{},[string]$Note='',
    [string]$InventoryFile,[switch]$Retry,[switch]$CheckOnly,
    [string]$ModelName,[string]$BaseUrl,
    [ValidateSet('auto','root','full')][string]$EndpointKind='auto',
    [string]$ModelOptionsFile,
    [ValidateRange(1,100)][int]$MaxIterations=20,
    [ValidateRange(1,86400)][int]$AgentTimeoutSeconds=3600,
    [ValidateRange(1,86400)][int]$EvaluationTimeoutSeconds=600,
    [string]$PythonCommand='python'
)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$operations=@($Init,$Run,$Status,$ListProjects,$Merge,$Probe,$ValidateInventory,$Stop,([bool]$ImportBatch)) | Where-Object {$_}
if (@($operations).Count -ne 1) {Write-Host '请选择一个操作：创建 -Init、运行 -Run、查看 -Status、合并 -Merge、接口测试 -Probe、列出项目 -ListProjects、校验清单 -ValidateInventory、停止 -Stop、导入 -ImportBatch。';$global:LASTEXITCODE=1;return}
if ($ListProjects) {
    & docker run --rm --mount 'type=volume,source=rtb_pyjava_data_v1,target=/data,readonly' repotransbench-pyjava:local ls /data/target_projects/Python/Java
    if ($LASTEXITCODE -ne 0) {Write-Host '无法读取默认数据卷，请检查 Docker Desktop。'}
    return
}
if ([string]::IsNullOrWhiteSpace($Name) -or $Name -match '[\\/:*?"<>|\x00-\x1f]' -or $Name -in @('.','..') -or $Name.EndsWith('.') -or $Name.EndsWith(' ')) {Write-Host '请通过 -Name 填写有效实验名称，例如 模型A实验01。';$global:LASTEXITCODE=1;return}
$root=Join-Path (Join-Path (Join-Path (Split-Path $repo -Parent) 'RepoTransBench-exports') '实验') $Name
$tempFile=Join-Path ([IO.Path]::GetTempPath()) ('rtb-v2-'+[guid]::NewGuid().ToString('N')+'.json')
$responseFile=$tempFile+'.response'
$savedUtf8=$env:PYTHONUTF8;$savedIo=$env:PYTHONIOENCODING;$savedConsole=[Console]::OutputEncoding
$clearKey=$false;$returnCode=0
Push-Location $repo
try {
    if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {throw '未找到本机 Python，请安装 Python 3.10 或更高版本。'}
    $env:PYTHONUTF8='1';$env:PYTHONIOENCODING='utf-8';[Console]::OutputEncoding=New-Object System.Text.UTF8Encoding($false)
    $request=@{root=$root;repo=$repo}
    if ($InventoryFile) {$request.inventory_file=(Resolve-Path -LiteralPath $InventoryFile).Path}
    if ($Init) {
        if (-not $ModelName) {$ModelName=Read-Host '请输入服务端实际模型名称（不限品牌）'}
        if (-not $BaseUrl) {$BaseUrl=Read-Host '请输入 API 根地址、含 /v1 的地址，或完整 Chat Completions 地址'}
        $options=@{}
        if ($ModelOptionsFile) {$options=Get-Content -LiteralPath $ModelOptionsFile -Raw -Encoding UTF8 | ConvertFrom-Json}
        $request.action='init';$request.config=@{ModelName=$ModelName;BaseUrl=$BaseUrl;EndpointKind=$EndpointKind;RequestOptions=$options;MaxIterations=$MaxIterations;AgentTimeoutSeconds=$AgentTimeoutSeconds;EvaluationTimeoutSeconds=$EvaluationTimeoutSeconds}
    } elseif ($Status) {$request.action='status';$request.sources=@($Sources)}
    elseif ($Merge) {
        if (-not $Sources) {throw '请用 -Sources 指定批次或已有汇总，例如 "批次001","汇总001"。'}
        $request.action='merge';$request.sources=@($Sources);$request.choices=$Choose
    } elseif ($Probe) {$request.action='probe'}
    elseif ($Stop) {$request.action='stop'}
    elseif ($ImportBatch) {$request.action='import';$request.import_path=(Resolve-Path -LiteralPath $ImportBatch).Path}
    elseif ($ValidateInventory) {
        if (-not $InventoryFile) {throw '请添加 -InventoryFile 清单文件路径。'}
        $request.action='validate'
    } else {
        if ($ProjectList) {$Projects=@(Get-Content -LiteralPath $ProjectList -Encoding UTF8)}
        if (-not $Projects) {$Projects=@((Read-Host '请输入本批项目名，用英文逗号分隔') -split ',')}
        $Projects=@($Projects | ForEach-Object {$_.Trim()} | Where-Object {$_})
        $request.action='prepare';$request.projects=$Projects;$request.retry=[bool]$Retry;$request.note=$Note;$request.response_file=$responseFile
        $request | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $tempFile -Encoding UTF8
        & $PythonCommand -m rtb_metrics.experiment_v2 --request $tempFile
        if ($LASTEXITCODE -ne 0) {$returnCode=$LASTEXITCODE;return}
        $prepared=Get-Content -LiteralPath $responseFile -Raw -Encoding UTF8 | ConvertFrom-Json
        if ($CheckOnly -or -not $prepared.ready) {return}
        $request.action='execute';$request.batch=$prepared.batch
    }
    if (($Run -and -not $CheckOnly) -or $Probe) {
        if ($Probe) {Write-Host '这是一次真实短请求，可能产生少量模型费用，并记录到实验总消耗。'}
        if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {
            $secret=Read-Host '请输入服务商 API Key（本地无需认证的服务可输入 local；不保存到配置）' -AsSecureString
            $env:LLM_API_KEY=[Net.NetworkCredential]::new('',$secret).Password;$clearKey=$true
            if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {throw 'API Key 不能为空。'}
        }
    }
    $request | ConvertTo-Json -Depth 40 | Set-Content -LiteralPath $tempFile -Encoding UTF8
    & $PythonCommand -m rtb_metrics.experiment_v2 --request $tempFile
    $returnCode=$LASTEXITCODE
} catch {Write-Host ('操作未完成：'+$_.Exception.Message);$returnCode=1}
finally {
    if ($clearKey) {Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue}
    $env:PYTHONUTF8=$savedUtf8;$env:PYTHONIOENCODING=$savedIo;[Console]::OutputEncoding=$savedConsole
    Remove-Item -LiteralPath $tempFile,$responseFile -ErrorAction SilentlyContinue
    Pop-Location;$global:LASTEXITCODE=$returnCode
}
