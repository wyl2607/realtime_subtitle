# 启动德语实时双语字幕（YouTube 等系统音频）
$ErrorActionPreference = "Stop"
# Repo root (this file lives in scripts/windows/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot

# ☠️ 判"是不是本项目的进程"一律用 StartsWith，不要用 -like。-like 会把路径里的
# [ ] 当成通配符字符类，而 install.ps1 只拦非 ASCII、[ 是 ASCII——装在
# C:\tools\[wip]\realtime_subtitle 这种目录下时，本脚本会误判"没在运行"允许
# 双开，停止脚本会认不出自己的进程只能走窗口标题兜底。
# OrdinalIgnoreCase：Windows 路径不区分大小写，Get-Process 返回的盘符/目录
# 大小写不保证和 $RepoRoot 一致。
. "$PSScriptRoot\_identity.ps1"

$pidFile = "$RepoRoot\subtitle.pid"
if (Test-Path $pidFile) {
    $oldIdentity = Read-SubtitleIdentity $pidFile
    $oldPid = if ($oldIdentity) { $oldIdentity.pid } else { $null }
    $oldProc = if ($oldPid) { Get-Process -Id $oldPid -ErrorAction SilentlyContinue } else { $null }
    # PID会被系统回收复用：光"这个PID有进程"不算数，还得确认解释器、入口和
    # 创建时间都对得上（裸 StartsWith(venv) 会误匹配 venv_backup）。
    if (Test-RealtimeInstance $oldProc $oldIdentity $RepoRoot) {
        Write-Host "已经在运行中（PID $oldPid），不用重复启动。要重启请先运行 停止字幕.bat"
        exit
    }
    Remove-Item $pidFile -ErrorAction SilentlyContinue  # 残留的过期pid文件
}
# ☠️ pid 文件没了不等于没在运行（跑测试误删过、用户手删过）。以前这里直接往下走：
# 先把正在写的 subtitle.log 截断，再起一个被单实例 mutex 挡下秒退的新进程，
# 最后照样打印"已启动 (PID …)"。按进程身份再查一遍，查到就顺手把 pid 文件补上，
# 这样停止/更新脚本也重新认得它。
$running = Find-RealtimeInstances $RepoRoot
if ($running.Count -gt 0) {
    Write-SubtitleIdentity -Proc $running[0] -PidFile $pidFile -RepoRoot $RepoRoot
    Write-Host "已经在运行中（PID $($running[0].Id)，已补回 subtitle.pid），不用重复启动。要重启请先运行 停止字幕.bat"
    exit
}

# venv 没建 = 还没跑安装（zip拷贝/只clone就双击）。给人话别给红字堆栈
if (-not (Test-Path "$RepoRoot\venv\Scripts\python.exe")) {
    Write-Host "❌ 还没有安装运行环境（venv 不存在）。"
    Write-Host "   请先运行本目录的 install.ps1（右键 → 使用 PowerShell 运行），"
    Write-Host "   或让你的 AI 助手按仓库里的 CLAUDE.md 完成安装。"
    exit 1
}

# 清掉可能残留的暂停/停止标记，保证每次启动都是正常运行状态
Remove-Item "$RepoRoot\.paused" -ErrorAction SilentlyContinue
Remove-Item "$RepoRoot\.stop" -ErrorAction SilentlyContinue

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

# 模型名从 config 读（config_local.py 里可能配了小模型），不要硬编码。
# 一次 python 调用读两个值：venv python 冷启动约0.5秒，起两次纯浪费
# ☠️ 只认带 RSCFG: 前缀的行：config_local.py 是 exec 进来的，里面随手一个
# print 就会混进 stdout。以前直接取第 1/2 行，混进来的文字会被当成模型名
# 拿去 `ollama pull`，报出"网络/模型名过期"，真正的原因反而被盖住。
$cfgOut = & "$RepoRoot\venv\Scripts\python.exe" -c "from realtime_subtitle import config; print('RSCFG:' + str(config.OLLAMA_MODEL)); print('RSCFG:' + str(config.WHISPER_MODEL))"
$cfgExit = $LASTEXITCODE
$cfg = @(@($cfgOut) | ForEach-Object { "$_".Trim() } | Where-Object { $_.StartsWith("RSCFG:") } | ForEach-Object { $_.Substring(6) })
if ($cfgExit -ne 0 -or $cfg.Count -lt 2 -or -not $cfg[0]) {
    Write-Host "❌ 读取配置失败（上面几行是 Python 的原始报错）。"
    Write-Host "   最常见原因：config_local.py 写坏了（语法错误/拼写错误）。"
    Write-Host "   修好它、或暂时改名成 config_local.py.bak 之后再启动。"
    exit 1
}
$txModel = $cfg[0]

# 首次启动检测：Whisper 模型还没下载过（HF 缓存里没有）就明确告知要等几分钟。
# Hidden 启动 + 下载无进度条，不提示的话新用户会以为"双击没反应"
$whisperModel = $cfg[1]
# HuggingFace 缓存位置。优先级必须和 huggingface_hub 自己的一致：
#   HF_HUB_CACHE > HUGGINGFACE_HUB_CACHE(旧名) > HF_HOME\hub > ~\.cache\huggingface\hub
# ☠️ 以前只认 HF_HOME，于是设了 HF_HUB_CACHE 的用户（模型放别的盘）每次启动
# 都会看到"首次启动，要下 1-3GB"的提示并被多留 10 秒——而模型明明早就在本地。
# uninstall.ps1 里有一份同样的实现（那边漏掉的后果更重：模型扫不到、删不掉）。
function Get-HFHubDir {
    if ($env:HF_HUB_CACHE) { return $env:HF_HUB_CACHE }
    if ($env:HUGGINGFACE_HUB_CACHE) { return $env:HUGGINGFACE_HUB_CACHE }
    if ($env:HF_HOME) { return (Join-Path $env:HF_HOME "hub") }
    return (Join-Path $env:USERPROFILE ".cache\huggingface\hub")
}
$firstRun = -not (Get-ChildItem (Get-HFHubDir) -Directory -Filter "models--*whisper*" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -like "*$whisperModel*" })
$models = (Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags").models.name
if ($models -notcontains $txModel) {
    Write-Host "正在下载 $txModel 模型（首次需要几分钟）..."
    & $ollamaExe pull $txModel
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 拉取翻译模型 $txModel 失败（网络/磁盘/模型名过期）。"
        Write-Host "   模型名会随 qwen 迭代变化：到 https://ollama.com/library 查当前名字，"
        Write-Host "   写进 config_local.py 的 OLLAMA_MODEL 后重试。国内网络不稳就多试一次。"
        exit 1
    }
}

Write-Host "启动实时字幕..."

# ☠️ -RedirectStandardOutput 会把 subtitle.log 整个截断。日志里有"概况"诊断行
# （识别/翻译分位数、缓冲分桶、扣留放行与切早计数），要靠跨天累积的数据来判断
# 该不该调参数——每次启动清空的话，重启一次或关一次机就前功尽弃。
# 所以启动前先把上一份挪进 logs\ 存档，只保留最近 30 份（一份约几十 KB）。
$logDir = Join-Path $RepoRoot "logs"
if (-not (Test-Path $logDir)) { New-Item -ItemType Directory -Path $logDir | Out-Null }
foreach ($name in @("subtitle.log", "subtitle.err.log")) {
    $cur = Join-Path $RepoRoot $name
    if ((Test-Path $cur) -and ((Get-Item $cur).Length -gt 0)) {
        $stamp = (Get-Item $cur).LastWriteTime.ToString("yyyyMMdd-HHmmss")
        $base = [IO.Path]::GetFileNameWithoutExtension($name)
        Move-Item $cur (Join-Path $logDir "$base-$stamp.log") -Force -ErrorAction SilentlyContinue
    }
}
# ☠️ 两个前缀要分别裁剪，不能只写 "subtitle-*.log"。归档名来自上面的
# $base，stdout 那份是 "subtitle-<时间戳>.log"，stderr 那份是
# "subtitle.err-<时间戳>.log"——后者**不匹配** `subtitle-*`（中间是 `.err`），
# 于是 err 归档从 2026-08-04 起一份都没被删过，而 stdout 那边正常滚动到 30 份。
# 症状很隐蔽：logs\ 里两种日志的最老日期对不上，除此之外一切正常。
# 各留 30 份而不是合起来 30 份：一次运行产出一对，合并计数会让 err 少的那边
# 把 stdout 的历史挤掉。
foreach ($prefix in @("subtitle", "subtitle.err")) {
    Get-ChildItem $logDir -Filter "$prefix-*.log" -ErrorAction SilentlyContinue |
        Where-Object { $_.Name -match "^$([regex]::Escape($prefix))-\d{8}-\d{6}\.log$" } |
        Sort-Object LastWriteTime -Descending | Select-Object -Skip 30 |
        Remove-Item -Force -ErrorAction SilentlyContinue
}

# 隐藏窗口启动+输出写进日志文件。之前用-NoNewWindow让python挂在这个控制台上，
# 用户手动关窗口时python会被一起杀掉，跟提示语说的"关窗不停止"正好相反
$proc = Start-Process -FilePath "$RepoRoot\venv\Scripts\python.exe" `
    -ArgumentList "-u", "main.py" `
    -WorkingDirectory $RepoRoot -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput "$RepoRoot\subtitle.log" `
    -RedirectStandardError "$RepoRoot\subtitle.err.log"
Write-SubtitleIdentity -Proc $proc -PidFile $pidFile -RepoRoot $RepoRoot
# ☠️ 起来了不等于跑起来了：import 期的致命错误、以及单实例 mutex（另一份装在
# 别处的副本在跑——mutex 是全机的，上面的进程身份检查只认本仓库）都会让它
# 一两秒内就退出。以前不看，照样打印"已启动"，用户只看到窗口自动关掉、字幕没出现。
# 正常启动时这里只多等 2 秒（模型在后台加载，远没到会退出的时候）。
for ($i = 0; $i -lt 8 -and -not $proc.HasExited; $i++) { Start-Sleep -Milliseconds 250 }
if ($proc.HasExited) {
    Remove-Item $pidFile -ErrorAction SilentlyContinue
    $out = @(Get-Content "$RepoRoot\subtitle.log" -Encoding UTF8 -ErrorAction SilentlyContinue)
    $err = @(Get-Content "$RepoRoot\subtitle.err.log" -Encoding UTF8 -ErrorAction SilentlyContinue)
    if (($out -join "`n") -match "已经在运行") {
        Write-Host "实时字幕已经在运行了（可能是装在别的目录的另一份），没有启动第二个。"
        exit 0
    }
    Write-Host "❌ 字幕程序启动后立刻退出了。最后几行输出："
    @($out + $err) | Where-Object { "$_".Trim() } | Select-Object -Last 12 |
        ForEach-Object { Write-Host "   $_" }
    Write-Host "   完整日志：subtitle.log / subtitle.err.log（可以直接发给 AI 排查）"
    exit 1
}
Write-Host "已启动 (PID $($proc.Id))，运行日志: subtitle.log"
if ($firstRun) {
    Write-Host ""
    Write-Host "⏬ 首次启动：字幕悬浮窗几秒内会先出现（带加载提示），同时在后台"
    Write-Host "   下载语音识别模型（1-3GB，视网速需要几分钟），下载完自动就绪。"
    Write-Host "   想看进度：用记事本打开本目录的 subtitle.log。"
    Write-Host "   （中国大陆网络若长时间无进展，参见 CLAUDE.md 的 HF_ENDPOINT 镜像设置）"
    Start-Sleep -Seconds 10  # 首次启动多留几秒让人读完上面这段
} else {
    Write-Host "字幕悬浮窗几秒内出现，模型在后台继续加载（悬浮窗上有进度提示）。这个窗口马上自动关闭。"
}