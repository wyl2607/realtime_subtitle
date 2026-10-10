# Target-side half of scripts/node/install_node_windows.sh (run on the Windows node).
# ASCII only: Windows PowerShell 5.1 reads BOM-less files as ANSI (CLAUDE.md section 4 item 5).
#
# Every action prints KEY=VALUE lines on success and exits non-zero on failure.
# The token never appears in argv or output: WriteToken reads it from stdin.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Preflight', 'NodeId', 'ReadToken', 'WriteToken', 'Build', 'SelfCheck', 'Swap', 'Rollback')]
    [string]$Action,
    [int]$Port = 8791,
    [string]$OllamaModel = 'qwen3.5:4b'
)
$ErrorActionPreference = 'Stop'

$Home_ = $env:USERPROFILE
$Cur = Join-Path $Home_ 'rs-node'
$New = Join-Path $Home_ 'rs-node.new'
$Prev = Join-Path $Home_ 'rs-node.prev'
$Failed = Join-Path $Home_ 'rs-node.failed'
$State = Join-Path $env:LOCALAPPDATA 'rs-node'
$TokenFile = Join-Path $State 'token'
$TaskName = 'rs-node-gateway'
$RuleName = 'rs-node-gateway-tailscale'

function Ensure-StateDir {
    if (-not (Test-Path $State)) { New-Item -ItemType Directory -Path $State | Out-Null }
}

# Only the current user (and SYSTEM, which can read everything anyway) may touch the file.
function Lock-Down([string]$Path) {
    & icacls $Path /inheritance:r /grant:r "$($env:USERNAME):F" /grant:r 'SYSTEM:F' | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "icacls failed on $Path" }
}

function Stop-Gateway {
    if (Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) {
        Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue
    }
    # Kill by exact command line, never by image name: other python/ollama processes live here too.
    Get-CimInstance Win32_Process -Filter "Name='pythonw.exe' OR Name='python.exe'" |
        Where-Object { $_.CommandLine -and ($_.CommandLine -like '*run_gateway.py*' -or $_.CommandLine -like '*realtime_subtitle.node.worker*') -and $_.CommandLine -like "*$Cur*" } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
}

function Register-Gateway {
    $py = Join-Path $Cur '.venv\Scripts\pythonw.exe'
    $runner = Join-Path $Cur 'scripts\node\windows\run_gateway.py'
    $action = New-ScheduledTaskAction -Execute $py -Argument "`"$runner`" --token-file `"$TokenFile`"" -WorkingDirectory $Cur
    # Logon trigger + interactive principal: CUDA and the idle-time probe need the user session.
    $trigger = New-ScheduledTaskTrigger -AtLogOn -User $env:USERNAME
    $principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
    $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) `
        -MultipleInstances IgnoreNew -StartWhenAvailable
    Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
}

function Ensure-Firewall {
    $rule = Get-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue
    if ($rule) {
        Set-NetFirewallRule -Name $RuleName -Direction Inbound -Action Allow -Enabled True -Profile Any
        Get-NetFirewallRule -Name $RuleName | Set-NetFirewallPortFilter -Protocol TCP -LocalPort $Port
        Get-NetFirewallRule -Name $RuleName | Set-NetFirewallAddressFilter -RemoteAddress '100.64.0.0/10'
    } else {
        New-NetFirewallRule -Name $RuleName -DisplayName 'Realtime Subtitle node (Tailscale only)' `
            -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress '100.64.0.0/10' -Action Allow -Profile Any | Out-Null
    }
}

switch ($Action) {
    'Preflight' {
        $py = (Get-Command python -ErrorAction Stop).Source
        $ver = & python -c "import sys; print('%d.%d' % sys.version_info[:2])"
        "PYTHON=$ver"
        $null = Get-Command git -ErrorAction Stop
        & python -m uv --version | Out-Null
        if ($LASTEXITCODE -ne 0) { & python -m pip install -q uv; if ($LASTEXITCODE -ne 0) { throw 'cannot install uv' } }
        "UV=ok"
        "TS_IP=" + ((& tailscale ip -4) | Select-Object -First 1)
        $ts = & tailscale status --json | ConvertFrom-Json
        "TS_DNS=" + $ts.Self.DNSName.TrimEnd('.')
        $gpu = & nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>$null
        if ($LASTEXITCODE -eq 0 -and $gpu) { "GPU=$gpu" } else { "GPU=none" }
        $null = Get-Command ollama -ErrorAction Stop
        $models = (& ollama list) -join "`n"
        if ($models -match [regex]::Escape($OllamaModel)) { "OLLAMA_MODEL=present" } else { "OLLAMA_MODEL=missing" }
        $free = [math]::Round((Get-PSDrive ($Home_.Substring(0, 1))).Free / 1GB)
        "DISK_FREE_GB=$free"
    }
    'NodeId' {
        Ensure-StateDir
        $f = Join-Path $State 'node_id'
        if (-not (Test-Path $f)) {
            $id = -join ((1..32) | ForEach-Object { '{0:x}' -f (Get-Random -Maximum 16) })
            [IO.File]::WriteAllText("$f.tmp", $id)
            Move-Item -Force "$f.tmp" $f
        }
        Lock-Down $f
        "NODE_ID=" + ([IO.File]::ReadAllText($f).Trim())
    }
    'ReadToken' {
        # Printed only to the controller's pipe (install script compares it), never logged there.
        if (Test-Path $TokenFile) { [IO.File]::ReadAllText($TokenFile).Trim() }
    }
    'WriteToken' {
        Ensure-StateDir
        $tok = [Console]::In.ReadLine()
        if ($tok -notmatch '^[0-9a-f]{64}$') { throw 'token on stdin has wrong shape' }
        $tmp = "$TokenFile.tmp"
        [IO.File]::WriteAllText($tmp, $tok)
        Lock-Down $tmp
        Move-Item -Force $tmp $TokenFile
        Lock-Down $TokenFile
        "TOKEN=written"
    }
    'Build' {
        Push-Location $New
        try {
            & python -m uv venv -q --python 3.13 .venv
            if ($LASTEXITCODE -ne 0) { throw 'uv venv failed' }
            & python -m uv pip install -q --python .venv\Scripts\python.exe -r requirements.txt -r realtime_subtitle\node\requirements.txt
            if ($LASTEXITCODE -ne 0) { throw 'dependency install failed' }
            # 8 GB cards: 9b does not fit next to Whisper (CLAUDE.md section 2). Allowed config_local key.
            "OLLAMA_MODEL = `"$OllamaModel`"`n" | Set-Content -Encoding ASCII config_local.py
            "BUILD=ok"
        } finally { Pop-Location }
    }
    'SelfCheck' {
        Ensure-StateDir
        Push-Location $New
        try {
            & .venv\Scripts\python.exe scripts\node\windows\selfcheck.py selfcheck.wav
            if ($LASTEXITCODE -ne 0) { throw "selfcheck failed ($LASTEXITCODE)" }
        } finally { Pop-Location }
    }
    'Swap' {
        Stop-Gateway
        if (Test-Path $Cur) {
            if (Test-Path $Prev) { Remove-Item -Recurse -Force $Prev }
            Move-Item $Cur $Prev
            "PREV=kept"
        }
        Move-Item $New $Cur
        Register-Gateway
        Ensure-Firewall
        Start-ScheduledTask -TaskName $TaskName
        "SWAP=ok"
    }
    'Rollback' {
        Stop-Gateway
        if (Test-Path $Failed) { Remove-Item -Recurse -Force $Failed }
        if (Test-Path $Cur) { Move-Item $Cur $Failed }
        if (Test-Path $Prev) {
            Move-Item $Prev $Cur
            Register-Gateway
            Start-ScheduledTask -TaskName $TaskName
            "ROLLBACK=restored-prev"
        } else {
            Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue
            "ROLLBACK=removed"
        }
    }
}
