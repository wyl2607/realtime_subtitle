# Download media and create bilingual subtitles.
param(
    [string]$Url,
    [string]$OutputDir,
    [switch]$NoSummary
)
$ErrorActionPreference = "Stop"
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot
$Python = Join-Path $RepoRoot "venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $Python)) {
    Write-Host "Python environment not found. Run install.ps1 first."
    exit 1
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    try {
        $ClipboardText = (Get-Clipboard -Raw -ErrorAction Stop).Trim()
        if ($ClipboardText -match 'https?://\S+') {
            $Url = $Matches[0].TrimEnd('"''.,);]')
            Write-Host "Using video URL from clipboard."
        }
    } catch {
        # Clipboard access is optional; fall back to the input prompt below.
    }
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = Read-Host "Video URL (paste or type)"
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    Write-Host "A video URL is required."
    exit 1
}
$CliArgs = @($Url)
if (-not [string]::IsNullOrWhiteSpace($OutputDir)) {
    $CliArgs += @("--output-dir", $OutputDir)
}
if ($NoSummary) {
    $CliArgs += "--no-summary"
}
& $Python (Join-Path $RepoRoot "download_subtitle.py") @CliArgs
$ExitCode = $LASTEXITCODE
if ($ExitCode -eq 0) {
    Read-Host "Finished. Press Enter to close"
} else {
    Read-Host "Failed. Press Enter to close"
}
exit $ExitCode
