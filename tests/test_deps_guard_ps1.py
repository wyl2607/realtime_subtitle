"""依赖同步前腾 venv（scripts/windows/_deps_guard.ps1，真跑 PowerShell + 真进程）。

☠️ 事故形态：Windows 上被加载着的 DLL/.pyd 不能替换。以前 update 的顺序是
先 pip install、后停字幕——字幕开着时 torch/ctranslate2/PyQt6 升级半路 WinError 5，
随后 start_and_update 又用"新代码 + 旧依赖"把字幕拉起来。

测试台：tmp_path 里搭一份最小仓库，用本仓库 venv 的启动器存根（复制过去，
配上 pyvenv.cfg）起两种假进程——一个是"实时字幕"（main.py + subtitle.pid 身份），
一个是"离线任务"（download_subtitle.py，没有 pid 身份）。停止脚本换成桩，
只杀 pid 文件里那个进程并留个记号，不碰 Ollama、不碰真仓库。

跳过条件：没有 powershell.exe，或本仓库还没建 venv。
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIN = REPO_ROOT / "scripts" / "windows"
VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"
# REALTIME_SUBTITLE_TEST_POWERSHELL=pwsh 可以把整套 PowerShell 用例切到 7 上跑：
# 桌面 bat 有 pwsh 就优先用它（见 install.ps1 的 $psPick），两个版本都得绿
POWERSHELL = (os.environ.get("REALTIME_SUBTITLE_TEST_POWERSHELL")
              or shutil.which("powershell") or shutil.which("powershell.exe"))

pytestmark = pytest.mark.skipif(
    not POWERSHELL or not VENV_PYTHON.is_file(),
    reason="需要 Windows PowerShell 和本仓库的 venv")

_SLEEPER = "import time\ntime.sleep(120)\n"

# 停止脚本桩：只做"真停止脚本对实时实例做的那件事"——按 pid 文件杀掉它
_STOP_STUB = """$root = (Get-Item $PSScriptRoot).Parent.Parent.FullName
$id = Get-Content (Join-Path $root 'subtitle.pid') -Raw | ConvertFrom-Json
Stop-Process -Id $id.pid -Force -ErrorAction SilentlyContinue
Remove-Item (Join-Path $root 'subtitle.pid') -ErrorAction SilentlyContinue
Set-Content -Path (Join-Path $root 'stop_called.txt') -Value 'yes'
"""


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    shutil.copyfile(VENV_PYTHON, tmp_path / "venv" / "Scripts" / "python.exe")
    cfg = REPO_ROOT / "venv" / "pyvenv.cfg"
    if cfg.is_file():
        shutil.copyfile(cfg, tmp_path / "venv" / "pyvenv.cfg")
    win = tmp_path / "scripts" / "windows"
    win.mkdir(parents=True)
    for name in ("_identity.ps1", "_deps_guard.ps1"):
        shutil.copyfile(WIN / name, win / name)
    (win / "stop_subtitles.ps1").write_text(_STOP_STUB, encoding="utf-8-sig")
    (tmp_path / "main.py").write_text(_SLEEPER, encoding="utf-8")
    (tmp_path / "download_subtitle.py").write_text(_SLEEPER, encoding="utf-8")
    yield tmp_path
    # 兜底清场：本用例起的进程一个都不许留下（按命令行里的临时目录认）
    subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
         f"$_.CommandLine.Contains('{tmp_path}') }} | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
        capture_output=True, timeout=60)


def _run(repo, body):
    script = repo / "driver.ps1"
    script.write_text(
        f"$root = '{repo}'\n"
        ". \"$root\\scripts\\windows\\_identity.ps1\"\n"
        ". \"$root\\scripts\\windows\\_deps_guard.ps1\"\n"
        "function Start-Fake($entry) {\n"
        "    $p = Start-Process -FilePath \"$root\\venv\\Scripts\\python.exe\" "
        "-ArgumentList $entry -WorkingDirectory $root -PassThru -WindowStyle Hidden\n"
        "    Start-Sleep -Milliseconds 800\n"
        "    return $p\n"
        "}\n"
        + body,
        encoding="utf-8-sig")
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)],
        capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    last = [ln for ln in done.stdout.splitlines() if ln.startswith("{")][-1]
    return json.loads(last)


def _alive(repo, pid):
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         f"if (Get-Process -Id {pid} -ErrorAction SilentlyContinue) {{ 'yes' }} else {{ 'no' }}"],
        capture_output=True, text=True, timeout=30)
    return done.stdout.strip() == "yes"


def test_running_realtime_instance_is_stopped_before_pip(repo):
    r = _run(repo, (
        "$p = Start-Fake 'main.py'\n"
        "Write-SubtitleIdentity -Proc $p -PidFile \"$root\\subtitle.pid\" -RepoRoot $root\n"
        "$r = Request-VenvForPip -RepoRoot $root -SettleSeconds 5\n"
        "@{ ok = $r.Ok; stopped = $r.StoppedRealtime; blockers = @($r.Blockers); "
        "pid = $p.Id } | ConvertTo-Json -Compress\n"))
    assert (repo / "stop_called.txt").is_file(), "字幕开着就必须先走停止脚本"
    assert r["stopped"] is True
    assert r["ok"] is True, f"停完之后 venv 应该空了，还剩 {r['blockers']}"
    assert not _alive(repo, r["pid"])


def test_offline_job_blocks_pip_and_is_never_killed(repo):
    """同 venv 的离线任务：报出来、这次不装依赖，但绝不替用户杀掉。"""
    r = _run(repo, (
        "$p = Start-Fake 'download_subtitle.py'\n"
        "$r = Request-VenvForPip -RepoRoot $root -SettleSeconds 1\n"
        "@{ ok = $r.Ok; stopped = $r.StoppedRealtime; blockers = @($r.Blockers); "
        "pid = $p.Id } | ConvertTo-Json -Compress\n"))
    assert r["ok"] is False
    assert r["stopped"] is False
    assert not (repo / "stop_called.txt").exists(), "离线任务不归停止脚本管"
    assert r["pid"] in r["blockers"]
    assert _alive(repo, r["pid"]), "离线任务被杀了——它可能已经跑了半小时"


def test_nothing_running_is_a_noop(repo):
    r = _run(repo, (
        "$r = Request-VenvForPip -RepoRoot $root -SettleSeconds 1\n"
        "@{ ok = $r.Ok; stopped = $r.StoppedRealtime; blockers = @($r.Blockers) } "
        "| ConvertTo-Json -Compress\n"))
    assert r == {"ok": True, "stopped": False, "blockers": []}
    assert not (repo / "stop_called.txt").exists()


def test_update_script_releases_venv_before_pip():
    """静态盯顺序：update_subtitles.ps1 里 Request-VenvForPip 必须排在 pip 之前。"""
    text = (WIN / "update_subtitles.ps1").read_text(encoding="utf-8-sig")
    assert '. "$PSScriptRoot\\_deps_guard.ps1"' in text
    guard = text.index("Request-VenvForPip")
    pip = text.index("& $vpy @pipArgs")
    assert guard < pip, "必须先腾出 venv 再 pip install"
