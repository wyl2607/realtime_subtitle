import ctypes
import sys

import pytest

from realtime_subtitle.ui import macos_hotkeys

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="Carbon hotkeys only exist on macOS")


@pytest.fixture(autouse=True)
def reset_hotkeys(monkeypatch):
    fake = FakeCarbon()
    monkeypatch.setattr(macos_hotkeys, "_carbon", fake)
    monkeypatch.setattr(macos_hotkeys, "_configured", False)
    monkeypatch.setattr(macos_hotkeys, "_target", None)
    monkeypatch.setattr(macos_hotkeys, "_handler_ref", macos_hotkeys.EventHandlerRef())
    monkeypatch.setattr(macos_hotkeys, "_handler_proc", None)
    macos_hotkeys._registered_refs.clear()
    macos_hotkeys._handlers_by_id.clear()
    yield fake
    macos_hotkeys._registered_refs.clear()
    macos_hotkeys._handlers_by_id.clear()


class FakeCarbon:
    def __init__(self):
        self.registered = []
        self.unregistered = []
        self.fail_ids = set()
        self.callback = None
        self.current_hotkey_id = None

    def GetApplicationEventTarget(self):
        return ctypes.c_void_p(42)

    def InstallEventHandler(self, _target, callback, _count, _spec, _user_data, out_handler):
        self.callback = callback
        ctypes.cast(out_handler, ctypes.POINTER(macos_hotkeys.EventHandlerRef)).contents.value = 99
        return 0

    def RegisterEventHotKey(self, keycode, modifiers, hotkey_id, _target, _options, out_ref):
        self.registered.append((keycode, modifiers, hotkey_id.id))
        if hotkey_id.id in self.fail_ids:
            return -9876
        ctypes.cast(out_ref, ctypes.POINTER(macos_hotkeys.EventHotKeyRef)).contents.value = (
            1000 + hotkey_id.id
        )
        return 0

    def UnregisterEventHotKey(self, ref):
        self.unregistered.append(ref.value if hasattr(ref, "value") else ref)
        return 0

    def GetEventParameter(
        self,
        _event,
        _name,
        _param_type,
        _actual_type,
        _buffer_size,
        _actual_size,
        out_data,
    ):
        hotkey_id = ctypes.cast(
            out_data,
            ctypes.POINTER(macos_hotkeys.EventHotKeyID),
        ).contents
        hotkey_id.signature = macos_hotkeys._SIGNATURE
        hotkey_id.id = self.current_hotkey_id
        return 0

    def press(self, hotkey_id):
        self.current_hotkey_id = hotkey_id
        return self.callback(None, ctypes.c_void_p(123), None)


def test_registers_five_hotkeys_with_expected_keycodes_and_modifiers(reset_hotkeys):
    fake = reset_hotkeys
    handlers = {label: (lambda: None) for label in [
        "Ctrl+Alt+P",
        "Ctrl+Alt+L",
        "Ctrl+Alt+M",
        "Ctrl+Alt+G",
        "Ctrl+Alt+C",
    ]}

    assert macos_hotkeys.register(handlers) == list(handlers)

    assert fake.registered == [
        (macos_hotkeys.kVK_ANSI_P, macos_hotkeys.controlKey | macos_hotkeys.optionKey, 1),
        (macos_hotkeys.kVK_ANSI_L, macos_hotkeys.controlKey | macos_hotkeys.optionKey, 2),
        (macos_hotkeys.kVK_ANSI_M, macos_hotkeys.controlKey | macos_hotkeys.optionKey, 3),
        (macos_hotkeys.kVK_ANSI_G, macos_hotkeys.controlKey | macos_hotkeys.optionKey, 4),
        (macos_hotkeys.kVK_ANSI_C, macos_hotkeys.controlKey | macos_hotkeys.optionKey, 5),
    ]


def test_callback_dispatches_by_hotkey_id(reset_hotkeys):
    fake = reset_hotkeys
    calls = []
    macos_hotkeys.register({
        "Ctrl+Alt+P": lambda: calls.append("pause"),
        "Ctrl+Alt+L": lambda: calls.append("language"),
        "Ctrl+Alt+M": lambda: calls.append("mouse"),
        "Ctrl+Alt+G": lambda: calls.append("perf"),
        "Ctrl+Alt+C": lambda: calls.append("cinema"),
    })

    fake.press(4)
    fake.press(1)
    fake.press(5)

    assert calls == ["perf", "pause", "cinema"]


def test_unregister_all_unregisters_each_registered_hotkey(reset_hotkeys):
    fake = reset_hotkeys
    macos_hotkeys.register({
        "Ctrl+Alt+P": lambda: None,
        "Ctrl+Alt+L": lambda: None,
        "Ctrl+Alt+M": lambda: None,
        "Ctrl+Alt+G": lambda: None,
        "Ctrl+Alt+C": lambda: None,
    })

    macos_hotkeys.unregister_all()

    assert fake.unregistered == [1001, 1002, 1003, 1004, 1005]
    assert macos_hotkeys._registered_refs == {}
    assert macos_hotkeys._handlers_by_id == {}


def test_register_failure_only_skips_that_hotkey(reset_hotkeys, capsys):
    fake = reset_hotkeys
    fake.fail_ids.add(3)
    handlers = {
        "Ctrl+Alt+P": lambda: None,
        "Ctrl+Alt+L": lambda: None,
        "Ctrl+Alt+M": lambda: None,
        "Ctrl+Alt+G": lambda: None,
        "Ctrl+Alt+C": lambda: None,
    }

    assert macos_hotkeys.register(handlers) == [
        "Ctrl+Alt+P",
        "Ctrl+Alt+L",
        "Ctrl+Alt+G",
        "Ctrl+Alt+C",
    ]

    assert "快捷键 Ctrl+Alt+M 注册失败" in capsys.readouterr().out
    assert set(macos_hotkeys._registered_refs) == {1, 2, 4, 5}


def test_real_carbon_register_smoke(monkeypatch):
    from PyQt6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])
    assert app is not None
    macos_hotkeys.unregister_all()
    monkeypatch.setattr(macos_hotkeys, "_carbon", None)
    monkeypatch.setattr(macos_hotkeys, "_configured", False)
    monkeypatch.setattr(macos_hotkeys, "_target", None)
    monkeypatch.setattr(macos_hotkeys, "_handler_ref", macos_hotkeys.EventHandlerRef())
    monkeypatch.setattr(macos_hotkeys, "_handler_proc", None)

    # F19 不在生产热键表里：只为冒烟临时加，避免和用户正在用的组合冲突
    monkeypatch.setitem(macos_hotkeys._HOTKEYS, "Ctrl+Alt+F19", (19, 0x50))  # kVK_F19
    registered = macos_hotkeys.register({"Ctrl+Alt+F19": lambda: None})
    if not registered:
        pytest.skip("Carbon 未能注册 Ctrl+Alt+F19；ssh/offscreen 会话可能没有 GUI 事件目标")
    macos_hotkeys.unregister_all()


def test_hotkeys_ignored_until_models_loaded(monkeypatch):
    """Carbon 要求构造期在主线程注册，但 running 之前按键要像 Windows 一样无效。"""
    import os
    os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    import realtime_subtitle.app as app
    from realtime_subtitle.ui import macos_hotkeys

    captured = {}
    monkeypatch.setattr(app.sys, "platform", "darwin")
    monkeypatch.setattr(macos_hotkeys, "register", lambda h: captured.update(h) or list(h))

    calls = []
    fake = type("A", (), {})()
    fake.running = False
    fake._toggle_pause = lambda: calls.append("P")
    fake._switch_language = lambda: calls.append("L")
    fake._toggle_perf_hotkey = lambda: calls.append("G")
    fake.subtitle_window = type("W", (), {"toggle_click_through": lambda self: calls.append("M"),
                                          "toggle_cinema": lambda self: calls.append("C")})()
    app.SubtitleApp._setup_hotkey(fake)

    captured["Ctrl+Alt+L"]()
    assert calls == []
    fake.running = True
    captured["Ctrl+Alt+L"]()
    captured["Ctrl+Alt+M"]()
    assert calls == ["L", "M"]
