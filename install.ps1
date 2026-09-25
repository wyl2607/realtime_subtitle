# Backward-compatible entrypoint — forwards to scripts/windows/install.ps1
param([switch]$Mirror)
$ErrorActionPreference = "Stop"
$target = Join-Path $PSScriptRoot "scripts\windows\install.ps1"
if ($Mirror) {
    & (Join-Path $PSHOME $(if ($PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })) -NoProfile -ExecutionPolicy Bypass -File $target -Mirror
} else {
    & (Join-Path $PSHOME $(if ($PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })) -NoProfile -ExecutionPolicy Bypass -File $target
}
exit $LASTEXITCODE
