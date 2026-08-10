# ============================================================
# 实时字幕翻译系统 卸载 / 清理脚本（Windows）
#
# 用法：powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1
#   或： powershell -ExecutionPolicy Bypass -File scripts\windows\uninstall.ps1 -CleanCache
#
# 为什么需要这个脚本：这套系统装完摊在四个位置，其中三个在仓库目录之外
# （HuggingFace 模型缓存、Ollama 模型、Ollama 程序本体）。只删仓库目录的话
# 十几 GB 里只能收回 venv 那几 GB，剩下的会静默留在 C 盘——而想卸载的人
# 通常正是嫌它占地方。所以这里逐项列出、逐项问。
#
# 原则：
#   - 每一步先报体积再问 Y/N，默认 N，不做"一把梭全删"
#   - 只碰这个项目自己产生的东西（HuggingFace 缓存里只动 faster-whisper 的模型）
#   - 用户数据（字幕存档/个人配置/窗口位置）默认保留，只告诉在哪
# ============================================================
param(
    # 只清垃圾不卸载：删中断下载的 .incomplete 残file + 没有完整模型的空壳目录。
    # 给"还要继续用，只是想腾点空间"的场景。
    [switch]$CleanCache
)
$ErrorActionPreference = "Stop"
# Repo root (this file lives in scripts/windows/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot

# 体积按量级选单位：残file经常只有几百 KB，一律按 GB 显示会变成一串"0 GB"，
# 看着像脚本坏了
function Format-Size {
    param([double]$Bytes)
    if ($Bytes -ge 1GB) { return ("{0:N2} GB" -f ($Bytes / 1GB)) }
    if ($Bytes -ge 1MB) { return ("{0:N1} MB" -f ($Bytes / 1MB)) }
    if ($Bytes -ge 1KB) { return ("{0:N0} KB" -f ($Bytes / 1KB)) }
    return ("{0} B" -f [int]$Bytes)
}

function Get-SizeBytes {
    param([string]$Path)
    if (-not (Test-Path -LiteralPath $Path)) { return 0 }
    $sum = (Get-ChildItem -LiteralPath $Path -Recurse -File -ErrorAction SilentlyContinue |
            Measure-Object -Property Length -Sum).Sum
    if (-not $sum) { return 0 }
    return $sum
}

function Confirm-Step {
    param([string]$Message)
    # 默认 N：回车 = 不删。卸载脚本手滑的代价是重下十几 GB，不值得图省事
    $ans = Read-Host "$Message  [y/N]"
    return ($ans -eq "y" -or $ans -eq "Y")
}

function Remove-Path {
    param([string]$Path, [string]$Label)
    try {
        Remove-Item -LiteralPath $Path -Recurse -Force -ErrorAction Stop
        Write-Host "     ✅ 已删除 $Label"
    } catch {
        Write-Host "     ❌ 删除失败 $Label : $_"
        Write-Host "        （文件可能被占用：先关掉字幕程序和资源管理器窗口再重试）"
    }
}

# HuggingFace 缓存位置：HF_HOME 优先，其次默认路径。
# 只在这个 hub 目录里找 faster-whisper 的模型，绝不整个清空——
# 用户很可能有别的项目在共用这个缓存。
function Get-HFHubDir {
    if ($env:HF_HOME) { return (Join-Path $env:HF_HOME "hub") }
    return (Join-Path $env:USERPROFILE ".cache\huggingface\hub")
}

Write-Host ""
Write-Host "=========================================="
if ($CleanCache) {
    Write-Host "   实时字幕 - 缓存清理（不卸载）"
} else {
    Write-Host "   实时字幕 - 卸载"
}
Write-Host "=========================================="
Write-Host ""

$hubDir = Get-HFHubDir

# ---------- 清理模式：只删中断下载的残file和空壳目录 ----------
if ($CleanCache) {
    Write-Host "[1/2] 扫描中断的下载残file（*.incomplete）..."
    $freed = 0.0
    if (Test-Path -LiteralPath $hubDir) {
        $incomplete = @(Get-ChildItem -LiteralPath $hubDir -Recurse -File -Filter "*.incomplete" -ErrorAction SilentlyContinue)
        if ($incomplete.Count -gt 0) {
            $bytes = ($incomplete | Measure-Object -Property Length -Sum).Sum
            Write-Host "  发现 $($incomplete.Count) 个残file，合计 $(Format-Size $bytes)："
            foreach ($f in $incomplete) {
                Write-Host "     $(Format-Size $f.Length)`t$($f.Name)"
            }
            if (Confirm-Step "  删掉这些残file？（下次要用会重新下载）") {
                foreach ($f in $incomplete) { Remove-Item -LiteralPath $f.FullName -Force -ErrorAction SilentlyContinue }
                $freed += $bytes
                Write-Host "     ✅ 已清理"
            }
        } else {
            Write-Host "  ✅ 没有残file"
        }
    } else {
        Write-Host "  ℹ️ 没有 HuggingFace 缓存目录，跳过"
    }

    Write-Host "[2/2] 扫描没有完整模型的空壳目录..."
    # 中断的下载会留下一个有 blobs/refs 但没有 snapshots/model.bin 的目录，
    # 占几 GB 却完全没用，faster-whisper 下次启动会绕过它重新下载
    $shells = @()
    if (Test-Path -LiteralPath $hubDir) {
        foreach ($d in @(Get-ChildItem -LiteralPath $hubDir -Directory -Filter "models--*faster-whisper*" -ErrorAction SilentlyContinue)) {
            $hasModel = @(Get-ChildItem -LiteralPath $d.FullName -Recurse -File -Filter "model.bin" -ErrorAction SilentlyContinue).Count -gt 0
            if (-not $hasModel) { $shells += $d }
        }
    }
    if ($shells.Count -gt 0) {
        foreach ($d in $shells) {
            $sz = Get-SizeBytes $d.FullName
            Write-Host "  $($d.Name) —— $(Format-Size $sz)，没有 model.bin（下载没完成）"
            if (Confirm-Step "  删掉它？") {
                Remove-Path $d.FullName $d.Name
                $freed += $sz
            }
        }
    } else {
        Write-Host "  ✅ 没有空壳目录"
    }

    Write-Host ""
    Write-Host "🎉 清理完成，共收回约 $(Format-Size $freed)。程序不受影响，照常用。"
    exit 0
}

# ---------- 卸载模式 ----------
# ☠️ 模型名必须在删 venv 之前读：venv 一没，import config 就跑不了了
$txModel = $null
$vpy = "$RepoRoot\venv\Scripts\python.exe"
if (Test-Path $vpy) {
    try { $txModel = (& $vpy -c "import config; print(config.OLLAMA_MODEL)" 2>$null).Trim() } catch { }
}
if (-not $txModel) {
    Write-Host "  ℹ️ 读不到配置里的翻译模型名（venv 可能已删），到时候手动选"
}

Write-Host "先看看各部分占多少地方："
Write-Host ""
$desktop = [Environment]::GetFolderPath("Desktop")
$shortcutDir = Join-Path $desktop "德语直播实时字幕"
$venvDir = "$RepoRoot\venv"

$venvBytes = Get-SizeBytes $venvDir
$whisperBytes = 0
$whisperDirs = @()
if (Test-Path -LiteralPath $hubDir) {
    $whisperDirs = @(Get-ChildItem -LiteralPath $hubDir -Directory -Filter "models--*faster-whisper*" -ErrorAction SilentlyContinue)
    foreach ($d in $whisperDirs) { $whisperBytes += (Get-SizeBytes $d.FullName) }
}
$ollamaProgDir = Join-Path $env:LOCALAPPDATA "Programs\Ollama"
$ollamaProgBytes = Get-SizeBytes $ollamaProgDir

Write-Host ("   venv（Python 依赖）        {0,10}" -f (Format-Size $venvBytes))
Write-Host ("   Whisper 识别模型缓存       {0,10}" -f (Format-Size $whisperBytes))
Write-Host  "   Ollama 翻译模型               见下（ollama list）"
Write-Host ("   Ollama 程序本体            {0,10}  ← 要单独卸" -f (Format-Size $ollamaProgBytes))
Write-Host ""

# 1. 先停掉在跑的程序，否则 venv 里的文件删不掉
Write-Host "[1/6] 停止正在运行的字幕..."
if (Test-Path "$RepoRoot\subtitle.pid") {
    try {
        & powershell -NoProfile -ExecutionPolicy Bypass -File "$PSScriptRoot\stop_subtitles.ps1" | Out-Null
        Write-Host "  ✅ 已停止"
    } catch {
        Write-Host "  ⚠️  停止脚本报错，继续（如果后面删 venv 失败，先手动关掉字幕）"
    }
} else {
    Write-Host "  ✅ 没在运行"
}

# 2. 桌面快捷方式
Write-Host "[2/6] 桌面快捷方式文件夹..."
if (Test-Path -LiteralPath $shortcutDir) {
    if (Confirm-Step "  删除「$shortcutDir」？") {
        Remove-Path $shortcutDir "桌面快捷方式"
    }
} else {
    Write-Host "  ✅ 不存在"
}

# 3. venv
Write-Host "[3/6] venv（Python 依赖，$(Format-Size $venvBytes)）..."
if (Test-Path -LiteralPath $venvDir) {
    if (Confirm-Step "  删除 venv？（想再用就重跑 install.ps1）") {
        Remove-Path $venvDir "venv"
    }
} else {
    Write-Host "  ✅ 不存在"
}

# 4. Whisper 模型缓存（单独问：可能别的项目也在用 faster-whisper）
Write-Host "[4/6] Whisper 识别模型缓存（$(Format-Size $whisperBytes)）..."
if ($whisperDirs.Count -gt 0) {
    foreach ($d in $whisperDirs) {
        Write-Host "     $($d.Name) —— $(Format-Size (Get-SizeBytes $d.FullName))"
    }
    Write-Host "  ℹ️ 只列出 faster-whisper 的模型，缓存里别的模型不会动"
    if (Confirm-Step "  删除这些模型？（重装后首次启动要重新下 1-3GB）") {
        foreach ($d in $whisperDirs) { Remove-Path $d.FullName $d.Name }
    }
} else {
    Write-Host "  ✅ 没有找到"
}

# 5. Ollama 翻译模型
Write-Host "[5/6] Ollama 翻译模型..."
$ollamaExe = (Get-Command ollama -ErrorAction SilentlyContinue).Source
if (-not $ollamaExe) {
    $guess = Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"
    if (Test-Path $guess) { $ollamaExe = $guess }
}
if ($ollamaExe) {
    & $ollamaExe list
    if ($txModel) {
        if (Confirm-Step "  删除本项目用的模型 $txModel ？") {
            & $ollamaExe rm $txModel
            Write-Host "     ✅ 已删除 $txModel"
        }
    } else {
        Write-Host "  ℹ️ 读不到配置里的模型名，需要的话自己 ollama rm <名字>"
    }
    Write-Host "  ℹ️ 上面列表里别的模型可能是你其它项目在用的，这里不动"
} else {
    Write-Host "  ✅ 没装 Ollama"
}

# 6. Ollama 程序本体：不代劳，因为很可能有别的项目在用它
Write-Host "[6/6] Ollama 程序本体（$(Format-Size $ollamaProgBytes)）..."
if (Test-Path -LiteralPath $ollamaProgDir) {
    Write-Host "  ℹ️ Ollama 是独立安装的服务，可能有别的项目在用，本脚本不代劳。"
    Write-Host "     确定不用了就自己跑： winget uninstall Ollama.Ollama"
} else {
    Write-Host "  ✅ 没装"
}

# 用户数据一律保留，只告诉在哪
Write-Host ""
Write-Host "------------------------------------------"
Write-Host "以下是你的数据，本脚本一律不动，要删自己删："
Write-Host "   字幕存档   $RepoRoot\transcripts\"
Write-Host "   个人配置   $RepoRoot\config_local.py"
Write-Host "   窗口位置   $RepoRoot\window_state.json"
Write-Host "   运行日志   $RepoRoot\subtitle.log / subtitle.err.log / logs\"
Write-Host ""
Write-Host "整个仓库目录（含以上数据）在："
Write-Host "   $RepoRoot"
Write-Host "确认都不要了，把它整个删掉即可。"
Write-Host "=========================================="
