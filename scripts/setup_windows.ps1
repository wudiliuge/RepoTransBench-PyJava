[CmdletBinding()]
param(
    [string]$ImageName = "repotransbench-pyjava:local",
    [string]$RawVolume = "rtb_pyjava_raw_v1",
    [string]$DataVolume = "rtb_pyjava_data_v1",
    [string]$ResultsVolume = "rtb_pyjava_results_v1",
    [string]$RunVolume = "rtb_pyjava_run_v1",
    [string]$MavenVolume = "rtb_maven_cache_v1",
    [string]$DatasetUrl = "https://github.com/DeepSoftwareAnalytics/RepoTransBench/releases/download/v1.0/projects.tar.gz",
    [string]$DatasetCacheDirectory = "",
    [switch]$RebuildImage
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
        # Windows PowerShell 5.1 wraps native stderr as ErrorRecord objects.
        # Docker and curl commonly write progress/warnings to stderr even on
        # success, so use the native exit code as the source of truth.
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

function Resolve-CurlCli {
    $command = Get-Command curl.exe -ErrorAction SilentlyContinue
    if ($command) {
        return $command.Source
    }

    $candidate = Join-Path $env:SystemRoot "System32\curl.exe"
    if (Test-Path -LiteralPath $candidate) {
        return $candidate
    }

    throw "curl.exe was not found. Install a current Windows curl client."
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

function Test-VolumePath {
    param(
        [string]$DockerCli,
        [string]$Volume,
        [string]$Path
    )

    $args = @(
        "run", "--rm",
        "--mount", "type=volume,source=$Volume,target=/volume,readonly",
        "alpine:3.22",
        "sh", "-c", "test -e /volume/$Path"
    )

    Invoke-NativeCommand `
        -FilePath $DockerCli `
        -Arguments $args `
        -Quiet
    return ($script:LastNativeExitCode -eq 0)
}

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dockerCli = Resolve-DockerCli
$dockerfile = Join-Path $repoRoot "Dockerfile.pyjava"

if ([string]::IsNullOrWhiteSpace($DatasetCacheDirectory)) {
    $DatasetCacheDirectory = Join-Path (Split-Path $repoRoot -Parent) "RepoTransBench-dataset-cache"
}

$DatasetCacheDirectory = [System.IO.Path]::GetFullPath($DatasetCacheDirectory)
$archive = Join-Path $DatasetCacheDirectory "projects.tar.gz"

Write-Host "Repo root    : $repoRoot"
Write-Host "Docker CLI   : $dockerCli"
Write-Host "Dataset cache: $DatasetCacheDirectory"

Invoke-NativeCommand -FilePath $dockerCli -Arguments @("info") -Quiet
if ($script:LastNativeExitCode -ne 0) {
    throw "Docker Desktop is not running or its Linux engine is unavailable."
}

if (-not (Test-Path -LiteralPath $dockerfile)) {
    throw "Dockerfile not found: $dockerfile"
}

if ($RebuildImage -or -not (Test-DockerImage -DockerCli $dockerCli -Name $ImageName)) {
    Write-Host "Building Docker image: $ImageName"
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
else {
    Write-Host "Docker image already exists: $ImageName"
}

Ensure-DockerVolume -DockerCli $dockerCli -Name $RawVolume
Ensure-DockerVolume -DockerCli $dockerCli -Name $DataVolume
Ensure-DockerVolume -DockerCli $dockerCli -Name $ResultsVolume
Ensure-DockerVolume -DockerCli $dockerCli -Name $RunVolume
Ensure-DockerVolume -DockerCli $dockerCli -Name $MavenVolume

$subsetReady = Test-VolumePath `
    -DockerCli $dockerCli `
    -Volume $DataVolume `
    -Path "subset_manifest.json"

if (-not $subsetReady) {
    New-Item -ItemType Directory -Path $DatasetCacheDirectory -Force | Out-Null

    $curlCli = Resolve-CurlCli
    Write-Host "Downloading or resuming official dataset archive ..."
    $curlArgs = @(
        "-L",
        "-C", "-",
        "--retry", "20",
        "--retry-all-errors",
        "--retry-delay", "5",
        "--connect-timeout", "30",
        "-o", $archive,
        $DatasetUrl
    )
    Invoke-NativeCommand -FilePath $curlCli -Arguments $curlArgs

    if ($script:LastNativeExitCode -ne 0) {
        throw "Dataset download failed. Re-run this script to resume."
    }

    Write-Host "Validating archive with Linux tar ..."
    $archiveCheckArgs = @(
        "run", "--rm",
        "--mount", "type=bind,source=$archive,target=/input/projects.tar.gz,readonly",
        "alpine:3.22",
        "sh", "-c", "tar -tzf /input/projects.tar.gz >/dev/null"
    )
    Invoke-NativeCommand -FilePath $dockerCli -Arguments $archiveCheckArgs
    if ($script:LastNativeExitCode -ne 0) {
        throw "The dataset archive is incomplete or corrupt: $archive"
    }

    $rawReady = Test-VolumePath `
        -DockerCli $dockerCli `
        -Volume $RawVolume `
        -Path "target_projects/projects_summary.jsonl"

    if (-not $rawReady) {
        $rawIsEmptyArgs = @(
            "run", "--rm",
            "--mount", "type=volume,source=$RawVolume,target=/raw",
            "alpine:3.22",
            "sh", "-c", 'test -z "$(find /raw -mindepth 1 -maxdepth 1 -print -quit)"'
        )
        Invoke-NativeCommand `
            -FilePath $dockerCli `
            -Arguments $rawIsEmptyArgs `
            -Quiet
        if ($script:LastNativeExitCode -ne 0) {
            throw "Raw volume '$RawVolume' is partially populated. Inspect it manually; this script will not delete it."
        }

        Write-Host "Extracting archive inside a Linux container ..."
        $extractArgs = @(
            "run", "--rm",
            "--mount", "type=bind,source=$archive,target=/input/projects.tar.gz,readonly",
            "--mount", "type=volume,source=$RawVolume,target=/raw",
            "alpine:3.22",
            "sh", "-c", "tar -xzf /input/projects.tar.gz -C /raw"
        )
        Invoke-NativeCommand -FilePath $dockerCli -Arguments $extractArgs
        if ($script:LastNativeExitCode -ne 0) {
            throw "Linux archive extraction failed."
        }
    }

    $extractSubset = @'
from pathlib import Path
import json
import shutil

raw = Path("/raw")
subset = Path("/subset")
manifest_path = subset / "subset_manifest.json"

if manifest_path.exists():
    print("Subset manifest already exists; validating instead of overwriting.")
else:
    existing = list(subset.iterdir())
    if existing:
        raise SystemExit(
            "Subset volume is partially populated and has no manifest; refusing to overwrite it."
        )

    summary_path = raw / "target_projects" / "projects_summary.jsonl"
    rows = [
        json.loads(line)
        for line in summary_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]

    metadata_rows = [
        row for row in rows
        if row.get("source_language") == "Python"
        and row.get("target_language") == "Java"
    ]

    selected = []
    skipped = []

    for row in metadata_rows:
        name = row["project_name"]
        source = raw / "source_projects" / "Python" / name
        target = raw / "target_projects" / "Python" / "Java" / name

        if not target.is_dir():
            skipped.append(name)
            continue
        if not source.is_dir():
            raise SystemExit(f"Missing source project for runnable target: {name}")

        selected.append(row)

    if len(selected) != 169:
        raise SystemExit(f"Expected 169 runnable Python->Java tasks, found {len(selected)}")

    source_root = subset / "source_projects" / "Python"
    target_root = subset / "target_projects" / "Python" / "Java"
    source_root.mkdir(parents=True)
    target_root.mkdir(parents=True)

    for index, row in enumerate(selected, start=1):
        name = row["project_name"]
        shutil.copytree(
            raw / "source_projects" / "Python" / name,
            source_root / name,
            symlinks=True,
        )
        shutil.copytree(
            raw / "target_projects" / "Python" / "Java" / name,
            target_root / name,
            symlinks=True,
        )
        if index % 20 == 0 or index == len(selected):
            print(f"Copied {index}/{len(selected)} tasks")

    filtered_summary = subset / "target_projects" / "projects_summary.jsonl"
    filtered_summary.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in selected),
        encoding="utf-8",
    )

    manifest = {
        "source_language": "Python",
        "target_language": "Java",
        "metadata_rows": len(metadata_rows),
        "runnable_tasks": len(selected),
        "skipped_missing_targets": sorted(skipped),
    }
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

source_dirs = [p for p in (subset / "source_projects" / "Python").iterdir() if p.is_dir()]
target_dirs = [p for p in (subset / "target_projects" / "Python" / "Java").iterdir() if p.is_dir()]

print(f"Python source projects : {len(source_dirs)}")
print(f"Python->Java targets   : {len(target_dirs)}")

if len(source_dirs) != 169 or len(target_dirs) != 169:
    raise SystemExit("Subset validation failed")

print("PYTHON_JAVA_SUBSET_OK")
'@

    Write-Host "Creating the 169-task Python->Java subset ..."
    $subsetArgs = @(
        "run", "--rm", "-i",
        "--mount", "type=volume,source=$RawVolume,target=/raw,readonly",
        "--mount", "type=volume,source=$DataVolume,target=/subset",
        $ImageName,
        "python", "-"
    )
    Invoke-NativeCommand `
        -FilePath $dockerCli `
        -Arguments $subsetArgs `
        -InputText $extractSubset
    if ($script:LastNativeExitCode -ne 0) {
        throw "Python->Java subset extraction failed."
    }
}
else {
    Write-Host "Python->Java subset already exists; skipping download and extraction."
}

$verify = @'
from pathlib import Path
import json

root = Path("/data")
manifest = json.loads((root / "subset_manifest.json").read_text(encoding="utf-8"))
sources = [p for p in (root / "source_projects" / "Python").iterdir() if p.is_dir()]
targets = [p for p in (root / "target_projects" / "Python" / "Java").iterdir() if p.is_dir()]
manifest_count = manifest.get("runnable_tasks", manifest.get("task_count"))

print(f"Manifest runnable tasks: {manifest_count}")
print(f"Python source projects : {len(sources)}")
print(f"Python->Java targets   : {len(targets)}")

assert manifest_count == 169
assert len(sources) == 169
assert len(targets) == 169
print("SETUP_VERIFICATION_OK")
'@

$verifyArgs = @(
    "run", "--rm", "-i",
    "--mount", "type=volume,source=$DataVolume,target=/data,readonly",
    "--mount", "type=bind,source=$repoRoot,target=/methods,readonly",
    "--workdir", "/methods",
    $ImageName,
    "python", "-"
)

Invoke-NativeCommand `
    -FilePath $dockerCli `
    -Arguments $verifyArgs `
    -InputText $verify
if ($script:LastNativeExitCode -ne 0) {
    throw "Final setup verification failed."
}

Write-Host ""
Write-Host "Setup completed successfully."
Write-Host "Run one project with:"
Write-Host ".\scripts\run_single.ps1 -ProjectName OilerNetwork_fossil_cairo0 -ModelName deepseek-v4-flash -MaxIterations 20"
