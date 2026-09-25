# 依赖同步前先把 venv 腾出来。供 update_subtitles.ps1 点源（需要先点源 _identity.ps1）。
#
# ☠️ Windows 上**正在被加载的 DLL/.pyd 不能被替换**。字幕开着时 torch /
# ctranslate2 / PyQt6 的二进制都被它占着，`pip install -r` 升级这些包会在半路
# 报 WinError 5（拒绝访问）。以前的顺序是 更新(含 pip) → 才停字幕：pip 失败、
# 指纹不写，而 start_and_update_subtitles.ps1 照样按"拉到了新代码"去停掉字幕、
# 用**新代码 + 旧依赖**把它拉起来。所以依赖要动时必须先停。
#
# 只停**实时字幕**（有 subtitle.pid 身份、停止脚本本来就管它）。同一个 venv
# 跑着的别的东西——典型是 YouTube 下载加字幕的离线任务，可能已经跑了半小时——
# 绝不替用户杀，只报出来、这次不装依赖。

function Get-VenvPythonUsers {
    param($RepoRoot)
    # 认的是 venv 启动器存根（镜像就是 venv\Scripts\python.exe）：真正的解释器
    # 是它的子进程，存根活着 ⇔ 子进程活着（见 stop_subtitles.ps1 末尾那段）
    return @(Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
        Where-Object {
            ($_.Name -eq 'python.exe' -or $_.Name -eq 'pythonw.exe') -and
            $_.ProcessId -ne $PID -and
            $_.ExecutablePath -and (Test-OurInterpreter $_.ExecutablePath $RepoRoot)
        })
}

function Request-VenvForPip {
    param(
        $RepoRoot,
        $StopScript = (Join-Path $PSScriptRoot "stop_subtitles.ps1"),
        [int]$SettleSeconds = 5
    )
    $stopped = $false
    # pid 文件丢了也要认得出来（见 _identity.ps1 的 Get-RunningRealtimeInstance），
    # 否则实时字幕会被当成"别的进程"挡住 pip，而不是被停掉
    if (Get-RunningRealtimeInstance $RepoRoot) {
        Write-Host "依赖要更新，而字幕正在运行（它占着 torch/PyQt6 等文件，pip 替换不了）——先停掉字幕..."
        & (Join-Path $PSHOME $(if ($PSEdition -eq 'Core') { 'pwsh.exe' } else { 'powershell.exe' })) -NoProfile -ExecutionPolicy Bypass -File $StopScript | Out-Host
        $stopped = $true
    }
    # 停止脚本自己会等残留退干净；这里再给一小段宽限，别被收尾中的进程误报
    $users = @()
    for ($i = 0; $i -lt ($SettleSeconds * 4); $i++) {
        $users = Get-VenvPythonUsers $RepoRoot
        if ($users.Count -eq 0) { break }
        Start-Sleep -Milliseconds 250
    }
    return [pscustomobject]@{
        Ok              = ($users.Count -eq 0)
        StoppedRealtime = $stopped
        Blockers        = @($users | ForEach-Object { $_.ProcessId })
    }
}
