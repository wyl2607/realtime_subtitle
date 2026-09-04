# Compatibility shim; the canonical script lives in scripts\windows\.
param(
    [string]$Url,
    [string]$OutputDir,
    [switch]$NoSummary
)
$ErrorActionPreference = "Stop"
$Target = Join-Path $PSScriptRoot "scripts\windows\download_subtitle.ps1"
$ForwardArgs = @{}
if ($Url) { $ForwardArgs.Url = $Url }
if ($OutputDir) { $ForwardArgs.OutputDir = $OutputDir }
if ($NoSummary) { $ForwardArgs.NoSummary = $true }
& powershell -NoProfile -ExecutionPolicy Bypass -File $Target @ForwardArgs
exit $LASTEXITCODE
