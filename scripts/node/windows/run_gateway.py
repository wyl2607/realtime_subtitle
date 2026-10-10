"""计划任务用 pythonw.exe 启动 gateway 的入口：没有控制台窗口，日志写文件。

pythonw 没有 stderr 句柄，gateway 的日志和 worker 子进程继承的 stderr 都会丢。
这里把 OS 级标准错误句柄（SetStdHandle）指向日志文件，子进程也就继承它。

用法（工作目录 = 仓库根）：pythonw.exe scripts\\node\\windows\\run_gateway.py --token-file <路径>
"""

import ctypes
import msvcrt
import os
import sys
from pathlib import Path

LOG_DIR = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "rs-node" / "logs"
LOG_MAX_BYTES = 5 * 1024 * 1024
STD_ERROR_HANDLE = -12


def _open_log():
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / "gateway.log"
    if path.exists() and path.stat().st_size > LOG_MAX_BYTES:
        os.replace(path, LOG_DIR / "gateway.log.1")
    return open(path, "a", encoding="utf-8", buffering=1)  # noqa: SIM115 - 进程生命周期内常开


def main():
    log = _open_log()
    ctypes.windll.kernel32.SetStdHandle(STD_ERROR_HANDLE, msvcrt.get_osfhandle(log.fileno()))
    os.dup2(log.fileno(), 2)
    sys.stderr = log
    sys.path.insert(0, os.getcwd())
    from realtime_subtitle.node import gateway

    gateway.main(sys.argv[1:])


if __name__ == "__main__":
    main()
