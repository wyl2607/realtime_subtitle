# 依赖指纹：记录上次成功安装的 requirements + CPU/GPU 档位。
# pip 失败不得写这个文件，所以提交不变时仍能重试。

function Get-InstallTier {
    $hasGpu = $false
    try {
        $smi = & nvidia-smi --query-gpu=name --format=csv,noheader,nounits 2>$null
        if ($LASTEXITCODE -eq 0 -and $smi) {
            # Keep this decision in sync with install.ps1: a visible NVIDIA
            # card with a CUDA driver below 12 is deliberately CPU tier.
            $smiHead = (& nvidia-smi 2>$null | Select-Object -First 15) -join "`n"
            $cudaReported = $null
            if ($smiHead -match 'CUDA(?:\s+UMD)?\s+Version:\s*([\d.]+)') {
                $cudaReported = [double]$Matches[1]
            }
            $hasGpu = $null -eq $cudaReported -or $cudaReported -ge 12.0
        }
    } catch {}
    if ($hasGpu) { return "gpu" }
    return "cpu"
}

function Get-DepsFingerprintPath {
    param($RepoRoot)
    return Join-Path $RepoRoot "venv\.deps_fingerprint"
}

function Get-CurrentDepsFingerprint {
    param($RepoRoot, $Tier)
    $vpy = Join-Path $RepoRoot "venv\Scripts\python.exe"
    if (-not (Test-Path $vpy)) { return $null }
    $env:RTS_REPO = $RepoRoot
    $env:RTS_TIER = $Tier
    $fp = & $vpy -c "import os; from pathlib import Path; from realtime_subtitle.deps_fingerprint import fingerprint_requirements; print(fingerprint_requirements(Path(os.environ['RTS_REPO'], 'requirements.txt').read_text(encoding='utf-8'), os.environ['RTS_TIER']))" 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $fp) { return $null }
    return ($fp | Select-Object -Last 1).ToString().Trim()
}

function Read-StoredDepsFingerprint {
    param($RepoRoot)
    $path = Get-DepsFingerprintPath $RepoRoot
    if (-not (Test-Path $path)) { return $null }
    return ((Get-Content $path -Raw -ErrorAction SilentlyContinue) + "").Trim()
}

function Write-DepsFingerprint {
    param($RepoRoot, $Tier)
    $fp = Get-CurrentDepsFingerprint -RepoRoot $RepoRoot -Tier $Tier
    if (-not $fp) { return }
    $dir = Join-Path $RepoRoot "venv"
    if (-not (Test-Path $dir)) { return }
    Set-Content -Path (Get-DepsFingerprintPath $RepoRoot) -Value $fp -Encoding ascii
}

function Get-RequirementsFileForTier {
    param($RepoRoot, $Tier)
    $src = Join-Path $RepoRoot "requirements.txt"
    if ($Tier -ne "cpu") { return $src }
    $dst = Join-Path $env:TEMP "rt_subtitle_requirements_cpu.txt"
    Get-Content $src |
        Where-Object { $_ -notmatch '^\s*nvidia-' } |
        Set-Content -Path $dst -Encoding UTF8
    return $dst
}
