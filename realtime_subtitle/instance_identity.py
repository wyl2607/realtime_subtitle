"""Identify the current realtime-subtitle process without prefix matching.

Used by tests and as the matching rules the Windows stop script must follow.
Window titles are only a discovery hint; they never authorize a kill.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

_OFFLINE_ENTRY = "download_subtitle.py"
_MAIN_ENTRY = "main.py"


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


def split_windows_command_line(command_line: str) -> list[str]:
    """Split a CreateProcess/WMI command line the way CommandLineToArgvW does."""
    if not command_line:
        return []
    if os.name == "nt":
        try:
            return _split_command_line_win32(command_line)
        except OSError:
            pass
    return _split_command_line_portable(command_line)


def _split_command_line_win32(command_line: str) -> list[str]:
    import ctypes

    argc = ctypes.c_int()
    CommandLineToArgvW = ctypes.windll.shell32.CommandLineToArgvW
    CommandLineToArgvW.restype = ctypes.POINTER(ctypes.c_wchar_p)
    CommandLineToArgvW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    argv = CommandLineToArgvW(command_line, ctypes.byref(argc))
    if not argv:
        raise OSError("CommandLineToArgvW failed")
    try:
        return [argv[i] for i in range(argc.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(argv)


def _split_command_line_portable(command_line: str) -> list[str]:
    args: list[str] = []
    i = 0
    n = len(command_line)
    while i < n:
        while i < n and command_line[i] in " \t":
            i += 1
        if i >= n:
            break
        token: list[str] = []
        in_quotes = False
        while i < n:
            char = command_line[i]
            if not in_quotes and char in " \t":
                break
            if char == "\\":
                slashes = 0
                while i < n and command_line[i] == "\\":
                    slashes += 1
                    i += 1
                if i < n and command_line[i] == '"':
                    token.append("\\" * (slashes // 2))
                    if slashes % 2 == 0:
                        in_quotes = not in_quotes
                    else:
                        token.append('"')
                    i += 1
                else:
                    token.append("\\" * slashes)
                continue
            if char == '"':
                in_quotes = not in_quotes
                i += 1
                continue
            token.append(char)
            i += 1
        args.append("".join(token))
    return args


def python_entry_script(command_line: str | None) -> str | None:
    """Return the script path Python would execute, or None if it is not a file entry."""
    argv = split_windows_command_line(command_line or "")
    if len(argv) < 2:
        return None
    index = 1
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            return argv[index + 1] if index + 1 < len(argv) else None
        if arg == "-":
            return None
        if arg.startswith("--"):
            if arg == "--check-hash-based-pycs":
                index += 2
                continue
            index += 1
            continue
        if arg.startswith("-c") or arg.startswith("-m"):
            return None
        if arg in {"-W", "-X"}:
            index += 2
            continue
        if arg.startswith("-"):
            index += 1
            continue
        return arg
    return None


def _is_bare_filename(path: str) -> bool:
    return Path(path).name == path and "/" not in path and "\\" not in path


def is_realtime_command(command_line: str | None, repo_root=None) -> bool:
    script = python_entry_script(command_line)
    if not script:
        return False
    name = Path(script).name.lower()
    if name == _OFFLINE_ENTRY:
        return False
    if name != _MAIN_ENTRY:
        return False
    if _is_bare_filename(script):
        # 启动器契约：python.exe -u main.py。带目录的相对路径不能猜工作目录。
        return True
    if repo_root is None:
        return False
    try:
        actual = Path(script).resolve()
        expected = (Path(repo_root) / "main.py").resolve()
    except OSError:
        return False
    return os.path.normcase(str(actual)) == os.path.normcase(str(expected))


def confirm_realtime_process(
    exe: str | None,
    command_line: str | None,
    repo_root,
    window_title: str | None = None,
) -> bool:
    """Title is ignored for authorization; interpreter + entry must match."""
    del window_title
    return is_our_interpreter(exe, repo_root) and is_realtime_command(command_line, repo_root)


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
