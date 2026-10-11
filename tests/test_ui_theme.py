"""字幕悬浮窗主题单测：译文主色、旧 state 迁移、按钮符号、句对层次。"""
import json
import os
import sys
import tempfile

import torch  # noqa: F401  先于 PyQt6；见 CLAUDE.md 第 4 节第 1 条
from PyQt6.QtWidgets import QApplication

import realtime_subtitle.config as config
import realtime_subtitle.ui.subtitle_window as subtitle_window
from realtime_subtitle.ui.subtitle_window import SubtitleWindow

sys.stdout.reconfigure(encoding="utf-8")


_APP = None
_WIN = None
_TMPDIR = None


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _window():
    global _WIN, _TMPDIR, _APP
    if _WIN is not None:
        return _WIN
    _app()
    _TMPDIR = tempfile.mkdtemp(prefix="rs_ui_theme_")
    subtitle_window.STATE_FILE = os.path.join(_TMPDIR, "window_state.json")
    _WIN = SubtitleWindow()
    _APP = _WIN.app
    _WIN._state_timer.stop()
    return _WIN


def _write_state(tmp_path, monkeypatch, tuning):
    path = tmp_path / "window_state.json"
    path.write_text(json.dumps({"tuning": tuning}), encoding="utf-8")
    monkeypatch.setattr(subtitle_window, "STATE_FILE", str(path))


def _snap_keys(*keys):
    return {k: getattr(config, k) for k in keys}


def _restore(snap):
    for key, value in snap.items():
        setattr(config, key, value)


def test_translation_and_source_default_colors():
    assert config.CHINESE_TEXT_COLOR.lower() == "#ffe894"
    assert config.TEXT_COLOR == "#F0F0F0"


def test_old_default_translation_color_migrates(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, {"CHINESE_TEXT_COLOR": "#c8c8c8"})

    loaded = SubtitleWindow._load_state()

    assert loaded["tuning"]["CHINESE_TEXT_COLOR"] == "#FFE894"


def test_custom_translation_color_is_not_migrated(tmp_path, monkeypatch):
    _write_state(tmp_path, monkeypatch, {"CHINESE_TEXT_COLOR": "#aabbcc"})

    loaded = SubtitleWindow._load_state()

    assert loaded["tuning"]["CHINESE_TEXT_COLOR"] == "#aabbcc"


def test_toolbar_button_text_contains_no_emoji_or_symbol_pictographs():
    win = _window()
    buttons = (
        win.minimize_btn,
        win.history_btn,
        win.cinema_btn,
        win.tv_btn,
        win.ai_btn,
        win.settings_btn,
        win.quit_btn,
    )
    for button in buttons:
        assert button.toolTip().strip(), "工具条按钮必须保留中文 tooltip"
        for char in button.text():
            codepoint = ord(char)
            assert codepoint < 0x1F000
            assert not 0x2600 <= codepoint <= 0x27BF


def test_old_sentence_pairs_are_dimmed_but_latest_pair_is_not():
    win = _window()
    snap = _snap_keys(
        "FONT_SIZE",
        "SHOW_BILINGUAL",
        "CHINESE_TEXT_COLOR",
        "TEXT_COLOR",
        "MAX_SENTENCE_PAIRS",
    )
    try:
        config.FONT_SIZE = 20
        config.SHOW_BILINGUAL = True
        config.CHINESE_TEXT_COLOR = "#FFE894"
        config.TEXT_COLOR = "#F0F0F0"
        config.MAX_SENTENCE_PAIRS = 3
        win.window.resize(900, 500)
        win.sentence_pairs.clear()
        win.live_committed = ""
        win.live_unstable = ""
        win.live_draft = ""
        win.status_line = ""

        win._add_pair("Alt eins", "旧译文一")
        win._add_pair("Alt zwei", "旧译文二")
        win._add_pair("Neu", "最新译文")
        html = win._last_html

        assert "opacity:0.60" in html
        assert "margin-top:0.45em" in html
        assert "font-size:17px" in html
        assert "font-size:20px" in html
        assert "#FFE894" in html

        latest_pos = html.rfind("最新译文")
        dim_pos = html.rfind("opacity:0.60")
        assert latest_pos > dim_pos
    finally:
        _restore(snap)
        win.sentence_pairs.clear()
        win._update_live("", "")
