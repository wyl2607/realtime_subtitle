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
    $targetPid = (Get-Content $pidFile | Select-Object -First 1).Trim()
    $targetProc = Get-Process -Id $targetPid -ErrorAction SilentlyContinue
    # 与 start 对称：PID 会被系统回收复用。只对「本项目 venv 下的 python」
    # 写 .stop / 强杀；路径对不上就当陈旧 pid，删文件后走窗口标题兜底。
    $isOurs = $targetProc -and $targetProc.Path -like "$PSScriptRoot\venv\*"
    if ($isOurs) {
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
    } elseif ($targetProc) {
        Write-Host "subtitle.pid 里的 PID $targetPid 不是本项目进程（可能已被系统复用），忽略并清理。"
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

# 卸载 Ollama 里常驻的翻译模型：光停 python 不会通知 Ollama，
# 模型会按 keep_alive（默认可长达数小时）继续占着显存/内存，
# llama-server.exe 表现就是“关了字幕但内存没释放”。
# 只卸载模型，不杀 ollama serve 本身，下次启动还是秒开。
# ☠️ 不能用 `ollama stop` CLI：Ollama 服务没在运行时它会自己拉起服务并
# 无限期等待（实测挂 3 分钟不返回）——开机后程序没启动就点停止脚本，
# 窗口就永远关不掉。改走 HTTP：服务不可达 2 秒即知、直接跳过。
$ollamaUp = $false
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/version" -TimeoutSec 2 | Out-Null
    $ollamaUp = $true
} catch {}
if ($ollamaUp) {
    # 只卸载「确实加载着」的本项目模型（/api/ps），不碰其它程序的模型；
    # 什么都没加载时连 python 都不用起，停止更快
    $loaded = @()
    try { $loaded = @((Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/ps" -TimeoutSec 3).models.name) } catch {}
    if ($loaded) {
        $models = & "$PSScriptRoot\venv\Scripts\python.exe" -c "import config; print(config.OLLAMA_MODEL); print(config.GAME_MODE_OLLAMA_MODEL)" 2>$null
        foreach ($m in ($models -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)) {
            if ($loaded -contains $m) {
                try {
                    # prompt留空 + keep_alive=0 = 立即卸载（官方用法）
                    Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/generate" -Method Post `
                        -ContentType "application/json" `
                        -Body (@{ model = $m; prompt = ""; keep_alive = 0 } | ConvertTo-Json -Compress) `
                        -TimeoutSec 15 | Out-Null
                    Write-Host "已卸载 Ollama 常驻模型 $m"
                } catch {}
            }
        }
    }
}

# 清掉暂停/停止标记，避免下次启动误判
Remove-Item $stopFlag -ErrorAction SilentlyContinue
Remove-Item "$PSScriptRoot\.paused" -ErrorAction SilentlyContinue
