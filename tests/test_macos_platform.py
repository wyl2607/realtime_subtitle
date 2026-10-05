"""macOS 平台隔离：单实例锁、热键跳过、鼠标穿透走 Qt 标志（不建真窗口）。"""
import os
import sys

import pytest

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
import realtime_subtitle.app as app  # noqa: E402
from realtime_subtitle.ui import subtitle_window  # noqa: E402
from PyQt6.QtCore import Qt  # noqa: E402

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="fcntl 只在 POSIX 上有")


def test_second_singleton_lock_exits(tmp_path):
    lock_file = tmp_path / "rs.lock"
    first = app._acquire_macos_singleton_lock(lock_file)
    try:
        with pytest.raises(SystemExit):
            app._acquire_macos_singleton_lock(lock_file)
    finally:
        first.close()
    # 第一把释放后应该能重新拿到（进程退出即释放，不留死锁文件）
    app._acquire_macos_singleton_lock(lock_file).close()


def test_setup_hotkey_skips_win32_api_off_windows(monkeypatch, capsys):
    monkeypatch.setattr(app.sys, "platform", "darwin")

    class _Boom:
        def __getattr__(self, name):
            raise AssertionError("非 win32 不应碰 ctypes.windll")

    import ctypes
    monkeypatch.setattr(ctypes, "windll", _Boom(), raising=False)
    app.SubtitleApp._setup_hotkey(object())
    assert "暂不支持全局快捷键" in capsys.readouterr().out


class _FakeContainer:
    def __init__(self):
        self.flags = Qt.WindowType.FramelessWindowHint
        self.shown = 0

    def windowFlags(self):
        return self.flags

    def setWindowFlags(self, flags):
        self.flags = flags

    def show(self):
        self.shown += 1

    def raise_(self):
        pass


def test_click_through_toggles_qt_input_transparency(monkeypatch):
    monkeypatch.setattr(subtitle_window.sys, "platform", "darwin")
    fake = type("W", (), {})()
    fake.container = _FakeContainer()
    apply = subtitle_window.SubtitleWindow._apply_click_through_native
    transparent = Qt.WindowType.WindowTransparentForInput

    apply(fake, True)
    assert fake.container.flags & transparent
    assert fake.container.flags & Qt.WindowType.WindowStaysOnTopHint
    apply(fake, False)
    assert not (fake.container.flags & transparent)
    assert fake.container.flags & Qt.WindowType.WindowStaysOnTopHint
    assert fake.container.shown == 2  # 改 windowFlags 会藏窗，每次都要重新 show
