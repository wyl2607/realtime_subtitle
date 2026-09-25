"""桌面 .bat 挑哪个 PowerShell：有 pwsh（7）用 pwsh，没有退回 5.1。

真跑：从 install.ps1 里原样抠出生成 bat 的那两行模板，让 PowerShell 把它
展开成一个真 bat，再用 cmd 跑。被调用的 ps1 是个桩，只报告自己跑在哪个版本
上并 exit 3——这样还能顺带验：

☠️ 失败时**不许用 5.1 再跑一遍**。模板要是写成 `where pwsh && pwsh … || powershell …`，
pwsh 那次返回非 0 就会触发 ||，整个脚本（比如启动字幕）被执行两次。

跳过条件：没有 powershell.exe（非 Windows）。选 pwsh 那条另需装了 pwsh。
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_PS1 = REPO_ROOT / "scripts" / "windows" / "install.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")
PWSH = shutil.which("pwsh") or shutil.which("pwsh.exe")

pytestmark = pytest.mark.skipif(not POWERSHELL, reason="需要 Windows PowerShell")

_STUB = """Add-Content -Path (Join-Path $PSScriptRoot 'runs.txt') -Value $PSEdition
exit 3
"""


def _make_bat(tmp_path):
    text = INSTALL_PS1.read_text(encoding="utf-8-sig")
    pick = re.search(r"^\$psPick = .*$", text, re.M)
    head = re.search(r"^\s*\$head = .*$", text, re.M)
    assert pick and head, "install.ps1 里找不到 $psPick / $head 模板行，同步改本测试"
    (tmp_path / "stub.ps1").write_text(_STUB, encoding="utf-8-sig")
    bat = tmp_path / "run.bat"
    script = (
        f"$RepoRoot = '{tmp_path}'\n$pair = @('run.bat', 'stub.ps1')\n"
        f"{pick.group(0)}\n{head.group(0).strip()}\n"
        f"[IO.File]::WriteAllText('{bat}', $head + \"exit /b %errorlevel%`r`n\", "
        f"(New-Object Text.UTF8Encoding $false))\n")
    done = subprocess.run([POWERSHELL, "-NoProfile", "-Command", script],
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr
    return bat


def _run_bat(bat, path_env):
    env = dict(os.environ, PATH=path_env)
    return subprocess.run(["cmd", "/c", str(bat)], env=env, capture_output=True,
                          timeout=120)


def _runs(tmp_path):
    return (tmp_path / "runs.txt").read_text(encoding="utf-8-sig").split()


def test_generated_bat_is_pure_ascii(tmp_path):
    """chcp 65001 下 cmd 解析含非 ASCII 的行会把下一行开头吃掉（install.ps1 ⚠️ 那条）。"""
    raw = _make_bat(tmp_path).read_bytes()
    raw.decode("ascii")


def test_falls_back_to_windows_powershell_without_pwsh(tmp_path):
    bat = _make_bat(tmp_path)
    path = os.pathsep.join(p for p in os.environ["PATH"].split(os.pathsep)
                           if not (Path(p) / "pwsh.exe").is_file())
    done = _run_bat(bat, path)
    assert done.returncode == 3, "ps1 的退出码要原样传回 bat（启动失败时靠它留窗口）"
    assert _runs(tmp_path) == ["Desktop"]


@pytest.mark.skipif(not PWSH, reason="本机没装 PowerShell 7")
def test_prefers_pwsh_and_never_reruns_on_failure(tmp_path):
    bat = _make_bat(tmp_path)
    done = _run_bat(bat, os.environ["PATH"])
    assert done.returncode == 3
    assert _runs(tmp_path) == ["Core"], "失败时被 5.1 又跑了一遍，或者根本没用上 pwsh"
