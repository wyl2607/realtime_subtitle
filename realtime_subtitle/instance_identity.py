"""Identify the current realtime-subtitle process without prefix matching.

Used by tests and as the matching rules the Windows stop script must follow.
Window titles are only a discovery hint; they never authorize a kill.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

_MAIN_ENTRY = re.compile(r"(?:^|[\\/\s])main\.py(?:\s|$)", re.I)
_OFFLINE_ENTRY = re.compile(r"download_subtitle\.py", re.I)


def parse_pid_file(raw: str) -> dict:
    """Accept JSON identity or a legacy bare PID."""
    text = (raw or "").strip()
    if not text:
        return {}
    try:
        data = json.loads(text)
    except ValueError:
        data = None
    if isinstance(data, dict) and "pid" in data:
        try:
            data["pid"] = int(data["pid"])
        except (TypeError, ValueError):
            return {}
        return data
    if re.fullmatch(r"\d+", text):
        return {"pid": int(text)}
    return {}


def is_our_interpreter(exe_path: str | None, repo_root) -> bool:
    """Exact venv\\Scripts\\python.exe (or pythonw), not a venv_backup sibling."""
    if not exe_path or not repo_root:
        return False
    try:
        actual = Path(exe_path).resolve()
        root = Path(repo_root).resolve()
    except OSError:
        return False
    expected = {
        (root / "venv" / "Scripts" / "python.exe").resolve(),
        (root / "venv" / "Scripts" / "pythonw.exe").resolve(),
    }
    return actual in expected


def is_realtime_command(command_line: str | None) -> bool:
    if not command_line:
        return False
    if _OFFLINE_ENTRY.search(command_line):
        return False
    return bool(_MAIN_ENTRY.search(command_line))


def confirm_realtime_process(
    exe: str | None,
    command_line: str | None,
    repo_root,
    window_title: str | None = None,
) -> bool:
    """Title is ignored for authorization; interpreter + entry must match."""
    del window_title
    return is_our_interpreter(exe, repo_root) and is_realtime_command(command_line)


def should_force_stop(recorded: dict | None, live: dict | None) -> bool:
    """True only when the live process is the recorded realtime instance."""
    if not recorded or not live:
        return False
    # A legacy bare PID is useful for discovery, never for authorization.
    rec_start = recorded.get("start_time")
    live_start = live.get("start_time")
    if not rec_start or not live_start:
        return False
    rec_pid = recorded.get("pid")
    live_pid = live.get("pid")
    if rec_pid is not None and live_pid is not None and int(rec_pid) != int(live_pid):
        return False
    if not confirm_realtime_process(
        live.get("exe") or recorded.get("exe"),
        live.get("command_line") or recorded.get("command_line"),
        recorded.get("repo_root"),
    ):
        return False
    if rec_start != live_start:
        return False
    return True
