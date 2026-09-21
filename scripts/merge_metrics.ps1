[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string[]]$BatchDirectory,
    [Parameter(Mandatory=$true)][string]$Destination,
    [string]$PythonCommand='python'
)
$ErrorActionPreference='Stop'
$paths=@($BatchDirectory | ForEach-Object {(Resolve-Path -LiteralPath $_).Path})
$outputPath=[IO.Path]::GetFullPath($Destination)
Push-Location (Join-Path $PSScriptRoot '..')
try {
    & $PythonCommand -m rtb_metrics.series merge --output $outputPath --batches @paths
    if ($LASTEXITCODE -ne 0) {throw 'Merge failed. Check duplicate projects and experiment settings.'}
    Write-Host "Merged reports: $outputPath"
} finally {Pop-Location}
