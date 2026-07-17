# 启动德语实时双语字幕（YouTube 等系统音频）
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

$pidFile = "$PSScriptRoot\subtitle.pid"
if (Test-Path $pidFile) {
    $oldPid = Get-Content $pidFile
    $oldProc = Get-Process -Id $oldPid -ErrorAction SilentlyContinue
    # PID会被系统回收复用：光"这个PID有进程"不算数，还得确认真是本项目venv的
    # python（2026-07-17实测：残留pid被别的进程占用→误判"已在运行"拒绝启动）
    if ($oldProc -and $oldProc.Path -like "$PSScriptRoot\venv\*") {
        Write-Host "已经在运行中（PID $oldPid），不用重复启动。要重启请先运行 停止字幕.bat"
        exit
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue  # 残留的过期pid文件
}

# 清掉可能残留的暂停/停止标记，保证每次启动都是正常运行状态
Remove-Item "$PSScriptRoot\.paused" -ErrorAction SilentlyContinue
Remove-Item "$PSScriptRoot\.stop" -ErrorAction SilentlyContinue

$ollamaDir = "$env:LOCALAPPDATA\Programs\Ollama"
if (Test-Path $ollamaDir) {
    $env:Path = "$ollamaDir;$env:Path"
}
# Ollama 可能装在用户目录也可能装在 Program Files，动态解析
$ollamaExe = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollamaExe) { $ollamaExe = "$ollamaDir\ollama.exe" }
if (-not (Test-Path $ollamaExe)) {
    Write-Host "❌ 找不到 Ollama。请先安装: https://ollama.com/download，或运行 install.ps1"
    exit 1
}

Write-Host "检查 Ollama 服务..."
# 轮询等就绪而不是固定睡几秒：Ollama 刚更新完/冷启动时可能十几秒才监听端口
function Test-OllamaReady {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
        return $true
    } catch { return $false }
}
if (-not (Test-OllamaReady)) {
    Write-Host "正在启动 Ollama..."
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-OllamaReady)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host "❌ 等了 60 秒 Ollama 服务还没就绪。"
            Write-Host "   可能它正在更新或上次更新没装好——等更新完成后再运行一次即可。"
            Write-Host "   还不行的话，手动开个终端运行 ollama serve 看报什么错。"
            exit 1
        }
        Start-Sleep -Seconds 2
    }
}

# 翻译模型名从 config 读（config_local.py 里可能配了小模型），不要硬编码
$txModel = & "$PSScriptRoot\venv\Scripts\python.exe" -c "import config; print(config.OLLAMA_MODEL)"
$models = (Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags").models.name
if ($models -notcontains $txModel) {
    Write-Host "正在下载 $txModel 模型（首次需要几分钟）..."
    & $ollamaExe pull $txModel
}

Write-Host "启动实时字幕..."
# 隐藏窗口启动+输出写进日志文件。之前用-NoNewWindow让python挂在这个控制台上，
# 用户手动关窗口时python会被一起杀掉，跟提示语说的"关窗不停止"正好相反
$proc = Start-Process -FilePath "$PSScriptRoot\venv\Scripts\python.exe" `
    -ArgumentList "-u", "main.py" `
    -WorkingDirectory $PSScriptRoot -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$PSScriptRoot\subtitle.log" `
    -RedirectStandardError "$PSScriptRoot\subtitle.err.log"
$proc.Id | Out-File -FilePath $pidFile -Encoding ascii
Write-Host "已启动 (PID $($proc.Id))，运行日志: subtitle.log"
Write-Host "模型加载需要半分钟左右，字幕悬浮窗随后出现。这个窗口马上自动关闭。"