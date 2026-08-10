# 暂停/继续 实时字幕的识别与翻译（不重启进程，Whisper模型继续留在显存里，切回来不用重新加载）
# Repo root (this file lives in scripts/windows/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot
$flag = "$RepoRoot\.paused"

if (Test-Path $flag) {
    Remove-Item $flag
    Write-Host "已继续识别与翻译"
} else {
    if (-not (Test-Path "$RepoRoot\subtitle.pid")) {
        Write-Host "实时字幕程序当前没有在运行，暂停没有意义"
        exit
    }
    New-Item -ItemType File -Path $flag | Out-Null
    Write-Host "已暂停识别与翻译（悬浮窗还在，不会占用GPU做识别）"
}
