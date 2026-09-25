"""全测试共享的守卫。

☠️ 仓库根上的 subtitle.pid / .stop / .paused 是**正在运行的那份实时字幕**和
start/stop/pause 脚本之间的跨进程约定（见 realtime_subtitle/paths.py）。测试
一旦碰了真文件，伤的是用户此刻开着的程序：

2026-09-26 实测：test_game_mode 里一条用例对假 SubtitleApp 调了真 stop()，
stop() 按路径删掉仓库根的 subtitle.pid——字幕开着时每跑一次测试，pid 文件就
没了。随后 启动字幕.bat 查不到身份，判定"没在运行"，又起一个实例（被单实例
mutex 挡下、却照样打印"已启动"）；停止字幕.bat 只能退回按窗口标题找。

所以每条用例前后都比对这三个文件：被动过就**先原样恢复**（别让测试继续伤
着用户开着的程序），再判红并点名是哪条用例。
"""
from pathlib import Path

import pytest

from realtime_subtitle.paths import REPO_ROOT

_GUARDED = ("subtitle.pid", ".stop", ".paused")


def _snapshot():
    snap = {}
    for name in _GUARDED:
        p = Path(REPO_ROOT) / name
        try:
            snap[name] = p.read_bytes()
        except OSError:
            snap[name] = None
    return snap


@pytest.fixture(autouse=True)
def _guard_live_runtime_files(request):
    before = _snapshot()
    yield
    after = _snapshot()
    changed = [n for n in _GUARDED if before[n] != after[n]]
    if not changed:
        return
    for name in changed:
        p = Path(REPO_ROOT) / name
        if before[name] is None:
            p.unlink(missing_ok=True)
        else:
            p.write_bytes(before[name])
    pytest.fail(f"{request.node.nodeid} 动了仓库根的运行时文件 {changed}"
                f"（已恢复）。这些是正在运行的实时字幕和脚本之间的约定，"
                f"测试里要把路径换到 tmp_path。")
