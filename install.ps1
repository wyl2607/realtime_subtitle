# ============================================================
# 实时字幕翻译系统 一键安装脚本（Windows）
#
# 用法：右键本文件 → 使用 PowerShell 运行
#   或： powershell -ExecutionPolicy Bypass -File install.ps1
# 可选： -Mirror  使用清华 PyPI 镜像（国内网络加速）
#
# 做的事：
#   1. 检查 Python 3.10-3.13（本机 3.13 实测可用）
#   2. 检测 NVIDIA 显卡（没有就自动生成 CPU 降级配置）
#   3. 创建 venv 并安装依赖
#   4. 检查/引导安装 Ollama，拉取翻译模型
#   5. 在桌面生成快捷方式文件夹（启动/停止/暂停）
# 全程可重复运行（幂等），中断后重跑即可。
# ============================================================
param(
    [switch]$Mirror  # 国内网络用清华 PyPI 镜像
)
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

Write-Host ""
Write-Host "=========================================="
Write-Host "   实时字幕翻译系统 - 安装程序"
Write-Host "=========================================="
Write-Host ""

# ---------- 1. 找 Python ----------
Write-Host "[1/5] 检查 Python..."
$python = $null
$candidates = @()
if (Get-Command py -ErrorAction SilentlyContinue) {
    $candidates += @("py -3.13", "py -3.12", "py -3.11", "py -3.10")
}
if (Get-Command python -ErrorAction SilentlyContinue) {
    $candidates += "python"
}
foreach ($cand in $candidates) {
    try {
        $parts = $cand -split " "
        $ver = & $parts[0] $parts[1..($parts.Count-1)] -c "import sys; print('%d.%d' % sys.version_info[:2])" 2>$null
        if ($ver -match "^3\.(10|11|12|13)$") {
            $python = $cand
            Write-Host "  ✅ 找到 Python $ver ($cand)"
            break
        }
    } catch { }
}
if (-not $python) {
    Write-Host "  ❌ 没有找到 Python 3.10-3.13。"
    Write-Host "     请到 https://www.python.org/downloads/ 安装 Python 3.13，"
    Write-Host "     安装时勾选 'Add python.exe to PATH'，然后重新运行本脚本。"
    Start-Process "https://www.python.org/downloads/"
    exit 1
}

# ---------- 2. 检测显卡 ----------
Write-Host "[2/5] 检测 NVIDIA 显卡..."
$hasGpu = $false
$gpuMemMB = 0
try {
    $smi = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $smi) {
        $hasGpu = $true
        $first = ($smi | Select-Object -First 1) -split ","
        $gpuName = $first[0].Trim()
        $gpuMemMB = [int]$first[1].Trim()
        Write-Host "  ✅ $gpuName (${gpuMemMB}MB 显存)"
    }
} catch { }
if (-not $hasGpu) {
    Write-Host "  ⚠️  没有检测到 NVIDIA 显卡，将使用 CPU 模式（识别速度会慢不少）"
}

# ---------- 3. venv + 依赖 ----------
Write-Host "[3/5] 安装 Python 依赖（首次需要几分钟）..."
if (-not (Test-Path "$PSScriptRoot\venv\Scripts\python.exe")) {
    $parts = $python -split " "
    & $parts[0] $parts[1..($parts.Count-1)] -m venv "$PSScriptRoot\venv"
}
$vpy = "$PSScriptRoot\venv\Scripts\python.exe"
$pipArgs = @("-m", "pip", "install", "--upgrade")
if ($Mirror) { $pipArgs += @("-i", "https://pypi.tuna.tsinghua.edu.cn/simple") }
& $vpy @pipArgs pip | Out-Null
& $vpy @pipArgs -r "$PSScriptRoot\requirements.txt"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 依赖安装失败，请检查网络后重跑（国内网络可加 -Mirror 参数）"
    exit 1
}
Write-Host "  ✅ 依赖安装完成"

# ---------- 4. 按硬件生成本机配置 ----------
# config_local.py 会覆盖 config.py 的同名配置（config.py 末尾 import 它）
$localCfg = "$PSScriptRoot\config_local.py"
$txModel = "qwen3.5:9b"
if (-not $hasGpu) {
    $txModel = "qwen3.5:2b"
    @"
# 本机降级配置（install.ps1 自动生成：没有检测到 NVIDIA 显卡）
# 如果之后加了显卡，删掉这个文件即可恢复 GPU 高配
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_MODEL = "small"        # CPU 跑不动 large-v3-turbo
OLLAMA_MODEL = "qwen3.5:2b"    # CPU 跑 9b 太慢，用小模型保流畅
CHUNK_SUBMIT_SECONDS = 1.0     # 放慢识别节奏，给 CPU 留时间
"@ | Out-File -FilePath $localCfg -Encoding utf8
    Write-Host "  ✅ 已生成 CPU 降级配置 config_local.py"
} elseif ($gpuMemMB -gt 0 -and $gpuMemMB -lt 6000) {
    @"
# 本机降级配置（install.ps1 自动生成：显存小于 6GB）
# Whisper 用中杯模型 + int8，给 Ollama 留显存
WHISPER_MODEL = "medium"
WHISPER_COMPUTE_TYPE = "int8"
"@ | Out-File -FilePath $localCfg -Encoding utf8
    Write-Host "  ✅ 显存较小，已生成 config_local.py（medium + int8）"
} else {
    Write-Host "  ✅ 显卡配置充足，使用默认高配（large-v3-turbo + float16）"
}

# ---------- 5. Ollama + 翻译模型 ----------
Write-Host "[4/5] 检查 Ollama（本地翻译模型运行时）..."
$ollamaExe = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollamaExe) {
    $guess = "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe"
    if (Test-Path $guess) { $ollamaExe = $guess }
}
if (-not $ollamaExe) {
    Write-Host "  ⚠️  没有安装 Ollama。已打开下载页面，请安装后重新运行本脚本"
    Write-Host "     （Ollama 用来在本地跑翻译模型，全程离线，不上传任何数据）"
    Start-Process "https://ollama.com/download"
    exit 1
}
Write-Host "  ✅ Ollama 已安装"
try {
    Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
} catch {
    Write-Host "  正在启动 Ollama 服务..."
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    Start-Sleep -Seconds 3
}
$installed = @()
try { $installed = (Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags").models.name } catch { }
if ($installed -notcontains $txModel) {
    Write-Host "  正在下载翻译模型 $txModel（首次需要几分钟）..."
    & $ollamaExe pull $txModel
} else {
    Write-Host "  ✅ 翻译模型 $txModel 已就绪"
}

# ---------- 6. 桌面快捷方式 ----------
Write-Host "[5/5] 生成桌面快捷方式..."
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutDir = Join-Path $desktop "德语直播实时字幕"
New-Item -ItemType Directory -Path $shortcutDir -Force | Out-Null

$batTemplate = @(
    @("启动字幕.bat", "start_subtitles.ps1"),
    @("停止字幕.bat", "stop_subtitles.ps1"),
    @("暂停继续字幕.bat", "pause_subtitles.ps1")
)
foreach ($pair in $batTemplate) {
    $head = "@echo off`r`nchcp 65001 >nul`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"$PSScriptRoot\$($pair[1])`"`r`n"
    if ($pair[0] -eq "启动字幕.bat") {
        # 成功≈3秒自动关；失败保留窗口让人看得到报错（报错文本由ps1打印）。
        # ⚠️ bat 必须纯 ASCII：chcp 65001 下 cmd 解析含中文的行会把下一行开头吃掉。
        # 用 ping 当 sleep：timeout.exe 在 stdin 被重定向时直接报错
        $tail = "if errorlevel 1 goto :err`r`nping -n 4 127.0.0.1 >nul`r`nexit /b 0`r`n:err`r`necho.`r`npause`r`n"
    } else {
        $tail = "ping -n 3 127.0.0.1 >nul`r`n"
    }
    [System.IO.File]::WriteAllText((Join-Path $shortcutDir $pair[0]), $head + $tail, (New-Object System.Text.UTF8Encoding $false))
}
@"
双击"启动字幕.bat"开始（首次启动会自动下载语音识别模型，需要几分钟）。
播放任何德语视频/直播，屏幕下方会出现双语字幕悬浮窗。

悬浮窗操作：
  - 鼠标拖动窗口任意位置 = 移动
  - 鼠标拖动窗口边缘/四角 = 缩放（窗口越大，显示的历史字幕越多）
  - ➖ 最小化字幕   📜 历史回看   ⚙️ 参数调节   ❌ 退出
全局快捷键：
  - Ctrl+Alt+P 暂停/继续
  - Ctrl+Alt+L 切换识别语言（德语↔英语）

看完后双击"停止字幕.bat"关闭。
字幕记录自动保存在程序目录的 transcripts\ 文件夹里。
"@ | Out-File -FilePath (Join-Path $shortcutDir "操作说明.txt") -Encoding utf8
Write-Host "  ✅ 快捷方式已生成: $shortcutDir"

Write-Host ""
Write-Host "=========================================="
Write-Host "🎉 安装完成！"
Write-Host "   双击桌面 [德语直播实时字幕\启动字幕.bat] 开始使用"
Write-Host "   （首次启动会下载语音识别模型，请耐心等待）"
Write-Host "=========================================="
