# 实时字幕进程身份：精确解释器路径 + 入口命令行 + 创建时间
# 供 start/stop/update 点源。窗口标题只用来发现候选，不能单独授权强杀。
# 裸 StartsWith(venv) 会误匹配 venv_backup，所以这里用精确 python.exe 路径。

function Get-VenvPythonExe {
    param($RepoRoot)
    return [IO.Path]::GetFullPath((Join-Path $RepoRoot "venv\Scripts\python.exe"))
}

function Test-OurInterpreter {
    param($ExePath, $RepoRoot)
    if (-not $ExePath) { return $false }
    try {
        $actual = [IO.Path]::GetFullPath($ExePath)
        $py = Get-VenvPythonExe $RepoRoot
        $pyw = [IO.Path]::GetFullPath((Join-Path $RepoRoot "venv\Scripts\pythonw.exe"))
        return $actual.Equals($py, [StringComparison]::OrdinalIgnoreCase) -or
            $actual.Equals($pyw, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
}

function Test-RealtimeCommandLine {
    param($CommandLine)
    if (-not $CommandLine) { return $false }
    if ($CommandLine -match 'download_subtitle\.py') { return $false }
    return [bool]($CommandLine -match '(^|[\\/\s])main\.py(\s|$)')
}

function Read-SubtitleIdentity {
    param($Path)
    if (-not (Test-Path $Path)) { return $null }
    $raw = ((Get-Content $Path -Raw -ErrorAction SilentlyContinue) + "").Trim()
    if (-not $raw) { return $null }
    try {
        return $raw | ConvertFrom-Json
    } catch {
        if ($raw -match '^\d+$') {
            return [pscustomobject]@{ pid = [int]$raw }
        }
        return $null
    }
}

function Get-ProcessCommandLine {
    param([int]$ProcessId)
    try {
        $cim = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction SilentlyContinue
        if ($cim) { return $cim.CommandLine }
    } catch {}
    return $null
}

function Get-ProcessStartFileTime {
    param($Proc)
    try {
        return [string]$Proc.StartTime.ToFileTimeUtc()
    } catch {
        return $null
    }
}

function Test-RealtimeInstance {
    param($Proc, $Identity, $RepoRoot)
    if (-not $Proc) { return $false }
    # A legacy bare PID may be parsed for cleanup, but it lacks enough identity
    # to authorize a kill. Window-title discovery passes $null and is validated
    # from the live process instead.
    if ($Identity -and (-not $Identity.start_time -or -not $Identity.exe -or -not $Identity.command_line)) {
        return $false
    }
    $cmd = Get-ProcessCommandLine -ProcessId $Proc.Id
    if (-not (Test-OurInterpreter $Proc.Path $RepoRoot)) { return $false }
    if (-not (Test-RealtimeCommandLine $cmd)) { return $false }
    if ($Identity -and $Identity.pid -and ([int]$Identity.pid -ne $Proc.Id)) {
        return $false
    }
    if ($Identity -and $Identity.start_time) {
        $liveStart = Get-ProcessStartFileTime $Proc
        if (-not $liveStart -or $Identity.start_time -ne $liveStart) {
            return $false
        }
    }
    return $true
}

function Write-SubtitleIdentity {
    param($Proc, $PidFile, $RepoRoot)
    $cmd = Get-ProcessCommandLine -ProcessId $Proc.Id
    if (-not $cmd) { $cmd = "$($Proc.Path) -u main.py" }
    $obj = [ordered]@{
        pid          = $Proc.Id
        start_time   = Get-ProcessStartFileTime $Proc
        exe          = $Proc.Path
        command_line = $cmd
        kind         = "realtime"
    }
    ($obj | ConvertTo-Json) | Set-Content -Path $PidFile -Encoding ascii
}
