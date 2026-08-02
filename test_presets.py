"""模式系统的面板侧单测（直播/看剧/性能/精听）。

2026-08-02 合并模式系统后，面板不再自己动控件（apply_preset/_apply_preset_key
那套 widget 旁路已删）：按钮只把请求转给 main 的 _apply_mode，控件显示统一由
refresh_from_config 读回。所以这里测的是"转发 + 读回 + 自定义态"三件事，
config 写入的正确性在 test_game_mode.py（_apply_mode）里测。

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
from subtitle_window import SettingsWindow, TUNING_KEYS, MODE_ICONS


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
    "WHISPER_BEAM_SIZE",
    "TRANSLATION_STYLE",
)


def test_every_mode_is_complete_and_has_icon():
    """四个模式的键集合必须一致（防加模式漏键）；每个模式都要有图标。"""
    presets = getattr(config, "PRESETS", None)
    assert isinstance(presets, dict) and presets, "config.PRESETS 缺失"
    key_sets = [frozenset(p) for p in presets.values()]
    assert len(set(key_sets)) == 1, f"各模式的键集合不一致: {key_sets}"
    for name, params in presets.items():
        assert name in MODE_ICONS, f"模式 {name} 没有图标"
        for key in params:
            assert hasattr(config, key), f"模式 {name} 的键 {key} 在 config 里不存在"
    # 语域必须都有对应的 prompt 模板，否则翻译会回落到随便一个
    for name, params in presets.items():
        style = params.get("TRANSLATION_STYLE")
        assert style in config.TRANSLATION_STYLE_PROMPTS, f"{name} 的语域 {style} 无模板"


def test_translation_style_is_persisted():
    """语域跟着模式走，必须一起持久化（否则重启后模式高亮和语域对不上）。"""
    assert "TRANSLATION_STYLE" in TUNING_KEYS


def test_mode_button_delegates_to_callback_only():
    """按钮只转发模式名给 on_mode_change，自己不写 config、不动控件。"""
    _app()
    snap = _snap_keys(*_PRESET_KEYS)
    try:
        config.CHUNK_SUBMIT_SECONDS = 0.5
        config.IDLE_FLUSH_SEC = 2.0
        win = SettingsWindow()
        got = []
        win.on_mode_change = got.append

        win._on_preset_clicked("性能")
        assert got == ["性能"]
        # 面板自己没改任何 config（这是 _apply_mode 的活）
        assert config.CHUNK_SUBMIT_SECONDS == 0.5
        assert config.IDLE_FLUSH_SEC == 2.0
    finally:
        _restore(snap)


def test_manual_slider_change_reports_custom():
    """手动拨滑块 → 清高亮 + 通知 main 变自定义（指示器不能撒谎）。"""
    _app()
    snap = _snap_keys(*_PRESET_KEYS)
    try:
        config.IDLE_FLUSH_SEC = 2.0
        win = SettingsWindow()
        win.refresh_from_config()
        got = []
        win.on_mode_change = got.append
        win.restore_active_preset("看剧")
        assert win._active_preset == "看剧"

        other = 4.0  # 收尾静音范围 1.0–5.0
        win.idle_flush_slider["slider"].setValue(
            round(other / win.idle_flush_slider["step"]))

        assert win._active_preset is None
        assert got == [None], "必须通知 main 变自定义"
        assert win._preset_buttons["看剧"].isChecked() is False
        assert abs(config.IDLE_FLUSH_SEC - other) < 1e-6
    finally:
        _restore(snap)


def test_refresh_from_config_syncs_controls_after_mode_applied():
    """_apply_mode 直接写 config 后，面板靠 refresh_from_config 读回显示。"""
    _app()
    snap = _snap_keys(*_PRESET_KEYS)
    try:
        win = SettingsWindow()
        expected = config.PRESETS["性能"]
        for key, val in expected.items():
            setattr(config, key, val)   # 模拟 _apply_mode 的纯 setattr
        win.refresh_from_config()

        assert win.chunk_submit_slider["slider"].value() == round(
            expected["CHUNK_SUBMIT_SECONDS"] / win.chunk_submit_slider["step"])
        assert win.max_pairs_slider["slider"].value() == round(
            expected["MAX_SENTENCE_PAIRS"] / win.max_pairs_slider["step"])
        assert win.chinese_only_cb.isChecked() is (not expected["SHOW_BILINGUAL"])
        assert win.draft_cb.isChecked() is bool(expected["DRAFT_TRANSLATION"])
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
