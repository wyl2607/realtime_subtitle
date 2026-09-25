# ============================================================
# 启动字幕 —— 一次双击 = 拉最新版 + 起字幕
#
# ☠️ 文件名里还留着 and_update，但它现在就是**唯一的**启动入口：桌面上那个
# 「启动字幕.bat」指向的就是这里（2026-09-20 把「启动」「更新」「启动并更新」
# 三个桌面入口合成了一个，见 install.ps1 的 $batTemplate）。
# 文件没跟着改名，是因为名字描述的是它**做什么**（先更新再启动），而
# start_subtitles.ps1 这个名字已经被它调用的那个脚本占着了。
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
    # 用**当前这个解释器**起子脚本：bat 挑了 pwsh 就一路 pwsh，别半路掉回 5.1
    $exe = Join-Path $PSHOME $(if ($PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })
    # ☠️ 不许经过管道（以前是 `& $exe -File $script | Out-Host`）。pwsh 7 下
    # start_subtitles.ps1 用 Start-Process 拉起的 python 会**继承这根管道的写端**，
    # 于是 Out-Host 要等到字幕程序退出才读到 EOF——启动字幕.bat 的黑窗口在字幕
    # 开着的整个期间都关不掉（2026-09-26 切到 pwsh 当天真机撞上；5.1 不继承，
    # 所以以前没暴露）。现在子脚本直接写控制台，函数的管道里只剩退出码。
    # ☠️ 也不能用 Start-Process -Wait：它等的是**整棵进程树**，常驻的字幕进程
    # 同样会把它挂住。WaitForExit() 只等子脚本自己。
    $argLine = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$script`"") + $Extra
    $proc = Start-Process -FilePath $exe -ArgumentList $argLine -NoNewWindow -PassThru
    $null = $proc.Handle  # 5.1：不先摸一下 Handle，退出后 ExitCode 读出来是 $null
    $proc.WaitForExit()
    return $proc.ExitCode
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

# pid 文件丢了也要认得出来：否则拉到新代码时旧实例不会被停，而启动脚本又
# 会因为"已经在运行"拒绝启动——新代码永远上不去（见 Get-RunningRealtimeInstance）
$running = [bool](Get-RunningRealtimeInstance $RepoRoot)

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
