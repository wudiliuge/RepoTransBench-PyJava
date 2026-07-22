[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateNotNullOrEmpty()]
    [string]$ProjectName,

    [ValidateNotNullOrEmpty()]
    [string]$ModelName = "deepseek-v4-flash",

    [ValidateRange(1, 100)]
    [int]$MaxIterations = 20,

    [ValidateNotNullOrEmpty()]
    [string]$BaseUrl = "https://api.deepseek.com",

    [string]$ImageName = "repotransbench-pyjava:local",
    [string]$DataVolume = "rtb_pyjava_data_v1",
    [string]$ResultsVolume = "rtb_pyjava_results_v1",
    [string]$RunVolume = "rtb_pyjava_run_v1",
    [string]$MavenVolume = "rtb_maven_cache_v1",

    [switch]$SkipImageBuild
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Invoke-NativeCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$FilePath,

        [string[]]$Arguments = @(),

        [AllowNull()]
        [string]$InputText = $null,

        [switch]$Quiet
    )

    $previousPreference = $ErrorActionPreference
    try {
        # Windows PowerShell 5.1 converts any native stderr output into an
        # ErrorRecord. Docker emits harmless warnings on stderr, so native
        # success must be decided by the process exit code instead.
        $ErrorActionPreference = "Continue"

        if ($null -ne $InputText) {
            if ($Quiet) {
                $InputText | & $FilePath @Arguments *> $null
            }
            else {
                $InputText | & $FilePath @Arguments
            }
        }
        elseif ($Quiet) {
            & $FilePath @Arguments *> $null
        }
        else {
            & $FilePath @Arguments
        }

        $script:LastNativeExitCode = $LASTEXITCODE
    }
    finally {
        $ErrorActionPreference = $previousPreference
    }
}

function Resolve-DockerCli {
    $command = Get-Command docker.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidates = @(
        "D:\Docker\DockerDesktop\resources\bin\docker.exe",
        (Join-Path $env:ProgramFiles "Docker\Docker\resources\bin\docker.exe")
    )

    foreach ($candidate in $candidates) {
        if ($candidate -and (Test-Path -LiteralPath $candidate)) {
            return $candidate
        }
    }

    throw "Docker CLI was not found. Install or start Docker Desktop, then reopen PowerShell."
}

function Test-DockerImage {
    param([string]$DockerCli, [string]$Name)

    Invoke-NativeCommand `
        -FilePath $DockerCli `
        -Arguments @("image", "inspect", $Name) `
        -Quiet
    return ($script:LastNativeExitCode -eq 0)
}

function Test-DockerVolume {
    param([string]$DockerCli, [string]$Name)

    Invoke-NativeCommand `
        -FilePath $DockerCli `
        -Arguments @("volume", "inspect", $Name) `
        -Quiet
    return ($script:LastNativeExitCode -eq 0)
}

function Ensure-DockerVolume {
    param([string]$DockerCli, [string]$Name)

    if (-not (Test-DockerVolume -DockerCli $DockerCli -Name $Name)) {
        Invoke-NativeCommand `
            -FilePath $DockerCli `
            -Arguments @("volume", "create", $Name) `
            -Quiet
        if ($script:LastNativeExitCode -ne 0) {
            throw "Failed to create Docker volume: $Name"
        }
        Write-Host "Created Docker volume: $Name"
    }
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dockerCli = Resolve-DockerCli
$dockerfile = Join-Path $repoRoot "Dockerfile.pyjava"

Write-Host "Repo root : $repoRoot"
Write-Host "Docker CLI: $dockerCli"

Invoke-NativeCommand -FilePath $dockerCli -Arguments @("info") -Quiet
if ($script:LastNativeExitCode -ne 0) {
    throw "Docker Desktop is not running or its Linux engine is unavailable."
}

if (-not (Test-DockerImage -DockerCli $dockerCli -Name $ImageName)) {
    if ($SkipImageBuild) {
        throw "Docker image '$ImageName' is missing. Run scripts\setup_windows.ps1 first."
    }

    if (-not (Test-Path -LiteralPath $dockerfile)) {
        throw "Dockerfile not found: $dockerfile"
    }

    Write-Host "Docker image is missing; building $ImageName ..."
    $buildArgs = @(
        "build",
        "--progress=plain",
        "--pull=false",
        "-f", $dockerfile,
        "-t", $ImageName,
        $repoRoot
    )
    Invoke-NativeCommand -FilePath $dockerCli -Arguments $buildArgs

    if ($script:LastNativeExitCode -ne 0) {
        throw "Docker image build failed."
    }
}

if (-not (Test-DockerVolume -DockerCli $dockerCli -Name $DataVolume)) {
    throw "Dataset volume '$DataVolume' is missing. Run scripts\setup_windows.ps1 first."
}

Ensure-DockerVolume -DockerCli $dockerCli -Name $ResultsVolume
Ensure-DockerVolume -DockerCli $dockerCli -Name $RunVolume
Ensure-DockerVolume -DockerCli $dockerCli -Name $MavenVolume

$projectCheck = @'
from pathlib import Path
import os

project = os.environ["PROJECT_NAME"]
source = Path("/data/source_projects/Python") / project
target = Path("/data/target_projects/Python/Java") / project

if not source.is_dir():
    raise SystemExit(f"Missing Python source project: {source}")
if not target.is_dir():
    raise SystemExit(f"Missing Java target project: {target}")

print(f"PROJECT_DATA_OK: {project}")
'@

$checkArgs = @(
    "run", "--rm", "-i",
    "--env", "PROJECT_NAME=$ProjectName",
    "--mount", "type=volume,source=$DataVolume,target=/data,readonly",
    $ImageName,
    "python", "-"
)

Invoke-NativeCommand `
    -FilePath $dockerCli `
    -Arguments $checkArgs `
    -InputText $projectCheck
if ($script:LastNativeExitCode -ne 0) {
    throw "Project validation failed before any API request was made."
}

$clearApiKeyAfterRun = $false
if ([string]::IsNullOrWhiteSpace($env:LLM_API_KEY)) {
    $secureKey = Read-Host "Enter DeepSeek API Key" -AsSecureString
    $plainKey = [System.Net.NetworkCredential]::new("", $secureKey).Password
    if ([string]::IsNullOrWhiteSpace($plainKey)) {
        throw "The API key is empty."
    }

    $env:LLM_API_KEY = $plainKey
    $plainKey = $null
    $clearApiKeyAfterRun = $true
}

$launcher = @'
set -eu

test -d "/data/source_projects/Python/$PROJECT_NAME"
test -d "/data/target_projects/Python/Java/$PROJECT_NAME"

mkdir -p /workspace
ln -sfn /data/source_projects /workspace/source_projects
ln -sfn /data/target_projects /workspace/target_projects
ln -sfn /results /workspace/translated_projects

echo "============================================================"
echo "RepoTransBench single-project run"
echo "Project        : $PROJECT_NAME"
echo "Model          : $MODEL_NAME"
echo "Max iterations : $MAX_ITERATIONS"
echo "============================================================"

exec python -m RepoTransAgent.run \
    --project_name "$PROJECT_NAME" \
    --source_language "Python" \
    --target_language "Java" \
    --model_name "$MODEL_NAME" \
    --max_iterations "$MAX_ITERATIONS"
'@

$launcher = $launcher.Replace("`r", "") + "`n"

$runArgs = @(
    "run", "--rm", "-i",
    "--env", "LLM_API_KEY",
    "--env", "LLM_BASE_URL=$BaseUrl",
    "--env", "PROJECT_NAME=$ProjectName",
    "--env", "MODEL_NAME=$ModelName",
    "--env", "MAX_ITERATIONS=$MaxIterations",
    "--env", "PYTHONPATH=/methods",
    "--env", "PYTHONPYCACHEPREFIX=/tmp/pycache",
    "--env", "MAVEN_OPTS=-Dmaven.wagon.http.retryHandler.count=5 -Dhttps.protocols=TLSv1.2",
    "--mount", "type=volume,source=$DataVolume,target=/data,readonly",
    "--mount", "type=volume,source=$ResultsVolume,target=/results",
    "--mount", "type=volume,source=$RunVolume,target=/experiment",
    "--mount", "type=volume,source=$MavenVolume,target=/root/.m2",
    "--mount", "type=bind,source=$repoRoot,target=/methods,readonly",
    "--workdir", "/experiment",
    $ImageName,
    "sh"
)

$runExitCode = 4
try {
    Invoke-NativeCommand `
        -FilePath $dockerCli `
        -Arguments $runArgs `
        -InputText $launcher
    $runExitCode = $script:LastNativeExitCode
}
finally {
    if ($clearApiKeyAfterRun) {
        $env:LLM_API_KEY = $null
        Remove-Variable secureKey -ErrorAction SilentlyContinue
    }
}

$statusText = switch ($runExitCode) {
    0 { "success" }
    1 { "agent reported failure" }
    2 { "maximum iterations reached" }
    3 { "interrupted" }
    4 { "runtime/infrastructure error" }
    default { "unexpected exit code" }
}

Write-Host ""
Write-Host "Run finished"
Write-Host "Project        : $ProjectName"
Write-Host "Model          : $ModelName"
Write-Host "Status         : $statusText"
Write-Host "Exit code      : $runExitCode"
Write-Host "Results volume : $ResultsVolume"
Write-Host "Logs volume    : $RunVolume"

$global:LASTEXITCODE = $runExitCode
