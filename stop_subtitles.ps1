# 停止德语实时字幕程序
$ErrorActionPreference = "SilentlyContinue"
Set-Location $PSScriptRoot

$pidFile = "$PSScriptRoot\subtitle.pid"
$stopped = $false

if (Test-Path $pidFile) {
    $targetPid = Get-Content $pidFile
    if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) {
        Stop-Process -Id $targetPid -Force
        Write-Host "已停止实时字幕程序 (PID $targetPid)"
        $stopped = $true
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

if (-not $stopped) {
    # 兜底：按窗口标题找。必须匹配"实时字幕"，不能只看"有窗口标题"，
    # 否则会把用户开着的其它python图形程序一起杀掉
    $procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*实时字幕*" }
    if ($procs) {
        $procs | Stop-Process -Force
        Write-Host "已按窗口标题停止实时字幕程序"
        $stopped = $true
    }
}

if (-not $stopped) {
    Write-Host "没有找到正在运行的实时字幕程序"
}

# 顺便清掉暂停标记，避免下次启动误判为暂停状态
Remove-Item "$PSScriptRoot\.paused" -ErrorAction SilentlyContinue
