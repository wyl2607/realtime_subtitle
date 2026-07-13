"""场景预设（直播/看剧/游戏/精听）单测。

运行: venv\\Scripts\\python.exe -m pytest test_presets.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114。
⚠️ QApplication 必须持有模块级引用，否则被 GC 后建 QWidget 触发 qFatal。
"""
import torch  # noqa: F401  先于 PyQt5
import sys

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication

import config
from subtitle_window import (
    SettingsWindow,
    TUNING_KEYS,
    PRESET_CONTROL_ATTRS,
)


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _snap_keys(*keys):
    return {k: getattr(config, k) for k in keys}


def _restore(snap):
    for k, v in snap.items():
        setattr(config, k, v)


_PRESET_KEYS = (
    "CHUNK_SUBMIT_SECONDS",
    "IDLE_FLUSH_SEC",
    "MAX_SENTENCE_PAIRS",
    "SHOW_BILINGUAL",
    "DRAFT_TRANSLATION",
)


def test_presets_keys_in_tuning_and_have_controls():
    """每个预设的键都在 TUNING_KEYS 里且面板有对应控件（防加键漏接）。"""
    _app()
    presets = getattr(config, "PRESETS", None)
    assert isinstance(presets, dict) and presets, "config.PRESETS 缺失"
    for name, params in presets.items():
        assert isinstance(params, dict) and params, f"预设 {name} 为空"
        for key in params:
            assert key in TUNING_KEYS, f"预设 {name} 的键 {key} 不在 TUNING_KEYS"
            assert key in PRESET_CONTROL_ATTRS, (
                f"预设 {name} 的键 {key} 无 PRESET_CONTROL_ATTRS 映射"
            )

    win = SettingsWindow()
    for key, attr in PRESET_CONTROL_ATTRS.items():
        assert hasattr(win, attr), f"SettingsWindow 缺少控件 {attr}（键 {key}）"
        control = getattr(win, attr)
        assert control is not None
        if attr.endswith("_slider"):
            assert "slider" in control and "step" in control
        else:
            # checkbox
            assert hasattr(control, "isChecked")


def test_apply_preset_game_syncs_config_controls_and_manual_clears():
    """apply_preset('游戏') 写五键+控件；再手动拨滑块 → _active_preset=None。"""
    _app()
    snap = _snap_keys(*_PRESET_KEYS)
    try:
        # 先拨到非游戏值，确保 apply 会真正改动
        config.CHUNK_SUBMIT_SECONDS = 0.5
        config.IDLE_FLUSH_SEC = 2.0
        config.MAX_SENTENCE_PAIRS = 20
        config.SHOW_BILINGUAL = True
        config.DRAFT_TRANSLATION = True

        win = SettingsWindow()
        win.refresh_from_config()
        assert win._active_preset is None

        ok = win.apply_preset("游戏")
        assert ok is True

        expected = config.PRESETS["游戏"]
        for key, val in expected.items():
            assert getattr(config, key) == val, (
                f"{key}: config={getattr(config, key)!r} expected={val!r}"
            )

        # 滑块/复选框同步
        assert win.chunk_submit_slider["slider"].value() == round(
            expected["CHUNK_SUBMIT_SECONDS"] / win.chunk_submit_slider["step"]
        )
        assert win.idle_flush_slider["slider"].value() == round(
            expected["IDLE_FLUSH_SEC"] / win.idle_flush_slider["step"]
        )
        assert win.max_pairs_slider["slider"].value() == round(
            expected["MAX_SENTENCE_PAIRS"] / win.max_pairs_slider["step"]
        )
        assert win.chinese_only_cb.isChecked() is (not expected["SHOW_BILINGUAL"])
        assert win.draft_cb.isChecked() is bool(expected["DRAFT_TRANSLATION"])
        assert win._active_preset == "游戏"
        assert win._preset_buttons["游戏"].isChecked() is True
        assert win._preset_buttons["直播"].isChecked() is False

        # 手动改滑块（不 blockSignals，走真实回调）→ 变自定义
        other = 4.0  # 收尾静音范围 1.0–5.0，与预设 2.5 不同
        win.idle_flush_slider["slider"].setValue(
            round(other / win.idle_flush_slider["step"])
        )
        assert win._active_preset is None
        assert win._preset_buttons["游戏"].isChecked() is False
        assert abs(config.IDLE_FLUSH_SEC - other) < 1e-6
    finally:
        _restore(snap)


def test_apply_preset_blocked_when_game_mode_active():
    """_game_mode_active=True 时 apply_preset 不改 config。"""
    _app()
    snap = _snap_keys(*_PRESET_KEYS)
    try:
        config.CHUNK_SUBMIT_SECONDS = 0.5
        config.IDLE_FLUSH_SEC = 2.0
        config.MAX_SENTENCE_PAIRS = 12
        config.SHOW_BILINGUAL = True
        config.DRAFT_TRANSLATION = True

        win = SettingsWindow()
        win.refresh_from_config()
        before = {k: getattr(config, k) for k in _PRESET_KEYS}

        win.set_game_mode(True)
        assert win._game_mode_active is True

        ok = win.apply_preset("游戏")
        assert ok is False
        for k, v in before.items():
            assert getattr(config, k) == v, f"游戏模式期间 {k} 被改写"
        assert win._active_preset is None

        # 关闭游戏模式后再切应成功
        win.set_game_mode(False)
        assert win.apply_preset("精听") is True
        assert win._active_preset == "精听"
        for key, val in config.PRESETS["精听"].items():
            assert getattr(config, key) == val
    finally:
        _restore(snap)


def test_restore_active_preset_highlight_only():
    """restore 只高亮，不改 config 值。"""
    _app()
    snap = _snap_keys(*_PRESET_KEYS)
    try:
        config.CHUNK_SUBMIT_SECONDS = 0.9
        config.IDLE_FLUSH_SEC = 3.0
        config.MAX_SENTENCE_PAIRS = 8
        config.SHOW_BILINGUAL = False
        config.DRAFT_TRANSLATION = False

        win = SettingsWindow()
        win.refresh_from_config()
        before = {k: getattr(config, k) for k in _PRESET_KEYS}

        win.restore_active_preset("看剧")
        assert win._active_preset == "看剧"
        assert win._preset_buttons["看剧"].isChecked() is True
        for k, v in before.items():
            assert getattr(config, k) == v

        win.restore_active_preset(None)
        assert win._active_preset is None
        assert all(not b.isChecked() for b in win._preset_buttons.values())
    finally:
        _restore(snap)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
