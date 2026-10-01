"""macOS 平台桥接；offscreen 不把伪 winId 交给 Cocoa。"""
import ctypes
import subprocess
import sys
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import torch  # noqa: F401  保留先于 Qt 的 DLL 导入顺序
from PyQt6.QtCore import QPoint, QPointF, QRect, Qt, QEvent
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS 专用桥接")
# ☠️ QApplication 引用必须持在模块级，防止 GC 后构造 QWidget qFatal。
_APP = QApplication.instance() or QApplication([])


def _pump_until(predicate):
    deadline = time.monotonic() + 2
    while not predicate() and time.monotonic() < deadline:
        _APP.processEvents()
    assert predicate()


class FakeCarbon:
    def __init__(self, failures=()):
        self.failures = failures
        self.registrations = []
        self.unregistered = []
        self.removed = []
        self.event_id = 1

    def GetEventDispatcherTarget(self):
        return 100

    def InstallEventHandler(self, target, callback, count, spec, data, ref):
        self.callback = callback
        assert (spec._obj.eventClass, spec._obj.eventKind) == (int.from_bytes(b"keyb", "big"), 6)
        ref._obj.value = 200
        return 0

    def RegisterEventHotKey(self, key, modifiers, identity, target, options, ref):
        self.registrations.append((key, modifiers, identity.id, identity.signature))
        if identity.id in self.failures:
            return -9878
        ref._obj.value = 300 + identity.id
        return 0

    def GetEventParameter(self, event, name, kind, actual, size, actual_size, out):
        assert name == int.from_bytes(b"----", "big")
        assert kind == int.from_bytes(b"hkid", "big")
        assert size == 8
        out._obj.signature = int.from_bytes(b"RSUB", "big")
        out._obj.id = self.event_id
        return 0

    def UnregisterEventHotKey(self, ref):
        self.unregistered.append(ref.value)
        return 0

    def RemoveEventHandler(self, ref):
        self.removed.append(ref.value)
        return 0


def test_hotkeys_registration_failure_dispatch_and_cleanup(capsys):
    from realtime_subtitle.macos.hotkeys import CarbonHotkeys
    threads = []
    callback = lambda: threads.append(threading.get_ident())
    carbon = FakeCarbon(failures=(3,))
    hotkeys = CarbonHotkeys({i: (letter, callback) for i, letter in enumerate("PLMGC", 1)}, carbon)
    assert hotkeys.register() == ["Ctrl+Option+P", "Ctrl+Option+L", "Ctrl+Option+G", "Ctrl+Option+C"]
    assert [row[0] for row in carbon.registrations] == [35, 37, 46, 5, 8]
    assert all(row[1] == 0x1800 for row in carbon.registrations)
    assert "Ctrl+Option+M 注册失败" in capsys.readouterr().out
    worker = threading.Thread(target=lambda: carbon.callback(None, None, None))
    worker.start()
    worker.join()
    assert not threads  # queued connection，即使 Carbon 回调来自其它线程也不直接跑 UI
    _pump_until(lambda: bool(threads))
    assert threads == [threading.get_ident()]
    carbon.event_id = 999
    assert carbon.callback(None, None, None) == -9874
    hotkeys.close()
    hotkeys.close()
    assert carbon.unregistered == [301, 302, 304, 305]
    assert carbon.removed == [200]


def test_carbon_ctypes_signatures(monkeypatch):
    from realtime_subtitle.macos.hotkeys import load_carbon, EventHotKeyID
    carbon = SimpleNamespace(**{name: Mock() for name in (
        "GetEventDispatcherTarget", "InstallEventHandler", "RegisterEventHotKey",
        "GetEventParameter", "UnregisterEventHotKey", "RemoveEventHandler")})
    monkeypatch.setattr(ctypes, "CDLL", lambda path: carbon)
    assert load_carbon() is carbon
    assert carbon.GetEventDispatcherTarget.restype is ctypes.c_void_p
    assert carbon.RegisterEventHotKey.argtypes[2] is EventHotKeyID
    assert ctypes.sizeof(EventHotKeyID) == 8


def test_native_overlay_flags_and_focus(monkeypatch):
    from AppKit import (
        NSStatusWindowLevel, NSWindowCollectionBehaviorCanJoinAllSpaces,
        NSWindowCollectionBehaviorFullScreenAuxiliary, NSWindowCollectionBehaviorStationary,
    )
    from realtime_subtitle.macos import windows
    native = Mock()
    monkeypatch.setattr(windows, "native_window", lambda widget: native)
    assert windows.configure_overlay(object(), click_through=True)
    native.setLevel_.assert_called_once_with(NSStatusWindowLevel)
    native.setCollectionBehavior_.assert_called_once_with(
        NSWindowCollectionBehaviorCanJoinAllSpaces | NSWindowCollectionBehaviorFullScreenAuxiliary
        | NSWindowCollectionBehaviorStationary)
    native.setIgnoresMouseEvents_.assert_called_once_with(True)
    native.setHidesOnDeactivate_.assert_called_once_with(False)
    assert windows.set_click_through(object(), False)
    native.setIgnoresMouseEvents_.assert_called_with(False)


def test_offscreen_never_wraps_fake_native_pointer(monkeypatch):
    import objc
    from realtime_subtitle.macos.windows import native_window
    wrapper = Mock(side_effect=AssertionError("offscreen 不是 NSView"))
    monkeypatch.setattr(objc, "objc_object", wrapper)
    monkeypatch.setattr(QApplication, "platformName", lambda: "offscreen")
    assert native_window(Mock()) is None
    wrapper.assert_not_called()


def test_single_instance_subprocess_and_reacquire(tmp_path):
    from realtime_subtitle.instance_identity import acquire_macos_instance_lock
    path = tmp_path / ".subtitle.lock"
    lock = acquire_macos_instance_lock(path)
    assert lock is not None
    code = ("from realtime_subtitle.instance_identity import acquire_macos_instance_lock; "
            "import sys; sys.exit(0 if acquire_macos_instance_lock(sys.argv[1]) is None else 1)")
    result = subprocess.run([sys.executable, "-c", code, str(path)], capture_output=True)
    assert result.returncode == 0, result.stderr.decode()
    lock.close()
    replacement = acquire_macos_instance_lock(path)
    assert replacement is not None
    replacement.close()
    assert path.exists()  # 不 unlink：下次仍竞争同一个 inode


def test_retina_legacy_and_logical_multiscreen_restore(monkeypatch):
    from realtime_subtitle.ui import window_geometry as geometry
    legacy = {"x": -1800, "y": -400, "w": 1000, "h": 200, "font_size": 32,
              "settings_geo": [-1600, -300, 560, 900], "coord_dpr": 1.0}
    restored = geometry.rescale_state_for_dpr(legacy, 2.0)
    assert [restored[k] for k in ("x", "y", "w", "h", "font_size")] == [-900, -200, 500, 100, 16]
    assert restored["settings_geo"] == [-800, -150, 280, 450]
    logical = dict(legacy, coord_platform="darwin", coord_dpr=2.0)
    assert geometry.rescale_state_for_dpr(logical, 1.0) == logical
    screen = SimpleNamespace(availableGeometry=lambda: QRect(-1920, 0, 1920, 1080))
    monkeypatch.setattr(QApplication, "screenAt", lambda pos: screen if pos.x() < 0 else None)
    assert geometry._clamp_geo_to_any_screen(-1800, 900, 1000, 200) == (-1800, 880, 1000, 200)


def _mouse(kind, x, y, button=Qt.MouseButton.LeftButton, buttons=Qt.MouseButton.LeftButton):
    return QMouseEvent(kind, QPointF(x, y), QPointF(x, y), button, buttons, Qt.KeyboardModifier.NoModifier)


@pytest.mark.parametrize("system_move", [True, False])
def test_click_vs_drag_threshold_and_return_to_origin(monkeypatch, system_move):
    from realtime_subtitle.ui.window_frame import DraggableWidget
    widget = DraggableWidget()
    callback = Mock()
    widget.on_click = callback
    handle = Mock()
    handle.startSystemMove.return_value = system_move
    monkeypatch.setattr(widget, "windowHandle", lambda: handle)
    widget.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 100, 100))
    widget.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 102, 102))
    handle.startSystemMove.assert_not_called()
    widget.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 102, 102))
    callback.assert_called_once()
    callback.reset_mock()
    widget.mousePressEvent(_mouse(QEvent.Type.MouseButtonPress, 100, 100))
    widget.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 106, 100))
    widget.mouseMoveEvent(_mouse(QEvent.Type.MouseMove, 100, 100))
    widget.mouseReleaseEvent(_mouse(QEvent.Type.MouseButtonRelease, 100, 100))
    handle.startSystemMove.assert_called_once()
    callback.assert_not_called()  # 拖出去再回原位，仍然是拖动
    widget.close()


@pytest.mark.parametrize("pos,edges,cursor", [
    ((0, 0), Qt.Edge.LeftEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeFDiagCursor),
    ((299, 0), Qt.Edge.RightEdge | Qt.Edge.TopEdge, Qt.CursorShape.SizeBDiagCursor),
    ((0, 199), Qt.Edge.LeftEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeBDiagCursor),
    ((299, 199), Qt.Edge.RightEdge | Qt.Edge.BottomEdge, Qt.CursorShape.SizeFDiagCursor),
    ((0, 100), Qt.Edge.LeftEdge, Qt.CursorShape.SizeHorCursor),
    ((299, 100), Qt.Edge.RightEdge, Qt.CursorShape.SizeHorCursor),
    ((150, 0), Qt.Edge.TopEdge, Qt.CursorShape.SizeVerCursor),
    ((150, 199), Qt.Edge.BottomEdge, Qt.CursorShape.SizeVerCursor),
])
def test_edge_cursor_and_resize_fallback(pos, edges, cursor):
    from realtime_subtitle.ui.window_frame import ResizableFramelessWidget
    widget = ResizableFramelessWidget()
    widget.setGeometry(-500, 100, 300, 200)
    assert widget._macos_edges_at(QPoint(*pos)) == edges
    assert widget._macos_resize_cursor(edges) == cursor
    widget._resize_edges = edges
    widget._resize_origin = QPoint(0, 0)
    widget._resize_geometry = widget.geometry()
    widget._macos_resize_to(QPoint(20, 10))
    assert widget.width() == (280 if edges & Qt.Edge.LeftEdge else 320 if edges & Qt.Edge.RightEdge else 300)
    assert widget.height() == (190 if edges & Qt.Edge.TopEdge else 210 if edges & Qt.Edge.BottomEdge else 200)
    widget.close()


def test_font_fallback_only_replaces_windows_names():
    from realtime_subtitle.ui.platform_fonts import platform_font_family
    assert platform_font_family("Microsoft YaHei, Arial") == "PingFang SC, Helvetica"
    assert platform_font_family('"Segoe UI", "Microsoft YaHei UI"') == '"Helvetica", "PingFang SC"'
    assert platform_font_family("Noto Sans, serif") == "Noto Sans, serif"


def test_native_view_resolution_and_accessory_focus(monkeypatch):
    import AppKit
    import objc
    from realtime_subtitle.macos import windows
    widget = Mock()
    widget.winId.return_value = 123
    view = Mock()
    wrapper = Mock(return_value=view)
    monkeypatch.setattr(objc, "objc_object", wrapper)
    monkeypatch.setattr(QApplication, "platformName", lambda: "cocoa")
    assert windows.native_window(widget) is view.window.return_value
    wrapper.assert_called_once_with(c_void_p=123)
    application = Mock()
    monkeypatch.setattr(AppKit, "NSApplication", SimpleNamespace(sharedApplication=lambda: application))
    windows.accessory_app()
    application.setActivationPolicy_.assert_called_once_with(AppKit.NSApplicationActivationPolicyAccessory)
    windows.focus_window(widget)
    application.activateIgnoringOtherApps_.assert_called_once_with(True)
    view.window.return_value.makeKeyAndOrderFront_.assert_called_once_with(None)
    widget.activateWindow.assert_called_once()


def test_overlay_clickthrough_indicator_and_state(monkeypatch, tmp_path):
    from realtime_subtitle import config
    from realtime_subtitle.ui import subtitle_window as sw
    from realtime_subtitle.macos import windows
    for name in sw.TUNING_KEYS:
        monkeypatch.setattr(config, name, getattr(config, name))
    monkeypatch.setattr(sw, "STATE_FILE", str(tmp_path / "window_state.json"))
    native = Mock()
    monkeypatch.setattr(windows, "native_window", lambda widget: native)
    window = sw.SubtitleWindow()
    window._state_timer.stop()
    try:
        window.container.show()
        window._toggle_click_through()
        assert window._click_through
        assert window.ct_indicator.isVisible()
        native.setIgnoresMouseEvents_.assert_called_with(True)
        window.container.hide()
        window.container.show()
        native.setIgnoresMouseEvents_.assert_called_with(True)
        window._toggle_click_through()
        assert not window._click_through
        assert not window.ct_indicator.isVisible()
        window._save_state_if_changed()
        assert window._last_saved_state["coord_platform"] == "darwin"
        assert window._last_saved_state["coord_dpr"] == window.container.devicePixelRatioF()
        window.cinema_bar.show()
        native.setIgnoresMouseEvents_.assert_called_with(True)
    finally:
        window.container.on_system_close = None
        for widget in (window.container, window.settings_window, window.history_window,
                       window.tv_window, window.cinema_bar, window.word_popup, window.ai_analysis_popup):
            widget.hide()


@pytest.mark.parametrize("supported", [True, False])
def test_resize_event_filter_calls_system_api_and_falls_back(monkeypatch, supported):
    from realtime_subtitle.ui.window_frame import ResizableFramelessWidget
    widget = ResizableFramelessWidget()
    widget.setGeometry(0, 0, 300, 200)
    handle = Mock()
    handle.startSystemResize.return_value = supported
    monkeypatch.setattr(widget, "windowHandle", lambda: handle)
    assert widget.eventFilter(widget, _mouse(QEvent.Type.MouseButtonPress, 299, 199))
    handle.startSystemResize.assert_called_once_with(Qt.Edge.RightEdge | Qt.Edge.BottomEdge)
    assert widget.eventFilter(widget, _mouse(QEvent.Type.MouseMove, 319, 209))
    assert widget.size().width() == (300 if supported else 320)
    assert widget.size().height() == (200 if supported else 210)
    assert widget.eventFilter(widget, _mouse(QEvent.Type.MouseButtonRelease, 319, 209))
    assert widget._resize_origin is None
    widget.close()
