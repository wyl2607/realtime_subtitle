# ============================================================
# 实时字幕翻译系统 一键安装脚本（Windows）
#
# 用法：右键本文件 → 使用 PowerShell 运行
#   或： powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# 可选： -Mirror  使用清华 PyPI 镜像（国内网络加速）
#
# 做的事：
#   0. 拦住非英文安装路径（会把生成的 .bat 弄坏）
#   1. 检查 Python 3.10-3.13（本机 3.13 实测可用）
#   2. 检测 NVIDIA 显卡（没有就自动生成 CPU 降级配置）
#   3. 创建 venv 并安装依赖（无 N 卡时跳过 CUDA 运行库）
#   4. 检查/引导安装 Ollama，拉取翻译模型
#   5. 在桌面生成快捷方式文件夹（启动/停止/暂停）
#   6. 自检：把最容易坏的依赖当场 import 一遍
# 全程可重复运行（幂等），中断后重跑即可。
# ============================================================
param(
    [switch]$Mirror  # 国内网络用清华 PyPI 镜像
)
$ErrorActionPreference = "Stop"
# Repo root (this file lives in scripts/windows/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot

Write-Host ""
Write-Host "=========================================="
Write-Host "   实时字幕翻译系统 - 安装程序"
Write-Host "=========================================="
Write-Host ""

# ---------- 0. 安装路径必须是纯 ASCII ----------
# ☠️ 桌面那四个 .bat 里会内嵌本目录的绝对路径，而 bat 在 chcp 65001 下解析
# 含非 ASCII 的行会把下一行开头吃掉（避坑清单第 4 条），启动脚本直接损坏。
# 中文 Windows 用户名（C:\Users\张三\...）是最常见的触发方式，而且症状
# 完全看不出跟路径有关，所以这里直接拦住，不让人装完再踩。
if ($RepoRoot -match '[^\x20-\x7E]') {
    Write-Host "  ❌ 安装路径里有非英文字符，装了也起不来："
    Write-Host "     $RepoRoot"
    Write-Host ""
    Write-Host "     原因：桌面快捷方式 .bat 会内嵌这个路径，cmd 在 UTF-8 代码页下"
    Write-Host "     解析含中文的行会啃掉下一行，启动脚本必坏（中文用户名最常踩）。"
    Write-Host "     解决：把整个仓库挪到纯英文路径再重跑，例如 C:\realtime_subtitle"
    exit 1
}

# 调用候选 Python 命令（"python" 或 "py -3.13"）。
# ☠️ 不能用 $parts[1..($parts.Count-1)]：Count=1 时 1..0 会把自身再塞回参数，
# 变成 `python python -c ...`，只有 python、没有 py launcher 的机器安装必挂。
# ☠️ 参数名不要叫 Cand/Command 等能被 -c 前缀匹配的：PowerShell 会把
# `Invoke-X -c "code"` 里的 -c 绑到 -Cand，脚本字符串被拆碎。
function Invoke-PythonCand {
    param(
        [Parameter(Mandatory = $true)][string]$Spec,
        [Parameter(Mandatory = $true)][string[]]$ArgList
    )
    $parts = @($Spec -split "\s+" | Where-Object { $_ })
    if ($parts.Count -eq 0) { throw "empty python candidate" }
    if ($parts.Count -eq 1) {
        & $parts[0] @ArgList
    } else {
        $exe = $parts[0]
        $prefix = $parts[1..($parts.Count - 1)]
        & $exe @prefix @ArgList
    }
}

# ---------- 1. 找 Python ----------
Write-Host "[1/6] 检查 Python..."
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
        $ver = Invoke-PythonCand -Spec $cand -ArgList @(
            "-c", "import sys; print('%d.%d' % sys.version_info[:2])"
        ) 2>$null
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
Write-Host "[2/6] 检测 NVIDIA 显卡..."
$hasGpu = $false
$gpuMemMB = 0
try {
    $smi = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null
    if ($LASTEXITCODE -eq 0 -and $smi) {
        $hasGpu = $true
        # 多张N卡时取显存最大那张分档（笔记本亮机卡/旧卡枚举在前会分错档）
        $best = $smi | ForEach-Object {
            $p = $_ -split ","
            [pscustomobject]@{ Name = $p[0].Trim(); Mem = [int]$p[1].Trim() }
        } | Sort-Object Mem -Descending | Select-Object -First 1
        $gpuName = $best.Name
        $gpuMemMB = $best.Mem
        Write-Host "  ✅ $gpuName (${gpuMemMB}MB 显存)"
        # 驱动 CUDA 运行库版本：旧版表头 "CUDA Version: 11.x"，新驱动也可能是
        # "CUDA UMD Version: 13.x"。ctranslate2/cublas12 要求驱动侧 CUDA ≥ 12.0
        $smiHead = (& nvidia-smi 2>$null | Select-Object -First 15) -join "`n"
        $cudaReported = $null
        if ($smiHead -match 'CUDA(?:\s+UMD)?\s+Version:\s*([\d.]+)') {
            $cudaReported = [double]$Matches[1]
        }
        if ($null -ne $cudaReported -and $cudaReported -lt 12.0) {
            Write-Host "  ❌ 驱动报告的 CUDA $cudaReported < 12.0，GPU 识别会在加载 cublas 时失败。"
            Write-Host "     请先升级 NVIDIA 驱动（GeForce Experience / 官网），再重跑本脚本。"
            Write-Host "     本次改为 CPU 模式配置，避免装完第一次启动直接崩；"
            Write-Host "     驱动升好后：删掉 config_local.py 再跑 install.ps1 恢复 GPU 档。"
            $hasGpu = $false
        } elseif ($null -ne $cudaReported) {
            Write-Host "  ✅ 驱动 CUDA $cudaReported（≥12.0，满足 cublas12）"
        }
    }
} catch { }
if (-not $hasGpu) {
    Write-Host "  ⚠️  没有检测到可用的 NVIDIA GPU 路径，将使用 CPU 模式（识别速度会慢不少）"
}

# ---------- 3. venv + 依赖 ----------
Write-Host "[3/6] 安装 Python 依赖（首次需要几分钟）..."
if (-not (Test-Path "$RepoRoot\venv\Scripts\python.exe")) {
    Invoke-PythonCand -Spec $python -ArgList @("-m", "venv", "$RepoRoot\venv")
}
$vpy = "$RepoRoot\venv\Scripts\python.exe"
$pipArgs = @("-m", "pip", "install", "--upgrade")
if ($Mirror) { $pipArgs += @("-i", "https://pypi.tuna.tsinghua.edu.cn/simple") }
& $vpy @pipArgs pip | Out-Null

# 没有 N 卡就别装那两个 CUDA 运行库（nvidia-cublas-cu12 + nvidia-cudnn-cu12
# 加起来 1GB 出头，CPU 档位一个字节都用不到；translator_queue 里的 PATH 注入
# 对 ImportError 有守卫，缺了不影响启动）。requirements.txt 本身保持完整——
# README 里写的手动装法是给有卡的机器用的，不能因为这个优化而失效。
$reqFile = "$RepoRoot\requirements.txt"
if (-not $hasGpu) {
    $reqFile = Join-Path $env:TEMP "rt_subtitle_requirements_cpu.txt"
    Get-Content "$RepoRoot\requirements.txt" |
        Where-Object { $_ -notmatch '^\s*nvidia-' } |
        Set-Content -Path $reqFile -Encoding UTF8
    Write-Host "  ℹ️ CPU 模式：跳过 CUDA 运行库（省约 1GB 下载）"
}
& $vpy @pipArgs -r $reqFile
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ❌ 依赖安装失败，请检查网络后重跑（国内网络可加 -Mirror 参数）"
    exit 1
}
Write-Host "  ✅ 依赖安装完成"

# ---------- 4. 按硬件生成本机配置 ----------
# config_local.py 会覆盖 config.py 的同名配置（config.py 末尾 import 它）。
# 分档原则：Whisper + 翻译模型 + 桌面/浏览器本身的显存占用要同时放得下，
# 宁可降翻译模型也不挤爆显存（挤爆 = Ollama 层挪 CPU，翻译显著变慢）。
# ⚠️ 已存在的 config_local.py 一律保留（可能是人工调过的）——想按硬件重新生成就先删掉它
$localCfg = "$RepoRoot\config_local.py"
if (Test-Path $localCfg) {
    Write-Host "  ℹ️ 已有 config_local.py，保留现有本机配置（想重新按硬件生成：删掉它再重跑）"
} elseif (-not $hasGpu) {
    @"
# 本机降级配置（install.ps1 自动生成：没有检测到 NVIDIA 显卡）
# 如果之后加了显卡，删掉这个文件重跑 install.ps1 即可
WHISPER_DEVICE = "cpu"
WHISPER_COMPUTE_TYPE = "int8"
WHISPER_MODEL = "small"        # CPU 跑不动 large-v3-turbo
OLLAMA_MODEL = "qwen3.5:2b"    # CPU 跑 9b 太慢，用小模型保流畅
GAME_MODE_OLLAMA_MODEL = None  # 主模型已是最轻档，游戏模式别再切（默认值4b反而更大）
CHUNK_SUBMIT_SECONDS = 1.0     # 放慢识别节奏，给 CPU 留时间
"@ | Out-File -FilePath $localCfg -Encoding utf8
    Write-Host "  ✅ 已生成 CPU 降级配置 config_local.py"
} elseif ($gpuMemMB -lt 4500) {
    @"
# 本机降级配置（install.ps1 自动生成：显存约 4GB）
WHISPER_MODEL = "small"
WHISPER_COMPUTE_TYPE = "int8"
OLLAMA_MODEL = "qwen3.5:2b"
GAME_MODE_OLLAMA_MODEL = None  # 主模型已是最轻档，游戏模式别再切（默认值4b反而更大）
"@ | Out-File -FilePath $localCfg -Encoding utf8
    Write-Host "  ✅ 显存约4GB，已生成 config_local.py（small int8 + qwen3.5:2b）"
} elseif ($gpuMemMB -lt 6500) {
    @"
# 本机降级配置（install.ps1 自动生成：显存约 6GB）
# large-v3-turbo int8 德语准确率远好于 medium，显存差不多（~2GB）
WHISPER_MODEL = "large-v3-turbo"
WHISPER_COMPUTE_TYPE = "int8"   # ⚠️ RTX 50系不支持int8：ctranslate2>=4.6.2会自动退回，无需处理
OLLAMA_MODEL = "qwen3.5:2b"     # 实测显存余量>2.5GB可手动升 "qwen3.5:4b"（翻译质量明显更好）
GAME_MODE_OLLAMA_MODEL = None  # 主模型已是最轻档，游戏模式别再切（默认值4b反而更大）
"@ | Out-File -FilePath $localCfg -Encoding utf8
    Write-Host "  ✅ 显存约6GB，已生成 config_local.py（turbo int8 + qwen3.5:2b）"
} elseif ($gpuMemMB -lt 9500) {
    @"
# 本机降级配置（install.ps1 自动生成：显存约 8GB，主流游戏卡档）
# Whisper ~2.4GB + qwen3.5:4b ~3.4GB + 桌面/浏览器 ~1.5GB，留有余量。
# 9b(5.6GB) 在 8GB 卡上会贴上限：Ollama 把放不下的层挪 CPU，翻译慢 1-2 秒。
# 想追求翻译质量可手动改 OLLAMA_MODEL = "qwen3.5:9b" 自己实测
WHISPER_MODEL = "large-v3-turbo"
WHISPER_COMPUTE_TYPE = "float16"
OLLAMA_MODEL = "qwen3.5:4b"
GAME_MODE_OLLAMA_MODEL = None  # 主模型已是4b，游戏模式不用再切
"@ | Out-File -FilePath $localCfg -Encoding utf8
    Write-Host "  ✅ 显存约8GB，已生成 config_local.py（turbo float16 + qwen3.5:4b）"
} else {
    Write-Host "  ✅ 显存充足（≥10GB），使用默认高配（large-v3-turbo float16 + qwen3.5:9b）"
}

# ---------- 5. Ollama + 翻译模型 ----------
Write-Host "[4/6] 检查 Ollama（本地翻译模型运行时）..."
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
# 轮询等就绪而不是固定睡几秒：Ollama 刚装完/刚更新完可能十几秒才监听端口
function Test-OllamaReady {
    try {
        Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags" -TimeoutSec 3 | Out-Null
        return $true
    } catch { return $false }
}
if (-not (Test-OllamaReady)) {
    Write-Host "  正在启动 Ollama 服务..."
    Start-Process -FilePath $ollamaExe -ArgumentList "serve" -WindowStyle Hidden
    $deadline = (Get-Date).AddSeconds(60)
    while (-not (Test-OllamaReady)) {
        if ((Get-Date) -gt $deadline) {
            Write-Host "  ❌ Ollama 服务 60 秒内没就绪（可能正在更新）。等它更新完重跑本脚本即可。"
            exit 1
        }
        Start-Sleep -Seconds 2
    }
}
# 模型名从「最终生效的配置」里读（config.py + 刚生成的 config_local.py），
# 保证脚本和程序运行时用的一定是同一个模型，不会各说各话
$txModel = (& $vpy -c "import config; print(config.OLLAMA_MODEL)").Trim()
$gameModel = (& $vpy -c "import config; print(config.GAME_MODE_OLLAMA_MODEL or '')").Trim()
$installed = @()
try { $installed = (Invoke-RestMethod -Uri "http://127.0.0.1:11434/api/tags").models.name } catch { }
foreach ($m in @($txModel, $gameModel) | Where-Object { $_ } | Select-Object -Unique) {
    if ($installed -notcontains $m) {
        Write-Host "  正在下载翻译模型 $m（首次需要几分钟）..."
        & $ollamaExe pull $m
        if ($LASTEXITCODE -ne 0) {
            Write-Host "  ❌ 拉取 $m 失败。若报模型不存在：qwen 系列命名可能已迭代，"
            Write-Host "     到 https://ollama.com/library 查最新名字，写进 config_local.py 的 OLLAMA_MODEL 后重跑"
            exit 1
        }
    } else {
        Write-Host "  ✅ 翻译模型 $m 已就绪"
    }
}

# ---------- 6. 桌面快捷方式 ----------
Write-Host "[5/6] 生成桌面快捷方式..."
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutDir = Join-Path $desktop "德语直播实时字幕"
New-Item -ItemType Directory -Path $shortcutDir -Force | Out-Null

$batTemplate = @(
    @("启动字幕.bat", "scripts\windows\start_subtitles.ps1"),
    @("停止字幕.bat", "scripts\windows\stop_subtitles.ps1"),
    @("暂停继续字幕.bat", "scripts\windows\pause_subtitles.ps1"),
    @("更新字幕.bat", "scripts\windows\update_subtitles.ps1"),
    @("卸载字幕.bat", "scripts\windows\uninstall.ps1")
)
foreach ($pair in $batTemplate) {
    $head = "@echo off`r`nchcp 65001 >nul`r`npowershell -NoProfile -ExecutionPolicy Bypass -File `"$RepoRoot\$($pair[1])`"`r`n"
    if ($pair[0] -eq "启动字幕.bat") {
        # 成功≈3秒自动关；失败保留窗口让人看得到报错（报错文本由ps1打印）。
        # ⚠️ bat 必须纯 ASCII：chcp 65001 下 cmd 解析含中文的行会把下一行开头吃掉。
        # 用 ping 当 sleep：timeout.exe 在 stdin 被重定向时直接报错
        $tail = "if errorlevel 1 goto :err`r`nping -n 4 127.0.0.1 >nul`r`nexit /b 0`r`n:err`r`necho.`r`npause`r`n"
    } elseif ($pair[0] -eq "更新字幕.bat" -or $pair[0] -eq "卸载字幕.bat") {
        # 这两个窗口一律保留：更新要看到"更新了什么"，卸载要看到"删了什么/剩什么"
        $tail = "echo.`r`npause`r`n"
    } else {
        $tail = "ping -n 3 127.0.0.1 >nul`r`n"
    }
    [System.IO.File]::WriteAllText((Join-Path $shortcutDir $pair[0]), $head + $tail, (New-Object System.Text.UTF8Encoding $false))
}
# 操作说明从仓库模板生成（单一真相源）。以前正文内联写在这里，加了
# Ctrl+Alt+M/G、📺电视全屏、点词查词、模式系统之后一直没同步——而且这个
# 文件每次跑 install 都会覆盖桌面上的那份，等于把新功能说明覆盖没了
$docTemplate = Join-Path $RepoRoot "docs\zh\user-guide-template.txt"
$docTarget = Join-Path $shortcutDir "操作说明.txt"
if (Test-Path $docTemplate) {
    (Get-Content $docTemplate -Raw -Encoding UTF8).Replace("{{INSTALL_DIR}}", $RepoRoot) |
        Out-File -FilePath $docTarget -Encoding utf8
} else {
    # 老版本仓库没有模板文件时的兜底（更新到新版后会自动用上模板）
    @"
双击"启动字幕.bat"开始：字幕悬浮窗几秒内出现（带加载提示），模型在
后台加载 10-30 秒后就绪（首次启动还会自动下载语音识别模型，需要几分钟）。
播放任何德语视频/直播，悬浮窗里就会出现双语字幕。

悬浮窗操作：鼠标拖动移动/边缘缩放；➖最小化 📜历史 📺电视全屏 ⚙️设置 ❌退出。
全局快捷键：Ctrl+Alt+P 暂停/继续，Ctrl+Alt+L 切换语言，
            Ctrl+Alt+M 鼠标穿透，Ctrl+Alt+G 切「性能」模式。
单击字幕里的德语单词 = 查词。

看完后双击"停止字幕.bat"关闭。字幕记录在程序目录的 transcripts\ 里。
程序有新版本时双击"更新字幕.bat"即可，个人配置和字幕记录不受影响。
"@ | Out-File -FilePath $docTarget -Encoding utf8
}
Write-Host "  ✅ 快捷方式已生成: $shortcutDir"

# ---------- 7. 装完自检 ----------
# 装完不验一下的话，环境问题要等用户双击启动、悬浮窗不出来、再去翻
# subtitle.err.log 才暴露。这里当场把最容易坏的四类依赖走一遍：
# torch(c10.dll) / PyQt5 / 音频捕获 / 重采样，再走一次 translator_queue
# 自己的 _ensure_ml_deps()——☠️ 必须走它，不能直接 import faster_whisper：
# cublas 目录是那个函数往 PATH 里注入的（避坑清单第 2 条），绕过去测会在
# 装得好好的机器上误报失败。
Write-Host "[6/6] 验证安装..."
$smokeCode = @"
import config, torch, PyQt5.QtWidgets, pyaudiowpatch, soxr
import translator_queue
translator_queue._ensure_ml_deps()
print('SMOKE_OK', config.WHISPER_MODEL, config.WHISPER_COMPUTE_TYPE, config.OLLAMA_MODEL)
"@
$smoke = & $vpy -c $smokeCode 2>&1
if ($LASTEXITCODE -eq 0 -and ($smoke -join "`n") -match "SMOKE_OK\s+(\S+)\s+(\S+)\s+(\S+)") {
    Write-Host "  ✅ 依赖自检通过：识别 $($Matches[1]) / $($Matches[2])，翻译 $($Matches[3])"
} else {
    Write-Host "  ⚠️  依赖自检没通过，程序大概率起不来。报错如下："
    $smoke | Select-Object -Last 12 | ForEach-Object { Write-Host "     $_" }
    Write-Host "     常见原因：显卡驱动 CUDA <12.0、杀毒软件删了 venv 里的文件、"
    Write-Host "     依赖没装全（重跑本脚本即可）。对照 CLAUDE.md 第 4 节避坑清单。"
}

Write-Host ""
Write-Host "=========================================="
Write-Host "🎉 安装完成！"
Write-Host "   双击桌面 [德语直播实时字幕\启动字幕.bat] 开始使用"
Write-Host "   （首次启动会下载语音识别模型，请耐心等待）"
Write-Host "=========================================="
