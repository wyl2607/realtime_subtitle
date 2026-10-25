# ============================================================
# 实时字幕 - 引导安装器（Windows / PowerShell 5.1 兼容）
#
# 这是一个**几十 KB 的小引导器**：不打包 Python / torch / PyQt6，只负责把
# 运行环境拉起来，然后把重活交给仓库里现成的 scripts\windows\install.ps1。
# 这样发布物只有两个小文件，避免把数 GB 的依赖冻结进安装包。
#
# 流程（每步失败都会打印中文原因 + 手动解决办法，然后退出非 0）：
#   [1/5] 检查 winget（Windows「应用安装程序」）
#   [2/5] 用 winget 装 Git / Python 3.12 / Ollama（已装则跳过）
#   [3/5] git clone（目录已存在则 git pull）
#   [4/5] 调用 scripts\windows\install.ps1（venv、依赖、模型、桌面入口）
#   [5/5] 在桌面和开始菜单建「实时字幕」快捷方式（指向 start_and_update_subtitles.ps1）
#
# 用法（推荐双击同目录的 bootstrap.cmd，或手动）：
#   powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1
# 可选参数：
#   -Branch <名>   要拉的分支（默认 master）
#   -Dir <路径>    安装目录（默认 %USERPROFILE%\realtime_subtitle；必须纯英文）
#   -SkipOllama    跳过 Ollama 安装（自己已有或稍后单独装）
#   -DryRun        只打印将要执行的命令，不真正改动系统
#
# ☠️ 不引入任何“把下载回来的文本再当代码执行”的做法（PowerShell 动态求值）：
#    引导器的每一步都是显式拼好的命令，下载回来的代码只有 install.ps1 一份，
#    且是 git clone 下来、可审阅的。tests/test_win_bootstrap.py 盯着这条。
# ============================================================
param(
    [string]$Branch = "master",
    [string]$Dir = $(Join-Path $env:USERPROFILE "realtime_subtitle"),
    [switch]$SkipOllama,
    [switch]$DryRun
)
$ErrorActionPreference = "Stop"

# 固定仓库地址：引导器只认这一份源码，不从命令行覆盖（防手滑 / 防被引导到别处）。
$RepoUrl = "https://github.com/wyl2607/realtime_subtitle.git"

Write-Host ""
Write-Host "=========================================="
Write-Host "   实时字幕 - 引导安装器"
Write-Host "=========================================="
if ($DryRun) {
    Write-Host "   （DryRun 模式：只打印要执行的命令，不会改动系统）"
}
Write-Host ""

# 统一的失败出口：打印中文原因 + 手动解决办法，然后退出非 0。
function Fail {
    param([string]$Reason, [string[]]$Fix)
    Write-Host ""
    Write-Host "❌ $Reason"
    foreach ($line in $Fix) { Write-Host "   $line" }
    Write-Host ""
    exit 1
}

function Test-CommandExists {
    param([string]$Name)
    return [bool](Get-Command $Name -ErrorAction SilentlyContinue)
}

# ☠️ winget 刚装完的程序，在**当前进程**里 Get-Command 找不到——PATH 是进程
# 启动时快照的。所以每装完一步都要从注册表把 Machine + User 的 Path 重读一遍，
# 再继续往下走，否则后面 git/python 明明装好了却报“找不到”。
function Update-SessionPath {
    try {
        $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $user = [Environment]::GetEnvironmentVariable("Path", "User")
        $combined = @($env:Path, $machine, $user) | Where-Object { $_ }
        $seen = @{}
        $parts = @()
        foreach ($p in (($combined -join ";") -split ";")) {
            $t = $p.Trim()
            if (-not $t) { continue }
            $key = $t.ToLowerInvariant()
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = $true
                $parts += $t
            }
        }
        $env:Path = [Environment]::ExpandEnvironmentVariables(($parts -join ";"))
    } catch {
        Write-Host "   ⚠️  刷新本进程 PATH 失败（不影响后面按绝对路径查找）"
    }
}

# 用 winget 装一个包。优先 --scope user（不需要管理员）；那一档装不上再退回
# 默认范围（可能弹 UAC）。DryRun 下只打印命令。
function Install-WingetPackage {
    param([string]$Id, [string]$Label)
    $baseArgs = @("install", "--id", $Id, "-e", "--accept-package-agreements", "--accept-source-agreements")
    if ($DryRun) {
        Write-Host "    [DryRun] winget $($baseArgs -join ' ') --scope user"
        return $true
    }
    Write-Host "    正在安装 $Label（winget $Id）..."
    winget @baseArgs --scope user
    if ($LASTEXITCODE -eq 0) { return $true }
    Write-Host "    ⚠️  --scope user 没装成（退出码 $LASTEXITCODE），改用默认范围重试；可能弹出管理员授权。"
    winget @baseArgs
    return ($LASTEXITCODE -eq 0)
}

# 返回本机可用的 Python 启动说明（如 "py -3.12" / "python"），没有返回 $null。
# 只认 install.ps1 同样接受的范围：3.10 - 3.13。
# ☠️ 别用 $parts[1..($parts.Count-1)]：Count=1 时 1..0 会把自身塞回参数。
function Get-PythonSpec {
    $candidates = @()
    if (Test-CommandExists "py") {
        $candidates += @("py -3.12", "py -3.13", "py -3.11", "py -3.10")
    }
    if (Test-CommandExists "python") { $candidates += "python" }
    $code = 'import sys; print("%d.%d" % sys.version_info[:2])'
    foreach ($cand in $candidates) {
        $parts = @($cand -split "\s+" | Where-Object { $_ })
        if ($parts.Count -eq 0) { continue }
        $out = $null
        try {
            if ($parts.Count -eq 1) {
                $out = & $parts[0] -c $code 2>$null
            } else {
                $out = & $parts[0] $parts[1] -c $code 2>$null
            }
        } catch {
            $out = $null
        }
        if ($out -match "^3\.(10|11|12|13)$") { return $cand }
    }
    return $null
}

# ---------- 1. 检查系统环境 ----------
Write-Host "[1/5] 检查系统环境..."
if (-not (Test-CommandExists "winget")) {
    if ($DryRun) {
        Write-Host "  [DryRun] 本机没有 winget（真机运行会提示去 Microsoft Store 装「应用安装程序」）"
    } else {
        Fail "没有找到 winget（Windows「应用安装程序」）。" @(
            "解决：打开 Microsoft Store，搜索并安装「应用安装程序」(App Installer)，",
            "      或点这个直达链接：ms-windows-store://pdp/?productid=9NBLGGH4NNS1",
            "      装好后重新双击 bootstrap.cmd 即可。"
        )
    }
} else {
    Write-Host "  ✅ winget 可用"
}

# ☠️ 安装路径必须是纯 ASCII：桌面 .bat 会内嵌这个路径，而 cmd 在 UTF-8 代码页下
# 解析含中文的行会把下一行开头吃掉（中文用户名最常踩，install.ps1 也会拦）。
if ($Dir -match '[^\x20-\x7E]') {
    Fail "安装目录里有非英文字符，装了也起不来：" @(
        "   $Dir",
        "解决：用 -Dir 指定一个纯英文目录，例如：",
        "      powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1 -Dir C:\realtime_subtitle"
    )
}

# ---------- 2. 准备 Git / Python / Ollama ----------
Write-Host ""
Write-Host "[2/5] 准备 Git / Python / Ollama（已装就跳过）..."

if (Test-CommandExists "git") {
    Write-Host "  ✅ Git 已安装"
} else {
    if (-not (Install-WingetPackage "Git.Git" "Git")) {
        Fail "Git 安装失败。" @(
            "解决：手动到 https://git-scm.com/download/win 安装（保持默认选项），",
            "      装好后重新运行本脚本。"
        )
    }
    Update-SessionPath
    if (-not (Test-CommandExists "git") -and -not $DryRun) {
        Fail "Git 装上了但当前进程仍找不到它。" @(
            "解决：关掉本窗口，重新双击 bootstrap.cmd（让 PATH 刷新）后再试。"
        )
    }
    Write-Host "  ✅ 已安装 Git"
}

$pySpec = Get-PythonSpec
if ($pySpec) {
    Write-Host "  ✅ Python 已安装（$pySpec）"
} else {
    if (-not (Install-WingetPackage "Python.Python.3.12" "Python 3.12")) {
        Fail "Python 安装失败。" @(
            "解决：手动到 https://www.python.org/downloads/ 下载 3.12 或 3.13，",
            "      安装时务必勾选「Add python.exe to PATH」，装好后重新运行本脚本。"
        )
    }
    Update-SessionPath
    $pySpec = Get-PythonSpec
    if ($pySpec) {
        Write-Host "  ✅ 已安装 Python（$pySpec）"
    } elseif (-not $DryRun) {
        Fail "Python 装上了但当前进程仍找不到它。" @(
            "解决：关掉本窗口，重新双击 bootstrap.cmd（让 PATH 刷新）后再试；",
            "      仍不行就手动把 Python 目录加入 PATH，或用 python.org 的安装包重装并勾选 Add to PATH。"
        )
    }
}

if ($SkipOllama) {
    Write-Host "  ⏭  按 -SkipOllama 跳过 Ollama"
} else {
    $hasOllama = (Test-CommandExists "ollama") -or
        (Test-Path (Join-Path $env:LOCALAPPDATA "Programs\Ollama\ollama.exe"))
    if ($hasOllama) {
        Write-Host "  ✅ Ollama 已安装"
    } else {
        if (-not (Install-WingetPackage "Ollama.Ollama" "Ollama")) {
            Fail "Ollama 安装失败。" @(
                "解决：Ollama 安装可能需要管理员权限——若弹出 UAC 请允许后重跑。",
                "      仍不行就手动到 https://ollama.com/download 安装，再重新运行本脚本。",
                "      只想先装字幕、Ollama 稍后自己装：重跑时加 -SkipOllama。"
            )
        }
        Update-SessionPath
        Write-Host "  ✅ 已安装 Ollama"
    }
}

# ---------- 3. 获取代码 ----------
Write-Host ""
Write-Host "[3/5] 获取代码（$RepoUrl，分支 $Branch）..."
$gitDir = Join-Path $Dir ".git"
if (Test-Path $gitDir) {
    Write-Host "  ℹ️  目录已存在，执行 git pull：$Dir"
    if ($DryRun) {
        Write-Host "    [DryRun] git -C `"$Dir`" pull --ff-only"
    } else {
        git -C $Dir pull --ff-only
        if ($LASTEXITCODE -ne 0) {
            Fail "git pull 失败（网络不通、本地改过仓库文件冲突，或分支不对）。" @(
                "解决：进 $Dir 执行 git status 看看是不是改过仓库文件；",
                "      改过就先 git stash 再重跑；网络问题就检查代理后重试。"
            )
        }
        Write-Host "  ✅ 代码已更新"
    }
} elseif (Test-Path $Dir) {
    # 目录在，但不是本项目仓库：绝不覆盖用户已有内容。
    $items = @(Get-ChildItem -LiteralPath $Dir -Force -ErrorAction SilentlyContinue)
    if ($items.Count -gt 0) {
        Fail "目标目录已存在、但不是本项目的 git 仓库：$Dir" @(
            "解决：换一个空目录重跑，例如加参数 -Dir C:\realtime_subtitle；",
            "      确认这个目录没用之后再手动删除它。"
        )
    }
    if ($DryRun) {
        Write-Host "    [DryRun] git clone --branch $Branch $RepoUrl `"$Dir`""
    } else {
        git clone --branch $Branch $RepoUrl $Dir
        if ($LASTEXITCODE -ne 0) {
            Fail "git clone 失败。" @(
                "解决：检查网络/代理，或确认分支名 '$Branch' 是否存在，然后重跑。"
            )
        }
        Write-Host "  ✅ 代码已下载到 $Dir"
    }
} else {
    if ($DryRun) {
        Write-Host "    [DryRun] git clone --branch $Branch $RepoUrl `"$Dir`""
    } else {
        git clone --branch $Branch $RepoUrl $Dir
        if ($LASTEXITCODE -ne 0) {
            Fail "git clone 失败。" @(
                "解决：检查网络/代理，或确认分支名 '$Branch' 是否存在，然后重跑。"
            )
        }
        Write-Host "  ✅ 代码已下载到 $Dir"
    }
}

# ---------- 4. 调用仓库安装脚本 ----------
# 引导器**只负责把环境拉起来**：venv、Python 依赖、模型分档、桌面入口全部由
# 现成的 install.ps1 干（它是那些逻辑的唯一真相源，不要再抄一份到这）。
Write-Host ""
Write-Host "[4/5] 运行仓库安装脚本 install.ps1（venv / 依赖 / 模型分档 / 桌面入口）..."
$installer = Join-Path $Dir "scripts\windows\install.ps1"
if (-not (Test-Path $installer)) {
    Fail "仓库里没有 scripts\windows\install.ps1，代码不完整。" @(
        "解决：删掉 $Dir 后重新运行本脚本，重新 clone 一次。"
    )
}
# ☠️ 用子进程（-File）调，不点源：install.ps1 里到处是 exit 1，点源会把引导器
#    一起退掉；子进程还能让 $LASTEXITCODE 干净地反映它的成败。
$psExe = Join-Path $PSHOME $(if ($PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })
if ($DryRun) {
    Write-Host "    [DryRun] & `"$psExe`" -NoProfile -ExecutionPolicy Bypass -File `"$installer`""
} else {
    & $psExe -NoProfile -ExecutionPolicy Bypass -File $installer
    if ($LASTEXITCODE -ne 0) {
        Fail "install.ps1 没跑成功（退出码 $LASTEXITCODE）。" @(
            "解决：往上翻看它的报错，常见原因——",
            "  · 显卡驱动太旧（CUDA < 12.0）：升级 NVIDIA 驱动后重跑；",
            "  · 下载依赖失败（中国大陆网络）：直接跑 scripts\windows\install.ps1 -Mirror；",
            "  · 杀毒软件拦了 venv 里的文件：加白名单后重跑。"
        )
    }
    Write-Host "  ✅ install.ps1 完成"
}

# ---------- 5. 快捷方式 ----------
# install.ps1 已经在桌面建了「德语直播实时字幕」文件夹（含五个 .bat 入口）。
# 这一步额外建一个顶层「实时字幕」快捷方式，直接指向 start_and_update_subtitles.ps1，
# 方便双击即用（它每次会先自动更新）。属于锦上添花：失败只警告，不改退出码。
Write-Host ""
Write-Host "[5/5] 创建「实时字幕」快捷方式（桌面 + 开始菜单）..."
$target = Join-Path $Dir "scripts\windows\start_and_update_subtitles.ps1"
$lnkArgs = "-NoProfile -ExecutionPolicy Bypass -File `"$target`""
$folders = @(
    [Environment]::GetFolderPath("Desktop"),
    [Environment]::GetFolderPath("Programs")
)
foreach ($folder in $folders) {
    if (-not $folder) { continue }
    $lnk = Join-Path $folder "实时字幕.lnk"
    if ($DryRun) {
        Write-Host "    [DryRun] 创建快捷方式 $lnk → $target"
        continue
    }
    try {
        $shell = New-Object -ComObject WScript.Shell
        $sc = $shell.CreateShortcut($lnk)
        $sc.TargetPath = $psExe
        $sc.Arguments = $lnkArgs
        $sc.WorkingDirectory = $Dir
        $sc.Description = "实时字幕：每次先自动更新再启动"
        $sc.Save()
        Write-Host "  ✅ $lnk"
    } catch {
        Write-Host "  ⚠️  创建快捷方式失败（不影响使用，桌面「德语直播实时字幕」文件夹仍在）：$lnk"
    }
}

Write-Host ""
Write-Host "=========================================="
Write-Host "🎉 引导安装完成！"
Write-Host "   双击桌面/开始菜单的「实时字幕」即可启动（每次先自动更新）。"
Write-Host "   完整入口也在桌面的「德语直播实时字幕」文件夹里。"
Write-Host "   首次启动会自动下载语音识别模型（1-3GB），请耐心等待。"
Write-Host "=========================================="
