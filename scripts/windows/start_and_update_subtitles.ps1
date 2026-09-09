# ============================================================
# 启动并更新字幕 —— 一次双击 = 拉最新版 + 起字幕
#
# 本脚本**只做流程编排**，三段实际工作全部交给现成脚本：
#   update_subtitles.ps1 →（必要时）stop_subtitles.ps1 → start_subtitles.ps1
# 一行业务逻辑都不复制过来。那三个脚本里每一条 ☠️ 注释都是真踩出来的，
# 抄一份到这里只会漂移成两套行为。
#
# ☠️ 必须用子进程调（& powershell -File），不能点源，两个理由都是硬的：
#   1. 那三个脚本里到处是 `exit 1`。点源的话它们的 exit 会把**本脚本**一起
#      退掉，后面的步骤根本跑不到——而"更新失败也要照常启动"正是本脚本
#      存在的全部理由。
#   2. git pull 可能刚好换掉 start_subtitles.ps1 自己。PowerShell 是在调用
#      那一刻才读脚本文件的，所以子进程拿到的是**刚拉下来的新版**；点源则
#      本进程早已解析完毕，等于拿旧逻辑去启动新代码。
#      （本文件被 pull 换掉不受影响：-File 启动时整份已解析进内存，
#        这一趟跑完的是旧的，下一趟才是新的。）
#
# 行为约定：
#   - 更新失败（断网 / git 冲突 / 压根不是 git 克隆装的）**不中断**，
#     打印原因后用当前已安装的版本照常启动。
#   - 只有"真拉到了新代码"且"字幕正在跑"时才停掉重启。没拉到新代码就
#     一根手指都不碰它。
# ============================================================
param(
    [switch]$Mirror  # 大陆网络：依赖同步走清华 PyPI 镜像（原样透传给 update_subtitles.ps1）
)

# ☠️ 这里刻意是 Continue，和同目录其它脚本的 "Stop" 相反，别"顺手改统一"。
# 本脚本的立身之本就是"子步骤失败也要继续走完"，而 Stop 之下外部命令
# （git、子 powershell）往 stderr 写一行就可能被当成终止错误抛出来，
# 那正好把唯一要防的事情变成必然发生的事情。所有外部调用的结果下面都
# 显式判了退出码，不依赖异常。
$ErrorActionPreference = "Continue"

# Repo root (this file lives in scripts/windows/)
$RepoRoot = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Set-Location $RepoRoot

# git 不可用、或这份不是 git 克隆装的（解压 zip）时返回 $null。调用方据此
# 放弃"代码变没变"的判断，而不是把两个 $null 判成"没变"或者把异常甩给用户。
function Get-HeadCommit {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) { return $null }
    if (-not (Test-Path "$RepoRoot\.git")) { return $null }
    $head = (& git rev-parse HEAD 2>$null)
    if ($LASTEXITCODE -ne 0) { return $null }
    if (-not $head) { return $null }
    return ([string]$head).Trim()
}

function Invoke-Sibling {
    param([string]$Name, [string[]]$Extra = @())
    $script = Join-Path $PSScriptRoot $Name
    # ☠️ 子脚本的输出必须 Out-Host，不能让它留在管道里。PowerShell 函数的
    # 返回值是"管道里的所有东西"，直接 return $LASTEXITCODE 拿到的会是
    # 「几十行文本 + 退出码」的数组，后面 `-ne 0` 的判断随即失效——而且
    # 失效方向是"永远认为失败"，正常更新也会被报成更新失败。
    & powershell -NoProfile -ExecutionPolicy Bypass -File $script @Extra | Out-Host
    return $LASTEXITCODE
}

# ---------- 1. 更新 ----------
$before = Get-HeadCommit

$updateArgs = @()
if ($Mirror) { $updateArgs += "-Mirror" }
$updateCode = Invoke-Sibling "update_subtitles.ps1" $updateArgs

if ($updateCode -ne 0) {
    Write-Host ""
    Write-Host "⚠️ 更新没能完成（原因见上面几行），改用当前已装好的版本继续启动。"
    Write-Host "   常见原因：断网、直接改过仓库文件导致冲突、这份不是 git 克隆装的。"
    Start-Sleep -Seconds 3
}

$after = Get-HeadCommit
# 两头都拿到 commit 才敢说"代码变了"。任何一头拿不到就一律当作没变：
# 宁可少重启一次（用户手动停一下就是了），也不能因为判断不了就去掐断
# 人家正在看的字幕。
$codeChanged = $before -and $after -and ($before -ne $after)

# ---------- 2. 需要的话先停掉旧进程 ----------
# ☠️ 点源放在 pull 之后：判"这个进程是不是本项目实例"的规则可能刚随这次
# 更新变了，这里要用新版判据。Test-RealtimeInstance 校验解释器路径 +
# main.py 入口 + 创建时间三项，光看 PID 会被系统回收复用坑到。
. "$PSScriptRoot\_identity.ps1"

$pidFile = "$RepoRoot\subtitle.pid"
$running = $false
if (Test-Path $pidFile) {
    $identity = Read-SubtitleIdentity $pidFile
    $runPid = if ($identity) { $identity.pid } else { $null }
    $runProc = if ($runPid) { Get-Process -Id $runPid -ErrorAction SilentlyContinue } else { $null }
    $running = Test-RealtimeInstance $runProc $identity $RepoRoot
}

if ($codeChanged -and $running) {
    Write-Host ""
    Write-Host "拉到了新版本，正在停掉当前字幕好让新版生效..."
    Write-Host "（识别模型和翻译模型都要重新加载，字幕会中断半分钟左右）"
    Invoke-Sibling "stop_subtitles.ps1" | Out-Null
} elseif ($running) {
    Write-Host ""
    Write-Host "字幕已经在运行，而且没有拉到新代码——不打断它。"
}

# ---------- 3. 启动 ----------
Write-Host ""
$startCode = Invoke-Sibling "start_subtitles.ps1"

if ($startCode -eq 0 -and $codeChanged) {
    # 有更新时多留几秒，让人读得完上面 update 打的那段 git log。
    # ☠️ 这个停顿只能写在 ps1 里：桌面 .bat 必须纯 ASCII（CLAUDE.md 第 4 节
    # 第 4 条），而且"这次到底有没有更新"也只有这里知道。
    Start-Sleep -Seconds 5
}

exit $startCode
