"""启动字幕.bat（start_and_update_subtitles.ps1）在字幕拉起来之后必须自己返回。

☠️ 2026-09-26 切到 pwsh 当天真机撞上：Invoke-Sibling 以前是
`& $exe -File start_subtitles.ps1 | Out-Host`。pwsh 7 下，start_subtitles.ps1 用
Start-Process 拉起的常驻 python 会继承那根管道的写端，Out-Host 等 EOF 一直等到
字幕程序退出——bat 的黑窗口在字幕开着的整个期间都关不掉。5.1 不继承句柄，所以
在 5.1 上一直没暴露；test_desktop_bat_shell 的桩也没起常驻子进程，同样没抓到。

测试台：update/stop 桩直接 exit 0；start 桩照真脚本的样子，用 Start-Process
+ -RedirectStandardOutput 拉起一个睡 90 秒的 python，然后 exit 0。
start_and_update 必须在几秒内返回 0。两个 PowerShell 版本都跑。
"""
import shutil
import subprocess
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIN = REPO_ROOT / "scripts" / "windows"
VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"
SHELLS = [s for s in (shutil.which("powershell"), shutil.which("pwsh")) if s]

pytestmark = pytest.mark.skipif(
    not SHELLS or not VENV_PYTHON.is_file(), reason="需要 PowerShell 和本仓库的 venv")

_START_STUB = """$root = (Get-Item $PSScriptRoot).Parent.Parent.FullName
Start-Process -FilePath '{python}' -ArgumentList '-c', '"import time; time.sleep(90)"' `
    -WorkingDirectory $root -PassThru -WindowStyle Hidden `
    -RedirectStandardOutput (Join-Path $root 'child.log') `
    -RedirectStandardError (Join-Path $root 'child.err.log') |
    ForEach-Object {{ Set-Content -Path (Join-Path $root 'child.pid') -Value $_.Id }}
Write-Host 'start stub done'
exit 0
"""


@pytest.mark.parametrize("shell", SHELLS, ids=lambda s: Path(s).stem)
def test_start_and_update_returns_while_subtitles_keep_running(tmp_path, shell):
    win = tmp_path / "scripts" / "windows"
    win.mkdir(parents=True)
    shutil.copyfile(WIN / "start_and_update_subtitles.ps1",
                    win / "start_and_update_subtitles.ps1")
    shutil.copyfile(WIN / "_identity.ps1", win / "_identity.ps1")
    for name in ("update_subtitles.ps1", "stop_subtitles.ps1"):
        (win / name).write_text("exit 0\n", encoding="utf-8-sig")
    (win / "start_subtitles.ps1").write_text(
        _START_STUB.format(python=VENV_PYTHON), encoding="utf-8-sig")

    out = tmp_path / "out.log"
    t0 = time.monotonic()
    try:
        with open(out, "wb") as fh:
            proc = subprocess.Popen(
                [shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(win / "start_and_update_subtitles.ps1")],
                cwd=tmp_path, stdout=fh, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL)
            try:
                code = proc.wait(timeout=40)
            except subprocess.TimeoutExpired:
                subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                               capture_output=True)
                pytest.fail("start_and_update 在字幕进程还活着时不返回——"
                            "黑窗口会一直开着（子脚本输出又走了管道？）")
        elapsed = time.monotonic() - t0
        text = out.read_bytes().decode("utf-8", errors="replace")
        assert code == 0, text
        assert "start stub done" in text, "子脚本的输出要直接出现在控制台上"
        assert elapsed < 30, f"用了 {elapsed:.0f} 秒"
    finally:
        # ☠️ 只按确切 PID 收：以前按"命令行里含 time.sleep(90)"宽匹配，连跑测试
        # 那个终端自己的 shell（命令行里恰好也有这串）都一起杀掉了
        pid_file = tmp_path / "child.pid"
        if pid_file.is_file():
            pid = pid_file.read_text(encoding="utf-8-sig").strip()
            subprocess.run(["taskkill", "/T", "/F", "/PID", pid], capture_output=True)
