"""pid 文件丢了之后，启动/停止脚本还认不认得正在跑的实时字幕（真 PowerShell + 真进程）。

2026-09-26 真机撞上的两件事：
A. stop_subtitles.ps1 的兜底按窗口标题找进程，而窗口属于**子进程**、子进程镜像是
   基础 Python——解释器校验必判"不是本仓库实例"而跳过，兜底从来没停下过真实例。
B. start_subtitles.ps1 只看 pid 文件：文件没了就截断正在写的 subtitle.log、再起一个
   被单实例 mutex 挡下秒退的进程，最后照样打印"已启动"。

测试台：tmp_path 里一份最小仓库，用复制过去的 venv 启动器起假 main.py——和真实
情况一样是"存根 + 基础 Python 子进程"两层。假 main.py 学真程序的样子：看到 .stop
就优雅退出并留个记号。假 realtime_subtitle/config.py 给一个不存在的模型名，
停止脚本的"卸载 Ollama 模型"那一段因此什么都不会卸。
"""
import json
import os
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
WIN = REPO_ROOT / "scripts" / "windows"
VENV_PYTHON = REPO_ROOT / "venv" / "Scripts" / "python.exe"
POWERSHELL = (os.environ.get("REALTIME_SUBTITLE_TEST_POWERSHELL")
              or shutil.which("powershell") or shutil.which("powershell.exe"))

pytestmark = pytest.mark.skipif(
    not POWERSHELL or not VENV_PYTHON.is_file(),
    reason="需要 Windows PowerShell 和本仓库的 venv")

# 学真程序：轮询 .stop，看到就优雅退出（真程序是 0.5 秒一查）
_GRACEFUL_MAIN = """import os, time
root = os.path.dirname(os.path.abspath(__file__))
for _ in range(1200):
    if os.path.exists(os.path.join(root, ".stop")):
        open(os.path.join(root, "graceful.txt"), "w").close()
        raise SystemExit(0)
    time.sleep(0.1)
"""


def _ollama_models():
    try:
        with urllib.request.urlopen("http://127.0.0.1:11434/api/tags", timeout=2) as r:
            return [m["name"] for m in json.load(r).get("models", [])]
    except Exception:
        return None


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "venv" / "Scripts").mkdir(parents=True)
    shutil.copyfile(VENV_PYTHON, tmp_path / "venv" / "Scripts" / "python.exe")
    cfg = REPO_ROOT / "venv" / "pyvenv.cfg"
    if cfg.is_file():
        shutil.copyfile(cfg, tmp_path / "venv" / "pyvenv.cfg")
    win = tmp_path / "scripts" / "windows"
    win.mkdir(parents=True)
    for name in ("_identity.ps1", "start_subtitles.ps1", "stop_subtitles.ps1"):
        shutil.copyfile(WIN / name, win / name)
    pkg = tmp_path / "realtime_subtitle"
    pkg.mkdir()
    (pkg / "__init__.py").write_text("", encoding="utf-8")
    (pkg / "config.py").write_text(
        "OLLAMA_MODEL = 'rs-test-model-that-does-not-exist'\n"
        "GAME_MODE_OLLAMA_MODEL = None\nWHISPER_MODEL = 'rs-test-whisper'\n",
        encoding="utf-8")
    (tmp_path / "main.py").write_text(_GRACEFUL_MAIN, encoding="utf-8")
    yield tmp_path
    subprocess.run(
        [POWERSHELL, "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -and "
         f"$_.CommandLine.Contains('{tmp_path}') -and $_.Name -eq 'python.exe' }} | "
         "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
        capture_output=True, timeout=60)


def _start_fake(repo):
    """按启动脚本的方式起：venv 启动器 + `-u main.py`，工作目录在仓库根。不写 pid 文件。"""
    proc = subprocess.Popen(
        [str(repo / "venv" / "Scripts" / "python.exe"), "-u", "main.py"], cwd=repo,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    time.sleep(1.0)
    assert proc.poll() is None
    return proc


def _run_script(repo, name, timeout=90):
    # ☠️ 不套这层的话，5.1 按 OEM 代码页往重定向的 stdout 写，中文全成问号，
    # 对提示文案的断言永远是假的（同 test_download_ps1_args）
    wrapper = repo / f"run_{name}"
    wrapper.write_text(
        "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        "$OutputEncoding = [System.Text.Encoding]::UTF8\r\n"
        f"& (Join-Path $PSScriptRoot 'scripts\\windows\\{name}')\r\n"
        "exit $LASTEXITCODE\r\n", encoding="utf-8-sig")
    log = repo / f"{name}.out"
    with open(log, "wb") as fh:
        p = subprocess.Popen(
            [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
             str(wrapper)],
            cwd=repo, stdout=fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL)
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(p.pid)], capture_output=True)
            pytest.fail(f"{name} 超时")
    return code, log.read_bytes().decode("utf-8", errors="replace")


def test_stop_finds_instance_without_pid_file_and_stops_gracefully(repo):
    """A：pid 文件没了，停止脚本按进程身份找到它，并且走的是**优雅退出**。"""
    fake = _start_fake(repo)
    assert not (repo / "subtitle.pid").exists()
    code, out = _run_script(repo, "stop_subtitles.ps1")
    assert code == 0, out
    fake.wait(timeout=15)
    assert (repo / "graceful.txt").is_file(), f"没走 .stop 优雅退出：\n{out}"
    assert "没有找到正在运行的实时字幕程序" not in out, out


def test_start_refuses_second_copy_and_restores_pid_file(repo):
    """B：pid 文件没了，启动脚本也得认出来：不截断日志、不起第二个、把 pid 文件补回来。"""
    _start_fake(repo)
    (repo / "subtitle.log").write_text("正在运行那份实例的日志\n", encoding="utf-8")
    code, out = _run_script(repo, "start_subtitles.ps1")
    assert code == 0, out
    assert "已经在运行中" in out, out
    assert (repo / "subtitle.log").read_text(encoding="utf-8") == "正在运行那份实例的日志\n", \
        "正在写的日志被截断了"
    ident = json.loads((repo / "subtitle.pid").read_text(encoding="utf-8-sig"))
    assert ident["exe"].lower().endswith(r"venv\scripts\python.exe"), ident
    # 补回来的 pid 文件要让停止脚本走正常的 pid 路径
    code, out = _run_script(repo, "stop_subtitles.ps1")
    assert (repo / "graceful.txt").is_file(), out


def _prepare_full_start(repo, main_source):
    """启动脚本走到 Start-Process 需要 Ollama 在跑、且配置的模型已经装好。
    用本机现成的模型名，保证它**绝不会触发 ollama pull**。"""
    models = _ollama_models()
    if not models:
        pytest.skip("本机 Ollama 没在跑或一个模型都没装")
    (repo / "realtime_subtitle" / "config.py").write_text(
        f"OLLAMA_MODEL = {models[0]!r}\nGAME_MODE_OLLAMA_MODEL = None\n"
        "WHISPER_MODEL = 'rs-test-whisper'\n", encoding="utf-8")
    prelude = ("import sys\nsys.stdout.reconfigure(encoding='utf-8')\n"
               "sys.stderr.reconfigure(encoding='utf-8')\n")
    (repo / "main.py").write_text(prelude + main_source, encoding="utf-8")


def test_start_reports_instant_crash_instead_of_started(repo):
    """B：新进程一两秒内就退了，不许再打印"已启动"，要把报错亮出来并返回 1（bat 靠它留窗口）。"""
    _prepare_full_start(repo, "import sys\nprint('boom: 致命错误', file=sys.stderr)\nsys.exit(1)\n")
    code, out = _run_script(repo, "start_subtitles.ps1")
    assert code == 1, out
    assert "已启动 (PID" not in out, out
    assert "boom: 致命错误" in out, out
    assert not (repo / "subtitle.pid").exists(), "死掉的进程不该留下 pid 文件"


def test_start_explains_mutex_exit(repo):
    """被另一份副本的单实例 mutex 挡下：说清楚，不报错，也不说"已启动"。"""
    _prepare_full_start(repo, "print('⚠️  实时字幕已经在运行了，不再启动第二个实例')\n")
    code, out = _run_script(repo, "start_subtitles.ps1")
    assert code == 0, out
    assert "已启动 (PID" not in out, out
    assert "已经在运行了" in out, out
