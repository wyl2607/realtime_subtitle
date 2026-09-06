# 下载视频并制作双语字幕。
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
    Write-Host "❌ 还没有安装运行环境（找不到 venv）。请先运行 install.ps1。"
    exit 1
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    try {
        $ClipboardText = (Get-Clipboard -Raw -ErrorAction Stop).Trim()
        if ($ClipboardText -match 'https?://\S+') {
            $Url = $Matches[0].TrimEnd('"''.,);]')
            Write-Host "已从剪贴板读取视频地址。"
        }
    } catch {
        # 读剪贴板失败就走下面的输入提示。
    }
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = Read-Host "请粘贴或输入视频地址"
}
if ([string]::IsNullOrWhiteSpace($Url)) {
    Write-Host "❌ 需要一个视频地址才能继续。"
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
    Read-Host "完成。按回车关闭"
} else {
    Read-Host "失败。按回车关闭"
}
exit $ExitCode
