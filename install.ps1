# Backward-compatible entrypoint — forwards to scripts/windows/install.ps1
param([switch]$Mirror)
$ErrorActionPreference = "Stop"
$target = Join-Path $PSScriptRoot "scripts\windows\install.ps1"
if ($Mirror) {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $target -Mirror
} else {
    & powershell -NoProfile -ExecutionPolicy Bypass -File $target
}
exit $LASTEXITCODE
