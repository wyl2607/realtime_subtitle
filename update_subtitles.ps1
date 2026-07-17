# ============================================================
# 一键更新：从 GitHub 拉最新版 + 按需同步依赖
#
# 不会动的东西（都不在 git 里）：
#   config_local.py（个人配置）/ window_state.json（窗口位置）/ transcripts\（字幕记录）
# 更新后需要重启字幕（停止字幕.bat → 启动字幕.bat）才生效。
# ============================================================
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot

if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
    Write-Host "❌ 没有安装 git，无法自动更新。"
    Write-Host "   安装：winget install --id Git.Git -e   （或 https://git-scm.com/download/win）"
    exit 1
}
if (-not (Test-Path "$PSScriptRoot\.git")) {
    Write-Host "❌ 本目录不是 git 克隆（可能当初是解压 zip 装的），无法增量更新。"
    Write-Host "   建议重装：git clone https://github.com/wyl2607/realtime_subtitle.git"
    Write-Host "   然后运行 install.ps1（个人配置 config_local.py 可以直接拷过去）"
    exit 1
}

$old = (git rev-parse HEAD).Trim()
Write-Host "正在检查更新..."
git pull --ff-only
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "❌ 更新失败。最常见原因：本地直接改过仓库文件，与新版本冲突。"
    Write-Host "   个人调参请写在 config_local.py（永远不会冲突），不要直接改 config.py。"
    Write-Host "   处理办法：把上面的报错原样发给你的 Claude Code / AI 助手；"
    Write-Host "   或手动运行 git stash 暂存本地改动后重试。"
    exit 1
}
$new = (git rev-parse HEAD).Trim()
if ($old -eq $new) {
    Write-Host "✅ 已经是最新版本，无需更新。"
    exit 0
}

Write-Host ""
Write-Host "本次更新内容："
git log --oneline --no-decorate "$old..$new"
Write-Host ""

# requirements.txt 变了才重装依赖（没变就不浪费时间）
$changed = git diff --name-only $old $new
if ($changed -contains "requirements.txt") {
    Write-Host "依赖清单有变化，正在同步（可能需要几分钟）..."
    & "$PSScriptRoot\venv\Scripts\python.exe" -m pip install -r "$PSScriptRoot\requirements.txt"
    if ($LASTEXITCODE -ne 0) {
        Write-Host "❌ 依赖安装失败，请检查网络后重跑本脚本（国内网络可先跑 install.ps1 -Mirror）"
        exit 1
    }
}
if ($changed -contains "install.ps1") {
    Write-Host "ℹ️ 安装脚本本身有更新，建议重跑一次 install.ps1（会刷新桌面快捷方式/本机配置检测）"
}

# 字幕正在运行的话提醒重启
$pidFile = "$PSScriptRoot\subtitle.pid"
if (Test-Path $pidFile) {
    $runPid = Get-Content $pidFile
    if (Get-Process -Id $runPid -ErrorAction SilentlyContinue) {
        Write-Host "⚠️ 字幕正在运行——先双击 停止字幕.bat 再 启动字幕.bat，新版本才生效。"
    }
}
Write-Host ""
Write-Host "🎉 更新完成。"
