"""依赖没装成，代码不许留在新版本上（真 PowerShell + 真 git + 真 pip，全离线）。

☠️ 事故形态：以前 update 是先 git pull、再 pip install。pip 失败（断网、被离线
任务挡住）时代码已经前移了，启动字幕.bat 接着用「新代码 + 旧依赖」拉起字幕——
新代码 import 一个还没装的包就直接起不来，"更新失败就用现有版本启动"的退路被
它自己堵死。现在 pip 失败会把 HEAD 退回更新前（git reset --keep）。

测试台：
- origin 是 tmp_path 里的一个本地裸仓库，不碰网络；
- venv 是复制过去的启动器存根；PYTHONPATH 指向本仓库 venv 的 site-packages，
  只为了让 `python -m pip` 和 deps_fingerprint 能跑；
- PIP_NO_INDEX=1：`pip` 这一行是"已满足"→ 成功且什么都不装；一个不存在的包名
  → 离线下立刻失败。不会往任何环境里装东西。
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIN = REPO_ROOT / "scripts" / "windows"
VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"
SITE = REPO_ROOT / "venv" / "Lib" / "site-packages"
GIT = shutil.which("git")
POWERSHELL = (os.environ.get("REALTIME_SUBTITLE_TEST_POWERSHELL")
              or shutil.which("powershell") or shutil.which("powershell.exe"))

pytestmark = pytest.mark.skipif(
    not POWERSHELL or not GIT or not VENV_PYTHON.is_file() or not SITE.is_dir(),
    reason="需要 PowerShell、git 和本仓库的 venv")

_SCRIPTS = ("update_subtitles.ps1", "_identity.ps1", "_update_deps.ps1", "_deps_guard.ps1")


def _git(cwd, *args):
    done = subprocess.run(
        [GIT, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, encoding="utf-8")
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


def _commit(work, requirements, marker, msg):
    (work / "requirements.txt").write_text(requirements, encoding="utf-8")
    (work / marker).write_text(msg, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", msg)
    _git(work, "push", "-q", "origin", "master")
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def setup(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "master", str(origin))
    # 发布端：往 origin 推"新版本"用
    pub = tmp_path / "pub"
    _git(tmp_path, "clone", "-q", str(origin), str(pub))
    _git(pub, "checkout", "-q", "-b", "master")
    (pub / "scripts" / "windows").mkdir(parents=True)
    for name in _SCRIPTS:
        shutil.copyfile(WIN / name, pub / "scripts" / "windows" / name)
    pkg = pub / "realtime_subtitle"
    pkg.mkdir()
    for name in ("__init__.py", "version.py", "deps_fingerprint.py"):
        shutil.copyfile(REPO_ROOT / "realtime_subtitle" / name, pkg / name)
    (pub / ".gitignore").write_text("venv/\n", encoding="utf-8")
    (pub / "notes.txt").write_text("原始内容\n", encoding="utf-8")
    base = _commit(pub, "pip\n", "v1.txt", "v1")

    # 用户这边：clone 下来 + 一个能跑 pip 的 venv 存根
    user = tmp_path / "user"
    _git(tmp_path, "clone", "-q", str(origin), str(user))
    (user / "venv" / "Scripts").mkdir(parents=True)
    shutil.copyfile(VENV_PYTHON, user / "venv" / "Scripts" / "python.exe")
    shutil.copyfile(REPO_ROOT / "venv" / "pyvenv.cfg", user / "venv" / "pyvenv.cfg")

    env = {k: v for k, v in os.environ.items() if not k.startswith("PIP_")}
    env.update(PYTHONPATH=str(SITE), PIP_NO_INDEX="1",
               PIP_DISABLE_PIP_VERSION_CHECK="1")
    return {"pub": pub, "user": user, "base": base, "env": env}


def _update(s):
    log = s["user"] / "update.out"
    with open(log, "wb") as fh:
        p = subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(s["user"] / "scripts" / "windows" / "update_subtitles.ps1")],
            cwd=s["user"], env=s["env"], stdout=fh, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL)
        try:
            code = p.wait(timeout=180)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            pytest.fail("update_subtitles.ps1 超时")
    return code, log.read_bytes().decode("utf-8", errors="replace")


def _head(s):
    return _git(s["user"], "rev-parse", "HEAD")


def test_failed_pip_rolls_code_back_and_keeps_local_edits(setup):
    s = setup
    code, out = _update(s)  # 首次：依赖指纹还没记过 → 装一遍（"pip" 已满足）
    assert code == 0, out
    assert _head(s) == s["base"]

    new = _commit(s["pub"], "rs-test-package-that-does-not-exist==0.0.1\n", "v2.txt", "v2")
    (s["user"] / "notes.txt").write_text("用户自己改的\n", encoding="utf-8")

    code, out = _update(s)
    assert code == 1, out
    assert _head(s) == s["base"], "依赖没装成，代码却留在了新版本上"
    assert _head(s) != new
    assert not (s["user"] / "v2.txt").exists()
    assert (s["user"] / "notes.txt").read_text(encoding="utf-8") == "用户自己改的\n", \
        "退回代码时把用户的本地改动弄丢了"


def test_successful_pip_moves_code_forward(setup):
    s = setup
    assert _update(s)[0] == 0
    new = _commit(s["pub"], "pip\n# v2 改了依赖清单的文本\n", "v2.txt", "v2")
    code, out = _update(s)
    assert code == 0, out
    assert _head(s) == new
    assert (s["user"] / "v2.txt").is_file()
    # 指纹记下了：再跑一次就是"已是最新"，不会再装
    code, out = _update(s)
    assert code == 0, out
    assert _head(s) == new
