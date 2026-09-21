[CmdletBinding(DefaultParameterSetName='Names')]
param(
    [Parameter(Mandatory=$true,ParameterSetName='Names')][string[]]$ProjectName,
    [Parameter(Mandatory=$true,ParameterSetName='File')][string]$ProjectList,
    [Parameter(Mandatory=$true)][string]$ModelName,
    [Parameter(Mandatory=$true)][string]$Destination,
    [ValidateRange(1,100000)][int]$BatchSize=100000,
    [string]$BaseUrl='https://api.deepseek.com',
    [ValidateRange(1,100)][int]$MaxIterations=20,
    [int]$AgentTimeoutSeconds=3600,
    [int]$EvaluationTimeoutSeconds=600,
    [string]$ExpectedInventory,
    [string]$DataVolume='rtb_pyjava_data_v1',
    [string]$ResultsVolume='rtb_pyjava_results_v1',
    [string]$MavenVolume='rtb_maven_cache_v1',
    [string]$ImageName='repotransbench-pyjava:local',
    [string]$PythonCommand='python',
    [switch]$CheckOnly
)
$ErrorActionPreference='Stop'
$repo=(Resolve-Path (Join-Path $PSScriptRoot '..')).Path
if (-not (Get-Command $PythonCommand -ErrorAction SilentlyContinue)) {throw 'Python 3.10+ is required locally. Set -PythonCommand to python.exe.'}
$destinationPath=[IO.Path]::GetFullPath($Destination)
if ($ExpectedInventory) {$ExpectedInventory=(Resolve-Path -LiteralPath $ExpectedInventory).Path}
$projects=@($ProjectName)
if ($ProjectList) {$projects=@(Get-Content -LiteralPath $ProjectList -Encoding UTF8)}
$projects=@($projects | ForEach-Object {$_.Trim()} | Where-Object {$_ -ne ''})
if ($projects.Count -eq 0) {throw 'Project list is empty.'}
$config=@{ModelName=$ModelName;BaseUrl=$BaseUrl;MaxIterations=$MaxIterations;AgentTimeoutSeconds=$AgentTimeoutSeconds;EvaluationTimeoutSeconds=$EvaluationTimeoutSeconds;DataVolume=$DataVolume;ResultsVolume=$ResultsVolume;MavenVolume=$MavenVolume;ImageName=$ImageName}
$tempDir=Join-Path ([IO.Path]::GetTempPath()) ('rtb-series-'+[guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null
$clearKey=$false
Push-Location $repo
try {
    $config | ConvertTo-Json | Set-Content (Join-Path $tempDir 'config.json') -Encoding UTF8
    $projects | Set-Content (Join-Path $tempDir 'projects.txt') -Encoding UTF8
    $argsList=@('-m','rtb_metrics.series','run','--projects',(Join-Path $tempDir 'projects.txt'),'--config',(Join-Path $tempDir 'config.json'),'--size',"$BatchSize",'--output',$destinationPath,'--repo',$repo)
    if ($ExpectedInventory) {$argsList+=@('--inventory',(Resolve-Path -LiteralPath $ExpectedInventory).Path)}
    & $PythonCommand @argsList --check-only
    if ($LASTEXITCODE -ne 0) {throw 'Project preflight failed.'}
    if (-not $CheckOnly) {
        if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {
            $secret=Read-Host 'Enter model API key' -AsSecureString
            $env:LLM_API_KEY=[Net.NetworkCredential]::new('',$secret).Password
            $clearKey=$true
            if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {throw 'API key cannot be empty.'}
        }
        & $PythonCommand @argsList
        if ($LASTEXITCODE -ne 0) {throw 'Series stopped; inspect retained batch logs.'}
        Write-Host "Combined reports: $destinationPath\combined"
    }
}
finally {
    if ($clearKey) {Remove-Item Env:LLM_API_KEY -ErrorAction SilentlyContinue}
    Pop-Location
    # Only the two files created above are removed; no recursive filesystem deletion.
    Remove-Item -LiteralPath (Join-Path $tempDir 'config.json'),(Join-Path $tempDir 'projects.txt') -ErrorAction SilentlyContinue
    Remove-Item -LiteralPath $tempDir -ErrorAction SilentlyContinue
}
