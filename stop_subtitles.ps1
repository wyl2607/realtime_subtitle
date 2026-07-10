# 停止德语实时字幕程序（优先优雅退出，超时再强杀）
$ErrorActionPreference = "SilentlyContinue"
Set-Location $PSScriptRoot

$pidFile = "$PSScriptRoot\subtitle.pid"
$stopFlag = "$PSScriptRoot\.stop"
# 优雅退出正常1-2秒（积压任务直接丢弃、在飞流式翻译会被打断）；
# 3秒还没退干净说明卡住了（GPU被抢时一次识别最坏~2.5秒），直接强杀无害
$graceSeconds = 3
$stopped = $false

function Wait-ProcessExit {
    param([int]$ProcessId, [int]$Seconds)
    # 250ms 一查：程序退完立刻返回，不多等
    for ($i = 0; $i -lt ($Seconds * 4); $i++) {
        if (-not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)) {
            return $true
        }
        Start-Sleep -Milliseconds 250
    }
    return -not (Get-Process -Id $ProcessId -ErrorAction SilentlyContinue)
}

if (Test-Path $pidFile) {
    $targetPid = Get-Content $pidFile
    if (Get-Process -Id $targetPid -ErrorAction SilentlyContinue) {
        # 写停止标记：主程序 QTimer 看到后走 app.quit → stop() 关线程/模型
        New-Item -ItemType File -Path $stopFlag -Force | Out-Null
        Write-Host "正在请求优雅退出 (PID $targetPid，最多等 ${graceSeconds}s)..."
        if (Wait-ProcessExit -ProcessId $targetPid -Seconds $graceSeconds) {
            Write-Host "已优雅停止实时字幕程序 (PID $targetPid)"
            $stopped = $true
        } else {
            Stop-Process -Id $targetPid -Force
            Write-Host "优雅退出超时，已强制停止 (PID $targetPid)"
            $stopped = $true
        }
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue
}

if (-not $stopped) {
    # 兜底：按窗口标题找。必须匹配"实时字幕"，不能只看"有窗口标题"，
    # 否则会把用户开着的其它python图形程序一起杀掉
    $procs = Get-Process python -ErrorAction SilentlyContinue | Where-Object { $_.MainWindowTitle -like "*实时字幕*" }
    if ($procs) {
        foreach ($proc in $procs) {
            New-Item -ItemType File -Path $stopFlag -Force | Out-Null
            if (-not (Wait-ProcessExit -ProcessId $proc.Id -Seconds $graceSeconds)) {
                Stop-Process -Id $proc.Id -Force
            }
        }
        Write-Host "已按窗口标题停止实时字幕程序"
        $stopped = $true
    }
}

if (-not $stopped) {
    Write-Host "没有找到正在运行的实时字幕程序"
}

# 清掉暂停/停止标记，避免下次启动误判
Remove-Item $stopFlag -ErrorAction SilentlyContinue
Remove-Item "$PSScriptRoot\.paused" -ErrorAction SilentlyContinue
