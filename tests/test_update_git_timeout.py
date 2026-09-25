"""update_subtitles.ps1 的 git pull 不能无限挂着（真跑 PowerShell + 真 git）。

☠️ 事故形态：git pull 默认没有任何超时。网络半通时 HTTPS 连上了却不给数据，
git 就一直等；启动字幕.bat = 更新 + 启动，于是用户看到的是"双击没反应"。

测试台：本地起一个**只接受连接、一个字节都不回**的假服务器当 origin，在
tmp_path 里建一个最小 git 仓库 + 复制过去的更新脚本。只给 GIT_HTTP_LOW_SPEED_TIME
（缩短等待），刻意**不给** LIMIT——git 要两者都有才启用低速检测，所以旧脚本
在这里会无限挂住（被本测试的超时判红），新脚本自己补上 LIMIT 后几秒内失败返回。

跳过条件：没有 powershell.exe 或 git。
"""
import os
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIN = REPO_ROOT / "scripts" / "windows"
# REALTIME_SUBTITLE_TEST_POWERSHELL=pwsh 可以把整套 PowerShell 用例切到 7 上跑：
# 桌面 bat 有 pwsh 就优先用它（见 install.ps1 的 $psPick），两个版本都得绿
POWERSHELL = (os.environ.get("REALTIME_SUBTITLE_TEST_POWERSHELL")
              or shutil.which("powershell") or shutil.which("powershell.exe"))
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(
    not POWERSHELL or not GIT, reason="需要 Windows PowerShell 和 git")


@pytest.fixture
def stalling_server():
    """接受连接、读掉请求、然后一声不吭地挂着。"""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held, stop = [], threading.Event()

    def loop():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = srv.accept()
                held.append(conn)
            except OSError:
                continue
    t = threading.Thread(target=loop, daemon=True)
    t.start()
    yield srv.getsockname()[1]
    stop.set()
    for c in held:
        c.close()
    srv.close()


def _git(cwd, *args):
    subprocess.run([GIT, *args], cwd=cwd, check=True, capture_output=True)


def test_git_pull_gives_up_on_stalled_connection(tmp_path, stalling_server):
    repo = tmp_path / "repo"
    (repo / "scripts" / "windows").mkdir(parents=True)
    for name in ("update_subtitles.ps1", "_identity.ps1", "_update_deps.ps1",
                 "_deps_guard.ps1"):
        shutil.copyfile(WIN / name, repo / "scripts" / "windows" / name)
    _git(repo, "init", "-q", "-b", "master")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q",
         "--allow-empty", "-m", "init")
    _git(repo, "remote", "add", "origin", f"http://127.0.0.1:{stalling_server}/r.git")
    _git(repo, "config", "branch.master.remote", "origin")
    _git(repo, "config", "branch.master.merge", "refs/heads/master")

    env = {k: v for k, v in os.environ.items()
           if not k.startswith("GIT_HTTP_LOW_SPEED")}
    env["GIT_HTTP_LOW_SPEED_TIME"] = "3"  # 只缩短等待，LIMIT 必须由脚本自己补
    # ☠️ 输出必须写文件、不能用管道，超时必须杀**整棵进程树**。实测过旧脚本：
    # timeout 只杀得掉 powershell，挂着的 git / git-remote-http 子进程继承了
    # stdout 管道，communicate() 等 EOF 等到天荒地老——把整个 pytest 一起挂死。
    log = tmp_path / "out.log"
    t0 = time.monotonic()
    with open(log, "wb") as fh:
        proc = subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(repo / "scripts" / "windows" / "update_subtitles.ps1")],
            cwd=repo, env=env, stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL)
        try:
            code = proc.wait(timeout=60)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
            proc.wait(timeout=10)
            pytest.fail("git pull 在不回数据的服务器上挂满 60 秒——没有低速超时")
    elapsed = time.monotonic() - t0
    out = log.read_bytes().decode("utf-8", errors="replace")
    assert code == 1, out
    assert elapsed < 45, f"用了 {elapsed:.0f} 秒才放弃"


def test_update_script_disables_interactive_credential_prompts():
    text = (WIN / "update_subtitles.ps1").read_text(encoding="utf-8-sig")
    pull = text.index("git pull --ff-only")
    for needle in ('$env:GIT_TERMINAL_PROMPT = "0"', '$env:GCM_INTERACTIVE = "never"',
                   "GIT_HTTP_LOW_SPEED_LIMIT", "GIT_HTTP_LOW_SPEED_TIME"):
        pos = text.find(needle)
        assert 0 <= pos < pull, f"{needle} 必须在 git pull 之前设好"
