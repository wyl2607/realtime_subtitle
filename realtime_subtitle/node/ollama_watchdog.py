"""Ollama 翻译服务看护：探测失败时自动拉起本机 `ollama serve`。

只在 translator 以 `ollama:` 开头时工作。探测连续失败 2 次（两次 tick）才动作；
成功即清零。两次拉起至少间隔 300 秒（含失败）。找不到可执行文件只记一次
`ollama_restart_skipped reason=not_found` 后不再尝试。
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

log = logging.getLogger("realtime_subtitle.node.ollama_watchdog")

_RESTART_COOLDOWN_S = 300.0
_MAX_CONSECUTIVE_FAILURES = 2


class OllamaWatchdog:
    def __init__(
        self,
        translator: str,
        *,
        probe: Callable[[], bool],
        spawn: Callable[[], None],
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._translator = translator
        self._probe = probe
        self._spawn = spawn
        self._clock = clock

        self._consecutive_failures = 0
        self._last_restart_at: float | None = None
        self._exe_logged_not_found = False
        self._exe_path: Path | None = None

    def tick(self) -> None:
        """每 60 秒由 gateway 调用一次。"""
        if not self._translator.startswith("ollama:"):
            return

        if self._probe():
            if self._consecutive_failures > 0:
                log.info("ollama_probe_recovered")
            self._consecutive_failures = 0
            return

        self._consecutive_failures += 1
        if self._consecutive_failures < _MAX_CONSECUTIVE_FAILURES:
            return

        now = self._clock()
        if self._last_restart_at is not None and now - self._last_restart_at < _RESTART_COOLDOWN_S:
            return

        exe = self._find_ollama_exe()
        if exe is None:
            if not self._exe_logged_not_found:
                log.warning("ollama_restart_skipped reason=not_found")
                self._exe_logged_not_found = True
            return

        attempt = 1 if self._last_restart_at is None else int((now - self._last_restart_at) / _RESTART_COOLDOWN_S) + 1
        log.info("ollama_restart attempt=%d", attempt)
        self._last_restart_at = now
        try:
            self._spawn()
        except Exception as e:  # noqa: BLE001 - 记日志但不阻塞 tick
            log.warning("ollama_restart_failed err=%s", type(e).__name__)

    def _find_ollama_exe(self) -> Path | None:
        if self._exe_path is not None:
            return self._exe_path
        if self._exe_logged_not_found:
            return None

        exe = _find_ollama_exe_default()
        if exe is not None:
            self._exe_path = exe
        return exe


def default_spawn() -> None:
    """默认的 spawn 实现：Popen 启动 `ollama serve`，去掉 OLLAMA_HOST 环境变量。"""
    exe = _find_ollama_exe_default()
    if exe is None:
        raise FileNotFoundError("ollama executable not found")

    env = dict(os.environ)
    env.pop("OLLAMA_HOST", None)
    # cwd 必须在节点目录之外：子进程继承网关的 cwd（~/rs-node），Windows 上被当作 cwd 的目录
    # 不能改名，节点重装时 Move-Item rs-node → rs-node.prev 就会失败回滚（2026-10-13 真机）
    cwd = str(exe.parent)

    if sys.platform == "win32":
        creationflags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        subprocess.Popen(
            [str(exe), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
            env=env,
            cwd=cwd,
        )
    else:
        subprocess.Popen(
            [str(exe), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            env=env,
            cwd=cwd,
        )


def _find_ollama_exe_default() -> Path | None:
    """供 default_spawn 使用的可执行文件查找（与 OllamaWatchdog 逻辑一致）。"""
    candidates: list[Path] = []
    if sys.platform == "win32":
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            candidates.append(Path(localappdata) / "Programs" / "Ollama" / "ollama.exe")
    else:
        candidates.extend([
            Path("/opt/homebrew/bin/ollama"),
            Path("/usr/local/bin/ollama"),
            Path("/Applications/Ollama.app/Contents/Resources/ollama"),
        ])

    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return c
    return None


def create_macos_launchctl_spawn() -> Callable[[], None]:
    """macOS 上若 brew services 管 ollama，优先用 launchctl kickstart；失败再用 serve。"""
    import subprocess

    def spawn() -> None:
        uid = os.getuid()
        try:
            subprocess.run(
                ["launchctl", "kickstart", "-k", f"gui/{uid}/homebrew.mxcl.ollama"],
                check=True,
                capture_output=True,
                timeout=5.0,
            )
            return
        except Exception:
            pass
        default_spawn()

    return spawn