"""scripts/macos/** 的集成测试：真跑 bash 脚本，只把三样东西换成假的。

为什么这么测：这些脚本的**契约全是跨进程文件约定**（subtitle.pid / .stop /
.paused / subtitle.log）+ 命令行参数 + 退出码。单元测试函数测不到这些——
它们全在"起一个进程、等它、把文件读回来"这条路径上。

被换掉的三样（其余全部是真的：真的 bash、真的 python、真的 realtime_subtitle 包、
真的 config.py、真的 git、真的 curl 打到一个本地假 Ollama HTTP 服务）：
  1. venv 解释器 → 指向本测试跑的 python（而不是 venv/bin/python），被启动的
     "程序"是临时仓库里的 main.py 桩
  2. ollama CLI → 假脚本（list/ps/pull/rm），但**服务**是真的本地 HTTP 服务
  3. HOME / HF_HUB_CACHE → 临时目录，绝不碰真桌面和真模型缓存

☠️ 为什么桩程序是 main.py 而不是"假的 venv/bin/python"：身份校验比的是
`ps -o args=` 的**整串**（见 _identity.sh）。一个带 shebang 的脚本被 exec 之后
argv[0] 会变成解释器路径（`/usr/bin/python3 /tmp/.../venv/bin/python -u ...`），
整串比对直接不成立——那是桩的假象，不是脚本的 bug。真实 venv/bin/python 是
真二进制（符号链接过去也是 exec 二进制），argv 原样保留，所以用真解释器 +
真 main.py 才对应生产环境。

☠️ 所有环境里都强制 TTY=""：_launcher.sh 成功后会用 osascript 关掉**自己那个**
终端窗口，而判据就是 $TTY。测试要是继承了真终端的 TTY，就有可能在跑测试的
过程中把用户正在用的终端窗口关掉。
"""
from __future__ import annotations

import contextlib
import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "darwin", reason="macOS 脚本只在 macOS 上测（bash 3.2 / ps / stat -f）"
)

REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS_DIR = REPO_ROOT / "scripts" / "macos"
LAUNCHER_DIR = MACOS_DIR / "launchers"
LAUNCHER_NAMES = [
    "启动字幕.command",
    "YouTube下载加字幕.command",
    "停止字幕.command",
    "暂停继续字幕.command",
    "卸载字幕.command",
]
SHORTCUT_DIR_NAME = "德语实时字幕"
TX_MODEL = "qwen3.5:4b"
WHISPER_MODEL = "large-v3-turbo"


# ---------------------------------------------------------------- 桩：main.py

STUB_MAIN = '''
"""测试用的字幕程序桩：记录 argv，然后等 .stop 出现。"""
import json, os, pathlib, sys, time

repo = pathlib.Path(sys.argv[0]).resolve().parent
record = os.environ.get("RTS_STUB_ARGS_FILE")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv) + "\\n")
mode = os.environ.get("RTS_STUB_MODE", "graceful")
print("STUB MAIN STARTED", flush=True)
if mode == "exit":
    print("桩：import 期就炸了", file=sys.stderr, flush=True)
    sys.exit(3)
stop = repo / ".stop"
# ☠️ 判据在**循环条件**里，不是在循环体里：写在循环体里只是把 sleep 放慢，
# .stop 一出现照样立刻退出——"强制停止"那条用例就永远测不到 SIGKILL
while not stop.exists() or mode == "ignore-stop":
    time.sleep(0.05)
print("STUB SAW .stop", flush=True)
'''

STUB_DOWNLOAD = '''
"""测试用的 download_subtitle.py 桩：记录 argv；--list-urls 时回显其中的链接。"""
import json, os, sys

record = os.environ.get("RTS_STUB_ARGS_FILE")
if record:
    with open(record, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(sys.argv) + "\\n")
if "--list-urls" in sys.argv:
    # 镜像 offline.extract_share_urls 的关键那条规则：按 URL 的合法字符正向匹配，
    # 撞到 CJK / 全角标点就停。写成"含 :// 的整个参数原样回显"的话，一整段分享
    # 文案会被当成**一个**链接返回，多链接那条用例就永远测不到
    import re
    url_re = re.compile(r"https?://[^\\s\\u3000-\\u303f\\uff01-\\uff60\\uff5b-\\uff65]+")
    for arg in sys.argv[1:]:
        if arg.startswith("-"):
            continue
        for found in url_re.findall(arg):
            print(found)
    sys.exit(0)
print("STUB OFFLINE DONE", flush=True)
'''

STUB_OLLAMA = '''#!/usr/bin/env bash
# 假 ollama CLI。服务是另配的真 HTTP 服务，这里只应付 CLI 那几条命令。
printf '%s\\n' "$*" >> "${RTS_STUB_OLLAMA_LOG:?}"
case "${1:-}" in
    list) printf 'NAME\\tID\\tSIZE\\tMODIFIED\\n%s\\tabc123\\t2.6 GB\\tnow\\n' "${RTS_STUB_TX_MODEL:-''' + TX_MODEL + '''}" ;;
    ps)   printf 'NAME\\tSIZE\\tPROCESSOR\\n%s\\t2.6 GB\\t100%% GPU\\n' "${RTS_STUB_TX_MODEL:-''' + TX_MODEL + '''}" ;;
    *)    exit 0 ;;
esac
'''

STUB_BREW = '''#!/usr/bin/env bash
# 假 brew：所有 formula 都"已安装"，别真的动这台机器
case "${1:-}" in
    list) printf 'ffmpeg\\nollama\\nuv\\n' ;;
    *) exit 0 ;;
esac
'''

STUB_UV = '''#!/usr/bin/env bash
# 假 uv：把 venv/bin/python 指向本测试跑着的 python，其余一律空转
root=""
for a in "$@"; do root="$a"; done   # uv venv --python 3.12 --seed <path>：路径是**最后一个**参数
case "${1:-} $2" in
    "venv --python")
        mkdir -p "$root/bin"
        ln -sf "$RTS_STUB_PYTHON" "$root/bin/python"
        ;;
esac
exit 0
'''

STUB_PBPASE = '''#!/usr/bin/env bash
# 假 pbpaste：测试里绝不读用户真的剪贴板（那是隐私，而且不确定）
printf '%s' "${RTS_STUB_CLIPBOARD:-}"
'''


# ---------------------------------------------------------------- 假 Ollama 服务


class _OllamaState:
    def __init__(self) -> None:
        self.models = [TX_MODEL]
        self.loaded = [TX_MODEL]
        self.unloaded: list[str] = []
        self.posts: list[str] = []


class _OllamaHandler(BaseHTTPRequestHandler):
    state: _OllamaState

    def log_message(self, *args):  # 安静
        pass

    def _send(self, payload, code=200):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/api/tags"):
            self._send({"models": [{"name": n} for n in self.state.models]})
        elif self.path.startswith("/api/ps"):
            self._send({"models": [{"name": n, "size_vram": 1} for n in self.state.loaded]})
        elif self.path.startswith("/api/version"):
            self._send({"version": "0.0.0-test"})
        else:
            self._send({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw)
        except ValueError:
            payload = {}
        self.state.posts.append(self.path)
        if self.path.startswith("/api/generate"):
            name = payload.get("model")
            if payload.get("keep_alive") == 0:
                self.state.unloaded.append(name)
                self.state.loaded = [m for m in self.state.loaded if m != name]
            self._send({"response": "OK", "done": True})
        else:
            self._send({"error": "unsupported"}, 404)


@pytest.fixture
def ollama():
    state = _OllamaState()
    handler = type("Handler", (_OllamaHandler,), {"state": state})
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{httpd.server_address[1]}", state
    finally:
        httpd.shutdown()
        httpd.server_close()


# ---------------------------------------------------------------- 环境


def _write_exec(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _kill_strays(repo: Path) -> None:
    """把测试起的桩进程全部收干净（按命令行里出现这个临时 main.py 来认）。

    ☠️ 只认**本临时仓库绝对路径 + main.py**：绝不用 pkill -f 'python' 这类
    按名字/模式的杀法（CLAUDE.md 第 4 节反复强调的那条）。
    """
    marker = str(repo / "main.py")
    out = subprocess.run(
        ["ps", "-A", "-o", "pid=,args="], capture_output=True, text=True
    ).stdout
    for line in out.splitlines():
        pid_str, _, args = line.strip().partition(" ")
        if marker in args and pid_str.isdigit():
            try:
                os.kill(int(pid_str), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


@pytest.fixture
def env(tmp_path, ollama):
    """一套完整的"假机器"：临时仓库 + 假 PATH + 假 HOME。"""
    url, state = ollama
    repo = tmp_path / "repo"
    (repo / "scripts").mkdir(parents=True)
    shutil.copytree(MACOS_DIR, repo / "scripts" / "macos")
    shutil.copytree(REPO_ROOT / "realtime_subtitle", repo / "realtime_subtitle")
    (repo / "docs").mkdir(exist_ok=True)
    shutil.copy2(REPO_ROOT / "docs" / "macos.md", repo / "docs" / "macos.md")
    (repo / "main.py").write_text(STUB_MAIN, encoding="utf-8")
    (repo / "download_subtitle.py").write_text(STUB_DOWNLOAD, encoding="utf-8")
    (repo / "requirements-macos.txt").write_text("faster-whisper\n", encoding="utf-8")
    (repo / "scripts" / "prune_venv.py").write_text("# 桩\n", encoding="utf-8")

    home = tmp_path / "home"
    (home / "Desktop").mkdir(parents=True)
    hf = tmp_path / "hf"
    (hf / f"models--Systran--faster-whisper-{WHISPER_MODEL}" / "snapshots").mkdir(
        parents=True
    )
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    _write_exec(fake_bin / "ollama", STUB_OLLAMA)
    _write_exec(fake_bin / "brew", STUB_BREW)
    _write_exec(fake_bin / "uv", STUB_UV)
    _write_exec(fake_bin / "pbpaste", STUB_PBPASE)

    args_file = tmp_path / "argv.jsonl"
    ollama_log = tmp_path / "ollama-cli.log"

    base = dict(os.environ)
    # 清掉外层可能带来的测试开关，免得本文件的行为依赖跑测试时的环境
    for key in list(base):
        if key.startswith("RTS_"):
            base.pop(key)
    base.update(
        {
            # 解释器指本测试跑着的 python：main.py 桩于是"真解释器 + 桩程序"
            "RTS_PYTHON": sys.executable,
            "RTS_OLLAMA_URL": url,
            "RTS_NO_SYSTEM_OLLAMA": "1",  # 绝不碰本机真 ollama（见 _common.sh ollama_exe）
            "RTS_STUB_ARGS_FILE": str(args_file),
            "RTS_STUB_OLLAMA_LOG": str(ollama_log),
            "RTS_STUB_TX_MODEL": TX_MODEL,
            "RTS_STUB_PYTHON": sys.executable,
            # 让 config / deps_fingerprint 从**临时仓库**里导入：版本号、路径
            # 全都落在临时目录，真实仓库一个字节都不动
            "PYTHONPATH": str(repo),
            "HOME": str(home),
            "HF_HUB_CACHE": str(hf),
            "PATH": f"{fake_bin}{os.pathsep}{base['PATH']}",
            # 见模块 docstring：绝不能让测试去关用户正在用的终端窗口
            "TTY": "",
        }
    )
    try:
        yield SimpleEnv(repo=repo, home=home, hf=hf, env=base, args_file=args_file,
                        ollama_log=ollama_log, state=state, fake_bin=fake_bin)
    finally:
        _kill_strays(repo)


class SimpleEnv:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    @property
    def pid_file(self) -> Path:
        return self.repo / "subtitle.pid"

    @property
    def stop_flag(self) -> Path:
        return self.repo / ".stop"

    @property
    def pause_flag(self) -> Path:
        return self.repo / ".paused"

    @property
    def desktop(self) -> Path:
        return self.home / "Desktop" / SHORTCUT_DIR_NAME

    def argv_calls(self) -> list[list[str]]:
        if not self.args_file.exists():
            return []
        return [json.loads(line) for line in self.args_file.read_text().splitlines() if line.strip()]

    def ollama_cli_calls(self) -> list[str]:
        if not self.ollama_log.exists():
            return []
        return self.ollama_log.read_text().splitlines()

    def run(self, script: str, *args, stdin="", extra_env=None, timeout=120):
        env = dict(self.env)
        env.update(extra_env or {})
        # ☠️ 跑的是**临时仓库里的那份**脚本副本，不是仓库里的原件：
        # 脚本按自己的位置算仓库根（scripts/macos/../..），跑原件的话
        # RTS_REPO_ROOT 就是真实仓库——subtitle.pid / .stop / logs/ 会全落在
        # 用户的工作目录里，download 那几个用例会真的去联网下载。
        done = subprocess.run(
            ["bash", str(self.repo / "scripts" / "macos" / script), *args],
            input=stdin.encode(),
            capture_output=True,
            env=env,
            # ☠️ cwd 必须是临时仓库：sys.path[0] 就是 cwd，不设的话 pytest 的
            # 工作目录（真实仓库）会把真实 config.py 抢在 PYTHONPATH 前面，
            # 临时仓库里的 config_local.py 全部失效——"测试全绿、机器上不работает"
            # 这一类假绿就是这么来的
            cwd=str(self.repo),
            timeout=timeout,
        )
        # errors="replace"：脚本万一吐出半个多字节字符（☠️ 见下面那条
        # test_no_bare_var_before_non_ascii），测试要拿到文本去断言，
        # 而不是自己先 UnicodeDecodeError 崩掉、看不出真正失败的是什么
        done.stdout = done.stdout.decode("utf-8", "replace")
        done.stderr = done.stderr.decode("utf-8", "replace")
        return done

    def source_libs(self, snippet: str, extra_env=None, timeout=60) -> str:
        """在 bash 里 source _common.sh + _identity.sh 再跑一段片段（单元级测身份规则）。"""
        env = dict(self.env)
        env.update(extra_env or {})
        code = (
            f'set -uo pipefail\n'
            f'RTS_SCRIPT_DIR={MACOS_DIR}\n'
            f'RTS_REPO_ROOT={self.repo}\n'
            f'. "$RTS_SCRIPT_DIR/_common.sh"\n'
            f'. "$RTS_SCRIPT_DIR/_identity.sh"\n'
            + snippet
        )
        return subprocess.run(
            ["bash", "-c", code], capture_output=True, text=True, env=env, timeout=timeout
        ).stdout

    def spawn_stub(self, *args, env_extra=None) -> subprocess.Popen:
        env = dict(self.env)
        env.update(env_extra or {})
        return subprocess.Popen(
            [sys.executable, "-u", str(self.repo / "main.py"), *args],
            cwd=self.repo, env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def start(self, **kw):
        result = self.run("start_subtitles.sh", **kw)
        assert result.returncode == 0, f"start 失败：\n{result.stdout}\n{result.stderr}"
        return result


def _pid_from(e: SimpleEnv) -> int:
    return int(json.loads(e.pid_file.read_text())["pid"])


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    return True


def _wait_gone(pid: int, timeout=8.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


# ---------------------------------------------------------------- start


def test_start_launches_and_writes_identity_file(env):
    result = env.start()
    pid = _pid_from(env)
    try:
        assert "已启动" in result.stdout
        payload = json.loads(env.pid_file.read_text())
        assert payload["pid"] == pid
        assert payload["kind"] == "realtime"
        assert payload["repo_root"] == str(env.repo)
        assert payload["command_line"].endswith(f"{env.repo / 'main.py'}")
        assert payload["start_time"] and payload["start_time"] != "?"
        # 启动契约：`<解释器> -u <main.py>`。桩记的是 Python 视角的 sys.argv，
        # 所以 `-u` 不会出现在里面（那是解释器自己的开关，吃掉之后 argv 从脚本
        # 路径开始）；`-u` 那半边由上面 command_line 的断言钉住
        assert env.argv_calls()[0] == [str(env.repo / "main.py")]
        assert _alive(pid)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_start_refuses_second_instance(env):
    env.start()
    pid = _pid_from(env)
    try:
        again = env.run("start_subtitles.sh")
        assert again.returncode == 0
        assert "已经在运行中" in again.stdout
        assert _pid_from(env) == pid
        assert len(env.argv_calls()) == 1, "不该起第二个进程"
    finally:
        os.kill(pid, signal.SIGKILL)


def test_start_recovers_missing_pid_file(env):
    """pid 文件会丢（误删/异常退出）。丢了也绝不能再起一个。"""
    env.start()
    pid = _pid_from(env)
    try:
        env.pid_file.unlink()
        again = env.run("start_subtitles.sh")
        assert "已补回 subtitle.pid" in again.stdout
        assert _pid_from(env) == pid
        assert len(env.argv_calls()) == 1
    finally:
        os.kill(pid, signal.SIGKILL)


def test_start_ignores_stale_pid_file(env):
    """过期 pid 文件（PID 已被复用/进程早没了）不能挡住启动。"""
    env.pid_file.write_text(json.dumps({
        "pid": 999999, "start_time": "Thu Oct  1 22:49:00 2026",
        "exe": str(env.repo / "venv" / "bin" / "python"), "kind": "realtime",
    }), encoding="utf-8")
    result = env.start()
    pid = _pid_from(env)
    try:
        assert _alive(pid)
        assert len(env.argv_calls()) == 1
        assert "已启动" in result.stdout
    finally:
        os.kill(pid, signal.SIGKILL)


def test_start_reports_immediate_exit(env):
    result = env.run("start_subtitles.sh", extra_env={"RTS_STUB_MODE": "exit"})
    assert result.returncode == 1
    assert "立刻退出" in result.stdout
    assert "桩：import 期就炸了" in result.stdout
    assert not env.pid_file.exists(), "失败的启动不该留下 pid 文件"
    # 也不能在屏幕上打"已启动"
    assert "已启动" not in result.stdout


def test_start_rotates_logs_and_keeps_30_archives(env):
    log_dir = env.repo / "logs"
    log_dir.mkdir()
    (env.repo / "subtitle.log").write_text("上一份日志\n", encoding="utf-8")
    (env.repo / "subtitle.err.log").write_text("上一份错误\n", encoding="utf-8")
    # 两个前缀各 35 份：err 那份不匹配 `subtitle-*`，必须分开裁
    for prefix in ("subtitle", "subtitle.err"):
        for i in range(35):
            # 35 个**互不相同**的名字。写成 `i % 9 + 1` 的话磁盘上只有 9 个真
            # 文件，后 26 次写入把前面的覆盖掉——这条用例就成了假通过，
            # 而它本来是唯一钉住"裁到 30 份"这件事的
            (log_dir / f"{prefix}-20260101-{i:06d}.log").write_text("x", encoding="utf-8")
    env.start()
    pid = _pid_from(env)
    try:
        archived = list(log_dir.glob("subtitle-2*.log"))
        assert len(archived) == 30, "stdout 归档应裁到 30 份"
        assert len(list(log_dir.glob("subtitle.err-2*.log"))) == 30, "err 归档要单独裁"
        assert (env.repo / "subtitle.log").exists(), "新日志要落在原地"
        # 上一份被挪进 logs/，内容还在
        kept = list(log_dir.glob("subtitle-2*.log"))
        assert any(p.read_text(encoding="utf-8").strip() == "上一份日志" for p in kept)
    finally:
        os.kill(pid, signal.SIGKILL)


def test_start_ignores_junk_printed_by_config_local(env):
    """config_local.py 是 exec 进来的，里面一个 print 就会混进 stdout。

    只认 `RSCFG:` 前缀的行：否则那行噪声会被当成模型名，去 `ollama pull` 一个
    不存在的模型，报出"网络/模型名过期"，真正的原因反而被盖住。
    """
    (env.repo / "config_local.py").write_text(
        'print("正在加载本机配置…")\nOLLAMA_MODEL = "qwen3.5:4b"\n', encoding="utf-8"
    )
    result = env.start()
    pid = _pid_from(env)
    try:
        assert "已启动" in result.stdout
        pulls = [c for c in env.ollama_cli_calls() if c.startswith("pull ")]
        assert pulls == [], f"不该 pull 任何东西（模型已就位），实际：{pulls}"
        assert "正在加载本机配置" not in result.stdout
    finally:
        os.kill(pid, signal.SIGKILL)


def test_start_needs_venv(tmp_path, env):
    """还没装（只有 clone）时给中文提示，而不是让 Python 抛栈。"""
    env.env["RTS_PYTHON"] = str(env.repo / "venv" / "bin" / "python")
    result = env.run("start_subtitles.sh")
    assert result.returncode == 1
    assert "还没有安装运行环境" in result.stdout
    assert "install.sh" in result.stdout


# ---------------------------------------------------------------- stop


def test_stop_is_graceful_and_unloads_model(env):
    env.start()
    pid = _pid_from(env)
    result = env.run("stop_subtitles.sh")
    assert result.returncode == 0
    assert "已优雅停止" in result.stdout
    assert _wait_gone(pid)
    assert not env.pid_file.exists()
    assert not env.stop_flag.exists()
    assert not env.pause_flag.exists()
    # 光停 python 不通知 Ollama：模型会按 keep_alive 继续占着内存
    assert env.state.unloaded == [TX_MODEL]
    assert "已卸载 Ollama 常驻模型" in result.stdout


def test_stop_kills_only_that_pid_after_grace(env):
    """不认 .stop 的进程 5 秒后被强杀——但只杀那一个 PID。"""
    bystander = env.spawn_stub("other.py")
    try:
        # 必须是"不认 .stop"模式，否则它自己就优雅退出了，这条用例根本测不到强杀
        env.start(extra_env={"RTS_STUB_MODE": "ignore-stop"})
        pid = _pid_from(env)
        result = env.run("stop_subtitles.sh")
        assert "强制停止" in result.stdout
        assert _wait_gone(pid)
        assert _alive(bystander.pid), "无关进程绝不能被杀"
        assert not env.pid_file.exists()
    finally:
        if bystander.pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(bystander.pid, signal.SIGKILL)


def test_stop_never_touches_other_python_with_stale_pid_file(env):
    """pid 文件指着别的 python（复用/离线任务）：只清理 pid 文件，不动手。"""
    bystander = env.spawn_stub("other.py")
    try:
        time.sleep(0.3)
        env.pid_file.write_text(json.dumps({
            "pid": bystander.pid,
            "start_time": "Thu Oct  1 22:49:00 2026",
            "exe": str(env.repo / "main.py"), "kind": "realtime",
        }), encoding="utf-8")
        result = env.run("stop_subtitles.sh")
        assert result.returncode == 0
        assert "不是当前实时字幕实例" in result.stdout
        assert not env.pid_file.exists()
        assert _alive(bystander.pid)
    finally:
        try:
            os.kill(bystander.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass


def test_stop_cleans_dead_pid_file(env):
    env.pid_file.write_text('{"pid": 999999, "start_time": "x", "kind": "realtime"}\n',
                            encoding="utf-8")
    result = env.run("stop_subtitles.sh")
    assert result.returncode == 0
    assert "已不存在" in result.stdout
    assert not env.pid_file.exists()


def test_stop_finds_instance_when_pid_file_missing(env):
    env.start()
    pid = _pid_from(env)
    env.pid_file.unlink()
    result = env.run("stop_subtitles.sh")
    assert "subtitle.pid 缺失，按进程身份找到" in result.stdout
    assert _wait_gone(pid)


# ---------------------------------------------------------------- pause


def test_pause_toggles_flag_while_running(env):
    env.start()
    pid = _pid_from(env)
    try:
        first = env.run("pause_subtitles.sh")
        assert "已暂停" in first.stdout
        assert env.pause_flag.exists()
        second = env.run("pause_subtitles.sh")
        assert "已继续" in second.stdout
        assert not env.pause_flag.exists()
        assert len(env.argv_calls()) == 1, "暂停不该重启进程"
    finally:
        os.kill(pid, signal.SIGKILL)


def test_pause_says_anything_when_not_running(env):
    result = env.run("pause_subtitles.sh")
    assert "没有在运行" in result.stdout
    assert not env.pause_flag.exists()


# ---------------------------------------------------------------- 身份规则（单元级）


def test_identity_rejects_mismatched_start_time(env):
    proc = env.spawn_stub()
    try:
        time.sleep(0.3)
        out = env.source_libs(
            f'if identity_is_ours {proc.pid} "Thu Jan  1 00:00:00 1970"; '
            f'then echo MATCH; else echo MISMATCH; fi\n'
            f'if identity_is_ours {proc.pid} "$(proc_start_time {proc.pid})"; '
            f'then echo MATCH2; else echo MISMATCH2; fi\n'
        )
        assert "MISMATCH" in out and "MISMATCH2" not in out
        assert "MATCH" in out
    finally:
        proc.kill()


def test_identity_matches_only_this_repo_and_main_py(env):
    """同一个解释器跑别的入口（离线任务）不算实时实例。"""
    proc = subprocess.Popen(
        [sys.executable, "-u", str(env.repo / "download_subtitle.py")],
        cwd=env.repo, env=env.env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        time.sleep(0.3)
        out = env.source_libs(
            f'if identity_is_ours {proc.pid}; then echo MATCH; else echo MISMATCH; fi\n'
        )
        assert "MISMATCH" in out
    finally:
        proc.kill()


# ---------------------------------------------------------------- launchers


def test_launchers_exist_reference_real_scripts_and_are_documented():
    docs = (REPO_ROOT / "docs" / "macos.md").read_text(encoding="utf-8")
    for name in LAUNCHER_NAMES:
        launcher = LAUNCHER_DIR / name
        assert launcher.is_file(), f"少了启动器 {name}"
        assert os.access(launcher, os.X_OK), f"{name} 不可执行（双击 .command 要求 +x）"
        body = launcher.read_text(encoding="utf-8")
        target = [ln for ln in body.splitlines() if ln.startswith("rts_launcher ")]
        assert target, f"{name} 没有调用 rts_launcher"
        script = target[0].split()[1]
        assert (MACOS_DIR / script).is_file(), f"{name} 指向的 {script} 不存在"
        assert name in docs, f"{name} 在 docs/macos.md 里没被解释过（安装脚本把该文件当操作说明）"


def test_launcher_finds_repo_via_sidecar(env):
    """被拷到桌面之后靠旁边的 .repo-root 找仓库（按自己的位置往上找会找错）。"""
    desktop = env.desktop
    desktop.mkdir(parents=True)
    shutil.copy2(env.repo / "scripts" / "macos" / "launchers" / "停止字幕.command",
                 desktop / "停止字幕.command")
    (desktop / ".repo-root").write_text(str(env.repo) + "\n", encoding="utf-8")
    result = subprocess.run(
        ["bash", str(desktop / "停止字幕.command")],
        capture_output=True, text=True, env=env.env, input="", timeout=60,
    )
    assert "找不到字幕程序所在目录" not in result.stdout + result.stderr
    assert "没有找到正在运行的实时字幕程序" in result.stdout
    assert result.returncode == 0


# ---------------------------------------------------------------- install


def test_install_preserves_config_local_and_writes_desktop_launchers(env):
    (env.repo / "config_local.py").write_text(
        '# 我自己调过的，别动\nWHISPER_MODEL = "medium"\n', encoding="utf-8"
    )
    # 装过一次的老用户桌面上可能还留着退役入口
    env.desktop.mkdir(parents=True)
    (env.desktop / "更新字幕.command").write_text("旧的", encoding="utf-8")

    result = env.run("install.sh", timeout=300)
    assert result.returncode == 0, f"install 失败：\n{result.stdout}\n{result.stderr}"
    # ☠️ 一律保留用户的 config_local.py（可能手工调过）
    assert (env.repo / "config_local.py").read_text(encoding="utf-8") == (
        '# 我自己调过的，别动\nWHISPER_MODEL = "medium"\n'
    )
    for name in LAUNCHER_NAMES:
        assert (env.desktop / name).is_file(), f"桌面少了 {name}"
        assert os.access(env.desktop / name, os.X_OK)
    assert (env.desktop / ".repo-root").read_text().strip() == str(env.repo)
    assert (env.desktop / "操作说明.md").is_file()
    assert not (env.desktop / "更新字幕.command").exists(), "退役入口要按确切文件名清掉"
    # 首次启动前必须讲清楚音频权限这件事（macOS 特有，脚本自己给不了）
    assert "屏幕与系统音频录制" in result.stdout


def test_install_writes_minimal_config_local_when_defaults_unusable(env):
    """config.py 默认是 cuda/float16，macOS 上跑不了 → 只补最小的一行。"""
    (env.repo / "realtime_subtitle" / "config.py").write_text(
        "WHISPER_DEVICE = 'cuda'\nWHISPER_COMPUTE_TYPE = 'float16'\n"
        "OLLAMA_MODEL = 'qwen3.5:4b'\nGAME_MODE_OLLAMA_MODEL = 'qwen3.5:4b'\n",
        encoding="utf-8",
    )
    result = env.run("install.sh", timeout=300)
    assert result.returncode == 0, result.stdout + result.stderr
    generated = (env.repo / "config_local.py").read_text(encoding="utf-8")
    assert 'WHISPER_DEVICE = "cpu"' in generated
    assert 'WHISPER_COMPUTE_TYPE = "int8"' in generated


# ---------------------------------------------------------------- uninstall


def test_uninstall_keeps_user_data_by_default(env):
    (env.repo / "transcripts").mkdir()
    (env.repo / "transcripts" / "2026-10-01.txt").write_text("我的字幕存档\n", encoding="utf-8")
    (env.repo / "downloads").mkdir()
    (env.repo / "config_local.py").write_text("# 我的配置\n", encoding="utf-8")
    env.desktop.mkdir(parents=True)
    (env.desktop / "启动字幕.command").write_text("x", encoding="utf-8")

    result = env.run("uninstall.sh", stdin="n\n" * 20, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (env.repo / "transcripts" / "2026-10-01.txt").exists()
    assert (env.repo / "config_local.py").exists()
    assert (env.desktop / "启动字幕.command").exists()
    # 用户数据的位置必须告诉用户，否则他不知道东西还在哪
    assert "transcripts" in result.stdout and "downloads" in result.stdout


def test_uninstall_clean_cache_removes_only_garbage(env):
    broken = env.hf / "models--Systran--faster-whisper-large-v3-turbo"
    (broken / "snapshots").mkdir(parents=True, exist_ok=True)
    partial = broken / "blobs" / "abc.incomplete"
    partial.parent.mkdir(parents=True, exist_ok=True)
    partial.write_bytes(b"x" * 2048)
    empty = env.hf / "models--Systran--faster-whisper-small"
    (empty / "blobs").mkdir(parents=True)
    (empty / "blobs" / "junk").write_bytes(b"y" * 1024)
    keep = env.hf / "models--openai--clip"
    keep.mkdir(parents=True)
    (keep / "model.bin").write_bytes(b"z")

    result = env.run("uninstall.sh", "--clean-cache", stdin="y\n" * 20, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert not partial.exists(), "残文件该被清掉"
    assert not empty.exists(), "没有 model.bin 的空壳目录该被清掉"
    assert (keep / "model.bin").exists(), "别人的模型不许动"


# ---------------------------------------------------------------- 下载加字幕


def test_download_forwards_all_options(env):
    result = env.run(
        "download_subtitle.sh",
        "--url", "https://example.com/v",
        "--target-language", "de",
        "--subtitle-mode", "target",
        "--no-summary",
        "--output-dir", str(env.repo / "out"),
    )
    assert result.returncode == 0, result.stdout + result.stderr
    calls = [c for c in env.argv_calls() if "--list-urls" not in c]
    assert len(calls) == 1, calls
    argv = calls[0][1:]
    assert argv[0] == "https://example.com/v"
    assert "--target-language" in argv and argv[argv.index("--target-language") + 1] == "de"
    assert "--subtitle-mode" in argv and argv[argv.index("--subtitle-mode") + 1] == "target"
    assert "--no-summary" in argv
    assert argv[argv.index("--output-dir") + 1] == str(env.repo / "out")


def test_download_asks_when_clipboard_empty_and_pasted_text_has_two_links(env):
    """粘一整段分享文案 + 里面有多个链接 → 必须让用户选，不能替他随机挑一个。"""
    text = "打开【小红书】App查看！ http://a.example/1，快去看 https://b.example/2 看看"
    result = env.run("download_subtitle.sh", stdin=text + "\n2\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "多个链接" in result.stdout + result.stderr
    calls = [c for c in env.argv_calls() if "--list-urls" not in c]
    assert calls[0][1] == "https://b.example/2"


def test_download_defaults_when_user_just_presses_enter(env):
    result = env.run("download_subtitle.sh", stdin="\n\n", extra_env={"RTS_STUB_CLIPBOARD": "https://c.example/3"})
    assert result.returncode == 0, result.stdout + result.stderr
    assert "双语" in result.stdout
    calls = [c for c in env.argv_calls() if "--list-urls" not in c]
    # 菜单序号不能当语言码传下去（"1" 不是 en）
    assert "--subtitle-mode" in calls[0] and "bilingual" in calls[0]
    assert "1" not in calls[0][1:], "菜单序号泄漏成了参数"


# ---------------------------------------------------------------- 静态约束


def test_all_scripts_parse_and_shellcheck():
    files = sorted(MACOS_DIR.glob("*.sh")) + sorted(LAUNCHER_DIR.glob("*.command"))
    assert files, "scripts/macos 下什么都没有"
    for path in files:
        done = subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, f"{path.name} 语法错误：{done.stderr}"
    shellcheck = shutil.which("shellcheck")
    if not shellcheck:
        pytest.skip("本机没装 shellcheck（brew install shellcheck）")
    done = subprocess.run([shellcheck, "-x", *map(str, files)], capture_output=True, text=True)
    assert done.returncode == 0, f"shellcheck 有意见：\n{done.stdout}"


def test_no_script_kills_by_name_or_pattern():
    """☠️ 绝不能出现 pkill / killall / pgrep -f 这类按名字或模式的杀法。

    同一个 venv 里可能还有用户的离线任务在跑（已经跑了半小时），按名字杀会
    连它一起杀掉。CLAUDE.md 第 4 节把它列成硬约束，这里用源码扫描钉住。
    """
    banned = ("pkill", "killall", "pgrep", "kill -9 -", "kill -TERM -")
    offenders = []
    for path in sorted(MACOS_DIR.glob("*.sh")) + sorted(LAUNCHER_DIR.glob("*.command")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            for needle in banned:
                if needle in stripped:
                    offenders.append(f"{path.name}:{lineno}: {stripped}")
    assert not offenders, "出现了按名字/模式杀进程的写法：\n  " + "\n  ".join(offenders)


def test_no_bare_var_before_non_ascii():
    """☠️ macOS 的 bash 3.2 会把"变量后面紧跟的那个多字节字符"吞进变量名。

    实测（/bin/bash 3.2.57）：

        LC_ALL=en_US.UTF-8 bash -c 'set -u; x=V; echo "$x（中）"'
        → bash: x?: unbound variable      （只吞了第一个字节 0xEF）

    C 语言环境下没事，UTF-8 环境下炸——而 macOS 上中文/德语用户的终端**就是**
    UTF-8 的。所以凡是 `"$var` 后面紧跟中文标点（全角逗号/括号/冒号，
    或者 ✅ ⚠️ 这类字符）的地方都必须写 `"${var}"`。这不是洁癖：本文件第一版
    就是被它坑的，`ollama_pull` 里那句"正在下载翻译模型 $model（首次…）"
    直接让整个启动脚本在真实用户机器上死掉。

    判据用字节而不是字符：bash 吞的就是**字节**，转义写法 ${var} 后接任何
    UTF-8 字符都安全，所以只查没花括号的那种形式。
    """
    import re

    pat = re.compile(rb"\$(?:[A-Za-z_][A-Za-z0-9_]*|[0-9@*#?!$!-])(?=[\x80-\xff])")
    offenders = []
    for path in sorted(MACOS_DIR.rglob("*")):
        if path.is_dir():
            continue
        for lineno, line in enumerate(path.read_bytes().splitlines(), 1):
            if line.lstrip().startswith(b"#"):
                continue
            if pat.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: "
                                 f"{line.decode('utf-8', 'replace').strip()[:90]}")
    assert not offenders, (
        "这些行的变量后面紧跟了非 ASCII 字符，UTF-8 语言环境下 bash 3.2 会报\n"
        f"unbound variable（加花括号 ${'{'}var{'}'} 即可）：\n  " + "\n  ".join(offenders)
    )


def test_short_names_are_forwarding_shells_with_no_logic():
    """start.sh / stop.sh / pause.sh 只能转发，不能长出业务逻辑。

    两条约束都各有代价：
    - 参数必须 `"$@"` 原样透传。列举参数名的话，真脚本以后加的新参数会变成
      "从短名调用时报找不到参数"，而且**只在真用到那个参数时**才暴露
      （Windows 根目录那 7 个转发壳踩过这个，见 CLAUDE.md 第 4 节第 46 条）。
    - 转发壳不许自己实现功能。否则真脚本改了、短名还按老逻辑跑，两边行为
      分叉，而分叉的方向是"看起来还能用"。
    """
    pairs = {
        "start.sh": "start_and_update_subtitles.sh",
        "stop.sh": "stop_subtitles.sh",
        "pause.sh": "pause_subtitles.sh",
    }
    for short, long in pairs.items():
        forwarder = MACOS_DIR / short
        target = MACOS_DIR / long
        assert forwarder.is_file(), f"缺转发壳 {short}"
        assert os.access(forwarder, os.X_OK), f"{short} 不可执行"
        assert target.is_file(), f"{short} 指向的 {long} 不存在"
        text = forwarder.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        assert lines[0] == "#!/usr/bin/env bash", f"{short} 第一行必须是 shebang"
        # 去掉 shebang 和注释之后，只该剩下 set + exec 两条
        code = [line for line in lines[1:] if not line.startswith("#")]
        assert len(code) == 2, f"{short} 里除了 set/exec 还有别的逻辑：{code}"
        assert code[0] == "set -uo pipefail"
        assert code[1].startswith("exec bash "), f"{short} 必须 exec 而不是另起进程"
        # long 本身已经带 .sh，别再补一次
        assert code[1].endswith(f'/{long}" "$@"'), f"{short} 必须把 \"$@\" 原样透传"
        assert forwarder.stat().st_size < target.stat().st_size, \
            f"{short} 比 {long} 还大，多半是逻辑跑到转发壳里了"


def test_pause_short_name_actually_forwards(env):
    """转发壳也要有**行为**层面的证据：跑短名，.paused 一样被切掉。"""
    env.pause_flag.write_text("", encoding="utf-8")
    result = env.run("pause.sh")
    assert result.returncode == 0, result.stdout + result.stderr
    assert not env.pause_flag.exists(), "短名 pause.sh 没转发到真脚本"


def test_scripts_are_executable_and_ignore_darwin_only_files():
    for path in sorted(MACOS_DIR.glob("*.sh")):
        if path.name.startswith("_"):
            continue  # 共用库是被 source 的，不需要可执行位
        assert os.access(path, os.X_OK), f"{path.name} 不可执行（桌面启动器会直接调它）"


def test_docs_macos_links_resolve():
    """docs/macos.md 是桌面《操作说明》的单一真相源，里面的相对链接不许坏。"""
    import re

    doc = REPO_ROOT / "docs" / "macos.md"
    text = re.sub(r"```.*?```", "", doc.read_text(encoding="utf-8"), flags=re.S)
    broken = []
    for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", text):
        target = target.strip()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        target = target.split("#", 1)[0]
        if target and not (doc.parent / target).resolve().exists():
            broken.append(target)
    assert not broken, f"docs/macos.md 里的坏链接：{broken}"