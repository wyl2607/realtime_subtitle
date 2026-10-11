# Target-side half of scripts/node/install_node_windows.sh (run on the Windows node).
# ASCII only: Windows PowerShell 5.1 reads BOM-less files as ANSI (CLAUDE.md section 4 item 5).
#
# Every action prints KEY=VALUE lines on success and exits non-zero on failure.
# The token never appears in argv or output: WriteToken reads it from stdin.
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet('Preflight', 'NodeId', 'ReadToken', 'WriteToken', 'Build', 'SelfCheck', 'Swap', 'Rollback', 'PushClient')]
    [string]$Action,
    [int]$Port = 8791,
    [string]$OllamaModel = 'qwen3.5:4b',
    [string]$NodeAlias = '',
    [string]$NodeId = '',
    [string]$UrlHost = ''
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
    # Recreate instead of patching: Set-NetFirewall*Filter does not take a rule from the pipeline here.
    Remove-NetFirewallRule -Name $RuleName -ErrorAction SilentlyContinue
    New-NetFirewallRule -Name $RuleName -DisplayName 'Realtime Subtitle node (Tailscale only)' `
        -Direction Inbound -Protocol TCP -LocalPort $Port -RemoteAddress '100.64.0.0/10' -Action Allow -Profile Any | Out-Null
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
        # Copy, do not move: legacy clients keep reading $TokenFile.
        $devDir = Join-Path $State 'tokens'
        if (-not (Test-Path $devDir)) { New-Item -ItemType Directory -Path $devDir | Out-Null }
        Lock-Down $devDir
        $devCopy = Join-Path $devDir 'default.token'
        Copy-Item -Force $TokenFile $devCopy
        Lock-Down $devCopy
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
    'PushClient' {
        # Client-side nodes.json + token. Token comes from stdin, never argv.
        # WriteAllText is given UTF-8 without BOM: the Python client reads the token as utf-8.
        if (-not $env:APPDATA) { throw 'APPDATA missing' }
        if ($NodeAlias -notmatch '^[A-Za-z0-9._@-]+$') { throw 'bad NodeAlias' }
        if ($NodeId -notmatch '^[A-Za-z0-9_-]{8,64}$') { throw 'bad NodeId' }
        if ($UrlHost -notmatch '^[A-Za-z0-9.-]+$') { throw 'bad UrlHost' }
        $tok = [Console]::In.ReadLine()
        if ($tok -notmatch '^[0-9a-f]{64}$') { throw 'token on stdin has wrong shape' }
        $root = Join-Path $env:APPDATA 'realtime_subtitle'
        $tokDir = Join-Path $root 'tokens'
        if (-not (Test-Path $root)) { New-Item -ItemType Directory -Path $root | Out-Null }
        if (-not (Test-Path $tokDir)) { New-Item -ItemType Directory -Path $tokDir | Out-Null }
        $utf8 = New-Object System.Text.UTF8Encoding $false
        $clientToken = Join-Path $tokDir ($NodeAlias + '.token')
        $tmpTok = "$clientToken.tmp"
        [IO.File]::WriteAllText($tmpTok, $tok, $utf8)
        Lock-Down $tmpTok
        Move-Item -Force $tmpTok $clientToken
        Lock-Down $clientToken
        $nodesPath = Join-Path $root 'nodes.json'
        $entries = @()
        if (Test-Path $nodesPath) {
            $raw = [IO.File]::ReadAllText($nodesPath)
            $trim = $raw.TrimStart()
            if (-not $trim.StartsWith('[')) { throw 'nodes.json is not an array' }
            $parsed = $raw | ConvertFrom-Json
            if ($null -eq $parsed) {
                $entries = @()
            } else {
                $entries = @($parsed)
            }
        }
        $url = "ws://${UrlHost}:${Port}"
        $new = [ordered]@{
            id = $NodeAlias
            node_id = $NodeId
            url = $url
            token_file = $clientToken
        }
        $tokensRoot = Join-Path (Join-Path $env:APPDATA 'realtime_subtitle') 'tokens'
        $tokensPrefix = [System.IO.Path]::GetFullPath($tokensRoot)
        $sep = [System.IO.Path]::DirectorySeparatorChar
        if (-not $tokensPrefix.EndsWith([string]$sep)) {
            $tokensPrefix = $tokensPrefix + $sep
        }
        $out = New-Object System.Collections.Generic.List[object]
        $replaced = $false
        foreach ($e in $entries) {
            if ([string]$e.id -eq $NodeAlias) {
                if (-not $replaced) {
                    $out.Add($new)
                    $replaced = $true
                }
                continue
            }
            $tokenFile = [string]$e.token_file
            $keep = $false
            if (-not [string]::IsNullOrWhiteSpace($tokenFile)) {
                try {
                    $full = [System.IO.Path]::GetFullPath($tokenFile)
                    if ($full.StartsWith($tokensPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                        $keep = $true
                    }
                } catch {
                    $keep = $false
                }
            }
            if ($keep) {
                $projected = [ordered]@{
                    id = [string]$e.id
                    node_id = [string]$e.node_id
                    url = [string]$e.url
                    token_file = $tokenFile
                }
                $out.Add($projected)
            }
        }
        if (-not $replaced) { $out.Add($new) }
        $arr = @($out.ToArray())
        if ($arr.Count -eq 1) {
            $json = '[' + (ConvertTo-Json -InputObject $arr[0] -Depth 6 -Compress) + ']'
        } elseif ($arr.Count -eq 0) {
            $json = '[]'
        } else {
            $json = ConvertTo-Json -InputObject $arr -Depth 6 -Compress
        }
        $tmpNodes = "$nodesPath.tmp"
        [IO.File]::WriteAllText($tmpNodes, $json, $utf8)
        Lock-Down $tmpNodes
        Move-Item -Force $tmpNodes $nodesPath
        Lock-Down $nodesPath
        'PUSH=ok'
    }
}
