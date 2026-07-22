<#
.SYNOPSIS
Exports the RepoTransBench Python-to-Java dataset or all experiment results.

.EXAMPLE
.\scripts\export_run.ps1 -Mode Dataset

.EXAMPLE
.\scripts\export_run.ps1 -Mode Results

.EXAMPLE
.\scripts\export_run.ps1 -Mode Results -Destination "D:\exports\rtb-results"
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Dataset", "Results")]
    [string]$Mode,

    [string]$Destination,

    [string]$DataVolume = "rtb_pyjava_data_v1",
    [string]$ResultsVolume = "rtb_pyjava_results_v1",
    [string]$RunVolume = "rtb_pyjava_run_v1"
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
        # Windows PowerShell 5.1 turns native stderr into ErrorRecord objects.
        # Docker may write harmless warnings to stderr, so use its exit code.
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

function Test-DockerVolume {
    param(
        [Parameter(Mandatory = $true)]
        [string]$DockerCli,

        [Parameter(Mandatory = $true)]
        [string]$Name
    )

    Invoke-NativeCommand `
        -FilePath $DockerCli `
        -Arguments @("volume", "inspect", $Name) `
        -Quiet
    return ($script:LastNativeExitCode -eq 0)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dockerCli = Resolve-DockerCli

if ([string]::IsNullOrWhiteSpace($Destination)) {
    $timestamp = Get-Date -Format "yyyyMMdd_HHmmss"
    $exportRoot = Join-Path (Split-Path -Parent $repoRoot) "RepoTransBench-exports"
    $Destination = Join-Path $exportRoot ("{0}_{1}" -f $Mode.ToLowerInvariant(), $timestamp)
}

$destinationPath = [System.IO.Path]::GetFullPath($Destination)
if ($destinationPath.Contains(",")) {
    throw "The destination path cannot contain a comma because Docker --mount treats commas as separators."
}

Write-Host "Repo root   : $repoRoot"
Write-Host "Docker CLI  : $dockerCli"
Write-Host "Export mode : $Mode"
Write-Host "Destination : $destinationPath"

Invoke-NativeCommand -FilePath $dockerCli -Arguments @("info") -Quiet
if ($script:LastNativeExitCode -ne 0) {
    throw "Docker Desktop is not running or its Linux engine is unavailable."
}

$requiredVolumes = if ($Mode -eq "Dataset") {
    @($DataVolume)
}
else {
    @($ResultsVolume, $RunVolume)
}

foreach ($volume in $requiredVolumes) {
    if (-not (Test-DockerVolume -DockerCli $dockerCli -Name $volume)) {
        throw "Required Docker volume '$volume' does not exist."
    }
}

if (Test-Path -LiteralPath $destinationPath) {
    $existingItem = Get-Item -LiteralPath $destinationPath
    if (-not $existingItem.PSIsContainer) {
        throw "Destination exists and is not a directory: $destinationPath"
    }

    if (Get-ChildItem -LiteralPath $destinationPath -Force | Select-Object -First 1) {
        throw "Destination directory is not empty. Choose a new -Destination to avoid overwriting an earlier export."
    }
}
else {
    New-Item -ItemType Directory -Path $destinationPath -Force | Out-Null
}

if ($Mode -eq "Dataset") {
    $containerScript = @'
set -eu

test -d /data/source_projects/Python
test -d /data/target_projects/Python/Java
test -f /data/subset_manifest.json

echo "Creating a complete compressed archive. This can take several minutes ..."
tar -czf /export/python-java-dataset.tar.gz -C /data .

cp /data/subset_manifest.json /export/subset_manifest.json
if [ -f /data/target_projects/projects_summary.jsonl ]; then
    cp /data/target_projects/projects_summary.jsonl /export/projects_summary.jsonl
fi

for project_directory in /data/source_projects/Python/*; do
    if [ -d "$project_directory" ]; then
        basename "$project_directory"
    fi
done | sort > /export/python_source_projects.txt

for project_directory in /data/target_projects/Python/Java/*; do
    if [ -d "$project_directory" ]; then
        basename "$project_directory"
    fi
done | sort > /export/python_java_target_projects.txt

source_count=$(wc -l < /export/python_source_projects.txt)
target_count=$(wc -l < /export/python_java_target_projects.txt)
archive_size=$(du -h /export/python-java-dataset.tar.gz | awk '{print $1}')

cat > /export/export_manifest.txt <<EOF
RepoTransBench Python-to-Java dataset export
Exported at (UTC): $(date -u '+%Y-%m-%dT%H:%M:%SZ')
Source projects: $source_count
Target projects: $target_count
Archive size: $archive_size

python-java-dataset.tar.gz      Complete dataset; preserves Linux-only filenames
subset_manifest.json            Runnable-subset metadata
projects_summary.jsonl          Official project metadata
python_source_projects.txt      Python source project names
python_java_target_projects.txt Python-to-Java target project names
EOF

echo "DATASET_EXPORT_OK"
echo "PYTHON_SOURCE_PROJECTS: $source_count"
echo "PYTHON_JAVA_TARGETS: $target_count"
echo "ARCHIVE_SIZE: $archive_size"
exit 0
'@

    $dockerArgs = @(
        "run", "--rm", "-i",
        "--mount", "type=volume,source=$DataVolume,target=/data,readonly",
        "--mount", "type=bind,source=$destinationPath,target=/export",
        "alpine:3.22",
        "sh"
    )
}
else {
    $containerScript = @'
set -eu

if [ -z "$(find /results -mindepth 1 -print -quit)" ]; then
    echo "The results volume is empty. Run at least one experiment first." >&2
    exit 20
fi

if [ -z "$(find /experiment -mindepth 1 -print -quit)" ]; then
    echo "The run/log volume is empty. Run at least one experiment first." >&2
    exit 21
fi

mkdir -p /export/generated-results /export/run-records

echo "Copying all generated results ..."
cp -a /results/. /export/generated-results/

echo "Copying all run logs ..."
cp -a /experiment/. /export/run-records/

result_files=$(find /results -type f | wc -l)
run_files=$(find /experiment -type f | wc -l)

find /results -type f | sort > /export/generated_result_files.txt
find /experiment -type f | sort > /export/run_record_files.txt

cat > /export/export_manifest.txt <<EOF
RepoTransBench generated-results export
Exported at (UTC): $(date -u '+%Y-%m-%dT%H:%M:%SZ')
Generated-result files: $result_files
Run/log files: $run_files

generated-results          Current generated projects from the results volume
run-records                All timestamped prompts, turns, and final summaries
generated_result_files.txt Inventory of result files
run_record_files.txt       Inventory of run/log files

Note: rerunning the same model/project may overwrite its generated project,
while timestamped run logs are retained separately.
EOF

echo "RESULTS_EXPORT_OK"
echo "GENERATED_RESULT_FILES: $result_files"
echo "RUN_RECORD_FILES: $run_files"
exit 0
'@

    $dockerArgs = @(
        "run", "--rm", "-i",
        "--mount", "type=volume,source=$ResultsVolume,target=/results,readonly",
        "--mount", "type=volume,source=$RunVolume,target=/experiment,readonly",
        "--mount", "type=bind,source=$destinationPath,target=/export",
        "alpine:3.22",
        "sh"
    )
}

$containerScript = $containerScript.Replace("`r", "") + "`n"

Invoke-NativeCommand `
    -FilePath $dockerCli `
    -Arguments $dockerArgs `
    -InputText $containerScript

if ($script:LastNativeExitCode -ne 0) {
    throw "Export failed with Docker exit code $script:LastNativeExitCode. The destination may contain a partial export."
}

$manifestPath = Join-Path $destinationPath "export_manifest.txt"
if (-not (Test-Path -LiteralPath $manifestPath)) {
    throw "Docker reported success, but export_manifest.txt was not created."
}

Write-Host ""
Write-Host "Export completed successfully."
Write-Host "Export directory:"
Write-Host $destinationPath
Write-Host ""
Write-Host "Open it with:"
Write-Host "explorer.exe `"$destinationPath`""
