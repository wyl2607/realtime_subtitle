# 停止德语实时字幕程序（优先优雅退出，超时再强杀）
$ErrorActionPreference = "SilentlyContinue"
# Repo root (this file lives in scripts/windows/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot

$pidFile = "$RepoRoot\subtitle.pid"
$stopFlag = "$RepoRoot\.stop"
# ☠️ 判"是不是本项目的进程"一律用 StartsWith，不要用 -like（-like 把路径里的
# [ ] 当通配符字符类，装在 C:\tools\[wip]\... 下就认不出自己的进程了）。
# 本文件末尾的残留检查一直用的就是 StartsWith，这里跟它统一。
$VenvPrefix = Join-Path $RepoRoot "venv"
function Test-OurProcess {
    param($Proc)
    return $Proc -and $Proc.Path -and
        $Proc.Path.StartsWith($VenvPrefix, [StringComparison]::OrdinalIgnoreCase)
}
# 优雅退出正常1-2秒（积压任务直接丢弃、在飞流式翻译会被打断），但
# .stop轮询0.5s+在飞识别(GPU被抢最坏~2.5s)+Ollama卸载HTTP(~1-2s)叠加时
# 会顶到3秒边缘（2026-07-20实测3次停止2次超时强杀）。放到5秒：退得快
# 照样立即返回（250ms一查），超时强杀仍无害（模型卸载有下面的HTTP兜底，
# 只丢最近15秒内的窗口位置变更）
$graceSeconds = 5
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
    $isOurs = Test-OurProcess $targetProc
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
    $psOk = $false
    try {
        $loaded = @((Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/ps" -TimeoutSec 3).models.name)
        $psOk = $true
    } catch {
        Write-Host "查询 Ollama 已加载模型失败，跳过卸载（显存最多占用到 keep_alive 到期）"
    }
    if ($psOk -and $loaded) {
        $models = & "$RepoRoot\venv\Scripts\python.exe" -c "from realtime_subtitle import config; print(config.OLLAMA_MODEL); print(config.GAME_MODE_OLLAMA_MODEL)" 2>$null
        foreach ($m in ($models -split "`n" | ForEach-Object { $_.Trim() } | Where-Object { $_ } | Select-Object -Unique)) {
            # 名字精确匹配 + 前缀匹配：config 写 "qwen3.5" 时 /api/ps 可能报
            # "qwen3.5:latest"，只做 -contains 会漏卸
            $hit = $loaded | Where-Object { $_ -eq $m -or $_ -like "$m`:*" }
            foreach ($h in $hit) {
                try {
                    # prompt留空 + keep_alive=0 = 立即卸载（官方用法）
                    Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/generate" -Method Post `
                        -ContentType "application/json" `
                        -Body (@{ model = $h; prompt = ""; keep_alive = 0 } | ConvertTo-Json -Compress) `
                        -TimeoutSec 20 | Out-Null
                    Write-Host "已卸载 Ollama 常驻模型 $h"
                } catch {
                    Write-Host "卸载 $h 失败（Ollama 正忙？显存最多占用到 keep_alive 到期）"
                }
            }
        }
    }
}

# 清掉暂停/停止标记，避免下次启动误判
Remove-Item $stopFlag -ErrorAction SilentlyContinue
Remove-Item "$RepoRoot\.paused" -ErrorAction SilentlyContinue

# 验收：确认本项目 venv 下没有残留的 python 进程。
# subtitle.pid 记的是 venv 启动器存根，真正的程序是它的子进程——2026-07-17
# 实验确认过强杀存根时子进程会随 CPython 启动器自带的 Job 一起消亡，所以这里
# 只观测不自动杀（按未经验证的假设去杀进程反而危险）。真出现残留就说明那条
# 结论在某个 Windows/Python 版本上不成立，需要重新做实验。
# ☠️ 要给宽限期，不能查一次就报。优雅退出真正生效之后（2026-08-11 修好
# .stop 路径之前这条路径从没被走到过），存根 1.5 秒就退出，而真正的子进程还要
# 再几百毫秒才收完尾——查一次必然撞上，于是每次正常停止都打一条「这不该发生」
# 吓用户。2026-08-11 实测 4 次停止：2 次 1.2~1.6 秒干净退出，2 次报了残留、
# 而那两个 PID 都在几秒内自行消失（其中一次超过 3 秒）。轮询 8 秒兜住这个抖动；
# 退干净就立刻返回，所以这 8 秒只有**真残留**时才会付。
$leftover = $null
for ($i = 0; $i -lt 32; $i++) {
    $leftover = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.ExecutablePath -and
            $_.ExecutablePath.StartsWith($VenvPrefix, [StringComparison]::OrdinalIgnoreCase) }
    if (-not $leftover) { break }
    Start-Sleep -Milliseconds 250
}
if ($leftover) {
    Write-Host ""
    Write-Host "⚠️  停止后仍有本项目的 python 进程残留（PID: $($leftover.ProcessId -join ', ')）。"
    Write-Host "   这不该发生。请把这行连同 subtitle.log 发给 AI（可能占着显存/音频设备，"
    Write-Host "   下次启动会提示「已经在运行」）。手动结束: Stop-Process -Id $($leftover.ProcessId -join ',') -Force"
}
