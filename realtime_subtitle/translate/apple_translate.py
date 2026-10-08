"""macOS 系统 Translation helper 客户端。

协议由 Swift 侧 rstranslate 固定：启动后 stdout 先吐 ready 行，之后 stdin/stdout
一问一答。这里故意只做一件事：把 Python 侧的读写、超时和重启边界收紧，翻译
质量/繁简转换都留给 helper。
"""
from __future__ import annotations

import json
import queue
import subprocess
import sys
import time
from pathlib import Path
from threading import Lock, Thread

from realtime_subtitle.paths import repo_path


_READY_TIMEOUT = 5.0
_CLOSE_TIMEOUT = 3.0


def _helper_lang(lang: str) -> str:
    """项目里用 zh 表示中文，Translation framework 需要明确简体区域。"""
    return "zh-Hans" if lang == "zh" else lang


class AppleTranslator:
    """常驻 rstranslate 子进程。

    stdout 由后台线程唯一读取，主线程只从 Queue 取协议行；这样请求超时时不会
    卡死在 readline() 上，也不会把下一次请求和上一次迟到响应混在一起。
    """

    def __init__(self, helper_path: str | None = None, startup_timeout: float = _READY_TIMEOUT):
        self.helper_path = helper_path or repo_path("macos-native", ".build", "release", "rstranslate")
        self.startup_timeout = startup_timeout
        self._lock = Lock()
        self._proc: subprocess.Popen | None = None
        self._reader: Thread | None = None
        self._lines: queue.Queue[str | None] = queue.Queue()
        self._next_id = 1

    def _start_locked(self) -> bool:
        if self._proc is not None and self._proc.poll() is None:
            return True
        self._cleanup_dead_locked()
        path = Path(self.helper_path)
        if not path.is_file():
            print(f"   ⚠️  Apple Translation helper 不存在: {path}", file=sys.stderr)
            return False
        try:
            self._lines = queue.Queue()
            # stderr 继承父进程：协议只走 stdout，诊断原样进 subtitle.err.log。
            self._proc = subprocess.Popen(
                [str(path)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=None,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as e:
            print(f"   ⚠️  Apple Translation helper 启动失败: {e}", file=sys.stderr)
            self._proc = None
            return False

        self._reader = Thread(target=self._read_stdout, daemon=True, name="AppleTranslateReader")
        self._reader.start()

        deadline = time.monotonic() + self.startup_timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                print("   ⚠️  Apple Translation helper 启动超时", file=sys.stderr)
                self._kill_locked()
                return False
            try:
                line = self._get_line(remaining)
            except TimeoutError:
                print("   ⚠️  Apple Translation helper 启动超时", file=sys.stderr)
                self._kill_locked()
                return False
            if line is None:
                print("   ⚠️  Apple Translation helper 启动时提前退出", file=sys.stderr)
                self._kill_locked()
                return False
            try:
                data = json.loads(line)
            except ValueError:
                print(f"   ⚠️  Apple Translation helper ready 行不是 JSON: {line!r}", file=sys.stderr)
                continue
            if data.get("ready") is True:
                return True
            print(f"   ⚠️  Apple Translation helper ready 行异常: {data!r}", file=sys.stderr)

    def _read_stdout(self) -> None:
        proc = self._proc
        stdout = proc.stdout if proc is not None else None
        if stdout is None:
            self._lines.put(None)
            return
        try:
            for raw in stdout:
                self._lines.put(raw.rstrip("\n"))
        finally:
            self._lines.put(None)

    def _get_line(self, timeout: float) -> str | None:
        try:
            return self._lines.get(timeout=max(0.0, timeout))
        except queue.Empty:
            raise TimeoutError

    def _cleanup_dead_locked(self) -> None:
        proc = self._proc
        if proc is not None and proc.poll() is None:
            return
        self._proc = None

    def _kill_locked(self) -> None:
        proc = self._proc
        self._proc = None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=1)
        except Exception:
            pass

    def _request(self, payload: dict, timeout: float) -> dict | None:
        with self._lock:
            if not self._start_locked():
                return None
            proc = self._proc
            if proc is None or proc.stdin is None:
                return None
            req_id = self._next_id
            self._next_id += 1
            payload = dict(payload)
            payload["id"] = req_id
            try:
                proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                proc.stdin.flush()
            except (BrokenPipeError, OSError) as e:
                print(f"   ⚠️  Apple Translation helper 写入失败: {e}", file=sys.stderr)
                self._cleanup_dead_locked()
                return None

            deadline = time.monotonic() + timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    print(f"   ⚠️  Apple Translation helper 响应超时({timeout}秒)", file=sys.stderr)
                    # 串行协议里迟到响应会污染下一次请求，超时后必须重置进程。
                    self._kill_locked()
                    return None
                try:
                    line = self._get_line(remaining)
                except TimeoutError:
                    print(f"   ⚠️  Apple Translation helper 响应超时({timeout}秒)", file=sys.stderr)
                    self._kill_locked()
                    return None
                if line is None:
                    print("   ⚠️  Apple Translation helper 已退出", file=sys.stderr)
                    self._kill_locked()
                    return None
                try:
                    data = json.loads(line)
                except ValueError:
                    print(f"   ⚠️  Apple Translation helper 响应不是 JSON: {line!r}", file=sys.stderr)
                    continue
                resp_id = data.get("id")
                if "error" in data:
                    if resp_id not in (req_id, None):
                        print(
                            f"   ⚠️  Apple Translation helper 响应 id 不匹配: {resp_id} != {req_id}",
                            file=sys.stderr,
                        )
                        self._kill_locked()
                        return None
                    print(f"   ⚠️  Apple Translation helper 返回错误: {data.get('error')}", file=sys.stderr)
                    return None
                if resp_id != req_id:
                    print(
                        f"   ⚠️  Apple Translation helper 响应 id 不匹配: {resp_id} != {req_id}",
                        file=sys.stderr,
                    )
                    self._kill_locked()
                    return None
                return data

    def translate(self, text: str, src: str, dst: str, timeout: float) -> str | None:
        data = self._request({
            "op": "translate",
            "src": _helper_lang(src),
            "dst": _helper_lang(dst),
            "text": text,
        }, timeout)
        if not data:
            return None
        value = data.get("text")
        return value if isinstance(value, str) else None

    def status(self, src: str, dst: str, timeout: float = 5.0) -> str | None:
        data = self._request({
            "op": "status",
            "src": _helper_lang(src),
            "dst": _helper_lang(dst),
        }, timeout)
        if not data:
            return None
        value = data.get("status")
        return value if isinstance(value, str) else None

    def close(self) -> None:
        with self._lock:
            proc = self._proc
            self._proc = None
            if proc is None:
                return
            try:
                if proc.stdin:
                    proc.stdin.close()
            except Exception:
                pass
            try:
                proc.wait(timeout=_CLOSE_TIMEOUT)
            except subprocess.TimeoutExpired:
                try:
                    proc.kill()
                    proc.wait(timeout=1)
                except Exception:
                    pass

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:
            pass
