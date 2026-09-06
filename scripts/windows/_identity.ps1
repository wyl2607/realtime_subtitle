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

if (-not ('RealtimeSubtitle.Native' -as [type])) {
    Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;
namespace RealtimeSubtitle {
    public static class Native {
        [DllImport("shell32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        public static extern IntPtr CommandLineToArgvW(string lpCmdLine, out int pNumArgs);
        [DllImport("kernel32.dll")]
        public static extern IntPtr LocalFree(IntPtr hMem);
    }
}
"@
}

function Split-WindowsCommandLine {
    param($CommandLine)
    if (-not $CommandLine) { return @() }
    $argc = 0
    $ptr = [IntPtr]::Zero
    try {
        $ptr = [RealtimeSubtitle.Native]::CommandLineToArgvW([string]$CommandLine, [ref]$argc)
        if ($ptr -eq [IntPtr]::Zero -or $argc -le 0) { return @() }
        $args = @()
        for ($i = 0; $i -lt $argc; $i++) {
            $itemPtr = [Runtime.InteropServices.Marshal]::ReadIntPtr($ptr, $i * [IntPtr]::Size)
            $args += [Runtime.InteropServices.Marshal]::PtrToStringUni($itemPtr)
        }
        return $args
    } finally {
        if ($ptr -ne [IntPtr]::Zero) {
            [RealtimeSubtitle.Native]::LocalFree($ptr) | Out-Null
        }
    }
}

function Get-PythonEntryScript {
    param($CommandLine)
    $argv = @(Split-WindowsCommandLine $CommandLine)
    if ($argv.Count -lt 2) { return $null }
    for ($i = 1; $i -lt $argv.Count; $i++) {
        $arg = [string]$argv[$i]
        if ($arg -eq '--') {
            if ($i + 1 -lt $argv.Count) { return [string]$argv[$i + 1] }
            return $null
        }
        if ($arg -eq '-') { return $null }
        if ($arg.StartsWith('--')) {
            if ($arg -eq '--check-hash-based-pycs') { $i++ }
            continue
        }
        if ($arg.StartsWith('-c') -or $arg.StartsWith('-m')) { return $null }
        if ($arg -eq '-W' -or $arg -eq '-X') { $i++; continue }
        if ($arg.StartsWith('-')) { continue }
        return $arg
    }
    return $null
}

function Test-RealtimeCommandLine {
    param($CommandLine, $RepoRoot)
    $script = Get-PythonEntryScript $CommandLine
    if (-not $script) { return $false }
    $name = [IO.Path]::GetFileName($script)
    if ($name -and $name.Equals('download_subtitle.py', [StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    if (-not ($name -and $name.Equals('main.py', [StringComparison]::OrdinalIgnoreCase))) {
        return $false
    }
    $bare = ($script -eq $name)
    if ($bare) { return $true }
    if (-not $RepoRoot) { return $false }
    if (-not [IO.Path]::IsPathRooted($script)) { return $false }
    try {
        $actual = [IO.Path]::GetFullPath($script)
        $expected = [IO.Path]::GetFullPath((Join-Path $RepoRoot 'main.py'))
        return $actual.Equals($expected, [StringComparison]::OrdinalIgnoreCase)
    } catch {
        return $false
    }
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
    if (-not (Test-RealtimeCommandLine $cmd $RepoRoot)) { return $false }
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
