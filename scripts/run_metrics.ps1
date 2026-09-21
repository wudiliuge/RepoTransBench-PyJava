[CmdletBinding(DefaultParameterSetName = 'Selected')]
param(
    [Parameter(Mandatory = $true, ParameterSetName = 'Selected')]
    [ValidateNotNullOrEmpty()][string[]]$ProjectName,
    [Parameter(Mandatory = $true, ParameterSetName = 'All')][switch]$All,
    [Parameter(Mandatory = $true)][ValidateNotNullOrEmpty()][string]$ModelName,
    [string]$BaseUrl = 'https://api.deepseek.com',
    [string]$ChatUrl,
    [string]$ModelOptionsFile,
    [string]$ExperimentMetadata,
    [switch]$ProbeOnly,
    [ValidateRange(1,100)][int]$MaxIterations = 20,
    [ValidateRange(1,86400)][int]$AgentTimeoutSeconds = 3600,
    [ValidateRange(1,86400)][int]$EvaluationTimeoutSeconds = 600,
    [string]$ExpectedInventory,
    [switch]$CheckOnly,
    [switch]$AllowUnknownInventory,
    [switch]$SkipProjectArchive,
    [switch]$StopOnApiError,
    [string]$Destination,
    [string]$ImageName = 'repotransbench-pyjava:local',
    [string]$DataVolume = 'rtb_pyjava_data_v1',
    [string]$ResultsVolume = 'rtb_pyjava_results_v1',
    [string]$MavenVolume = 'rtb_maven_cache_v1'
)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function Invoke-Docker {
    param([string[]]$DockerArguments)
    $savedPreference = $ErrorActionPreference
    try {
        $ErrorActionPreference = 'Continue'
        & $script:dockerCli @DockerArguments
        $script:dockerExitCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $savedPreference }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
$dockerCommand = Get-Command docker.exe -ErrorAction SilentlyContinue
if (-not $dockerCommand) { throw 'Docker CLI not found. Start Docker Desktop and reopen PowerShell.' }
$script:dockerCli = $dockerCommand.Source
Invoke-Docker -DockerArguments @('info','--format','{{.OSType}}')
if ($script:dockerExitCode -ne 0) { throw 'Docker Linux engine is unavailable.' }
Invoke-Docker -DockerArguments @('image','inspect',$ImageName,'--format','{{.Id}}')
if ($script:dockerExitCode -ne 0) { throw 'Run scripts/setup_windows.ps1 first to create the image.' }
Invoke-Docker -DockerArguments @('volume','inspect',$DataVolume,'--format','{{.Name}}')
if ($script:dockerExitCode -ne 0) { throw 'Run scripts/setup_windows.ps1 first to prepare the dataset.' }
foreach ($volume in @($ResultsVolume,$MavenVolume)) {
    Invoke-Docker -DockerArguments @('volume','create',$volume)
    if ($script:dockerExitCode -ne 0) { throw "Unable to prepare volume: $volume" }
}
if (-not $ChatUrl -and $BaseUrl -match '/v1/?$|/chat/completions/?$') {
    throw 'BaseUrl must omit /v1 and /chat/completions; the agent appends /v1/chat/completions.'
}
$batchId = 'metrics_' + (Get-Date -Format 'yyyyMMdd_HHmmss') + '_' + ([guid]::NewGuid().ToString('N').Substring(0,8))
if ([string]::IsNullOrWhiteSpace($Destination)) {
    $Destination = Join-Path (Join-Path (Split-Path $repoRoot -Parent) 'RepoTransBench-exports') $batchId
}
$destinationPath = [System.IO.Path]::GetFullPath($Destination)
foreach ($path in @($repoRoot,$destinationPath)) {
    if ($path.Contains(',')) { throw 'Docker mount paths must not contain commas.' }
}
if (Test-Path -LiteralPath $destinationPath) {
    if (-not (Test-Path -LiteralPath $destinationPath -PathType Container)) { throw 'Destination must be a directory.' }
    if (Get-ChildItem -LiteralPath $destinationPath -Force | Where-Object {$_.Name -ne 'client_process.json'} | Select-Object -First 1) { throw 'Destination must be empty; use a new directory per experiment.' }
}
else { New-Item -ItemType Directory -Path $destinationPath -Force | Out-Null }

$configuration = @{
    batch_id=$batchId; model=$ModelName; projects=@(); max_iterations=$MaxIterations;
    agent_timeout=$AgentTimeoutSeconds; evaluation_timeout=$EvaluationTimeoutSeconds;
    check_only=[bool]$CheckOnly; allow_unknown_inventory=[bool]$AllowUnknownInventory;
    skip_project_archive=[bool]$SkipProjectArchive
    base_url=$BaseUrl; data_volume=$DataVolume; image_name=$ImageName
    stop_on_api_error=[bool]$StopOnApiError
    chat_url=$ChatUrl
}
if ($ModelOptionsFile) {$configuration.request_options=Get-Content -LiteralPath $ModelOptionsFile -Raw -Encoding UTF8 | ConvertFrom-Json}
if ($ExperimentMetadata) {$configuration.experiment_metadata=Get-Content -LiteralPath $ExperimentMetadata -Raw -Encoding UTF8 | ConvertFrom-Json}
if (-not $All) { $configuration.projects = @($ProjectName) }
$dockerArgs = @('run','--rm','-i','--cidfile',(Join-Path $destinationPath 'docker.cid'),
    '--env','LLM_API_KEY', '--env',"LLM_BASE_URL=$BaseUrl",
    '--env','PYTHONPATH=/methods', '--env','PYTHONPYCACHEPREFIX=/tmp/pycache',
    '--env','MAVEN_OPTS=-Dmaven.wagon.http.retryHandler.count=5 -Dhttps.protocols=TLSv1.2',
    '--mount',"type=bind,source=$repoRoot,target=/methods,readonly",
    '--mount',"type=bind,source=$destinationPath,target=/metrics-output",
    '--mount',"type=volume,source=$DataVolume,target=/data,readonly",
    '--mount',"type=volume,source=$ResultsVolume,target=/results",
    '--mount',"type=volume,source=$MavenVolume,target=/root/.m2",
    '--workdir','/metrics-output')
if ($ExpectedInventory) {
    $inventoryPath = (Resolve-Path -LiteralPath $ExpectedInventory).Path
    if ($inventoryPath.Contains(',')) { throw 'Inventory path must not contain commas.' }
    $dockerArgs += @('--mount',"type=bind,source=$inventoryPath,target=/expected-inventory.json,readonly")
    $configuration.inventory_file = '/expected-inventory.json'
}
$runnerModule='rtb_metrics.runner'
if ($ProbeOnly) {$runnerModule='rtb_metrics.probe'}
$dockerArgs += @($ImageName,'python','-m',$runnerModule)
$clearKey = $false
if (-not $CheckOnly -and [string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {
    $secureKey = Read-Host 'Enter model API key (local Ollama: enter ollama)' -AsSecureString
    $env:LLM_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
    if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) { throw 'API key must not be empty.' }
    $clearKey = $true
}
# Starts after interactive key entry and prerequisite checks. Includes Docker startup and final report output.
$watch = [System.Diagnostics.Stopwatch]::StartNew()
$startedAt = [DateTime]::UtcNow.ToString('o')
$runCode = 4
try {
    Write-Host "Batch: $batchId"
    Write-Host "Reports: $destinationPath"
    Write-Host 'Tasks run sequentially. Per-task progress is in tasks/<project>/agent.log.'
    $savedPreference = $ErrorActionPreference
    $savedEncoding = $OutputEncoding
    try {
        $ErrorActionPreference = 'Continue'
        $OutputEncoding = New-Object System.Text.UTF8Encoding($false)
        $configuration | ConvertTo-Json -Depth 10 -Compress | & $script:dockerCli @dockerArgs
        $runCode = $LASTEXITCODE
    }
    finally { $ErrorActionPreference = $savedPreference; $OutputEncoding = $savedEncoding }
}
finally {
    $watch.Stop()
    if ($clearKey) { Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue }
    $timing = @{
        batch_id=$batchId; started_at=$startedAt; ended_at=[DateTime]::UtcNow.ToString('o');
        total_elapsed_seconds=$watch.Elapsed.TotalSeconds; docker_exit_code=$runCode;
        scope='Measured run: Docker startup, all selected tasks, retries, final evaluation, reports and generated-project archive unless skipped. Excludes prerequisite checks, key entry, setup and model download.'
    }
    $timing | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $destinationPath 'launcher_timing.json') -Encoding UTF8
    $summaryPath = Join-Path $destinationPath 'summary.json'
    if (Test-Path -LiteralPath $summaryPath) {
        $summary = Get-Content -LiteralPath $summaryPath -Raw -Encoding UTF8 | ConvertFrom-Json
        $summary | Add-Member -NotePropertyName total_elapsed_seconds -NotePropertyValue $watch.Elapsed.TotalSeconds -Force
        $summary | Add-Member -NotePropertyName total_elapsed_scope -NotePropertyValue $timing.scope -Force
        $summary | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $summaryPath -Encoding UTF8
        $textPath = Join-Path $destinationPath 'summary.txt'
        $reportText = Get-Content -LiteralPath $textPath -Raw -Encoding UTF8
        $reportText = [regex]::Replace($reportText,'(?m)^Total measured workflow seconds \(host\):[^\r\n]*',("Total measured workflow seconds (host): " + $watch.Elapsed.TotalSeconds))
        Set-Content -LiteralPath $textPath -Value $reportText -Encoding UTF8
    }
}
Write-Host "Total elapsed: $($watch.Elapsed.TotalSeconds) seconds"
Write-Host "Report directory: $destinationPath"
Write-Host "Generated projects volume: $ResultsVolume/$batchId/generated"
if ($runCode -ne 0) { Write-Warning "Measured run ended with code $runCode. Inspect partial results and logs." }
$global:LASTEXITCODE = $runCode
