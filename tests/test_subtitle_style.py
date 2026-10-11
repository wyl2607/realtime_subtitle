"""字幕样式设置：控件、渲染、样式表、tuning 和重启恢复。"""
import json
import sys

import torch  # noqa: F401  先于 PyQt6；见 CLAUDE.md 第 4 节第 1 条
from PyQt6.QtWidgets import QApplication, QGraphicsDropShadowEffect

import realtime_subtitle.config as config
import realtime_subtitle.ui.subtitle_window as subtitle_window
from realtime_subtitle.ui.settings_window import (
    TUNING_KEYS,
    apply_tuning,
    collect_tuning,
    sync_display_mode_from_show_bilingual,
)
from realtime_subtitle.ui.subtitle_window import SubtitleWindow

sys.stdout.reconfigure(encoding="utf-8")


_APP = None


STYLE_KEYS = (
    "FONT_SIZE",
    "SOURCE_TEXT_COLOR",
    "TEXT_COLOR",
    "CHINESE_TEXT_COLOR",
    "BACKGROUND_COLOR",
    "BACKGROUND_OPACITY",
    "TEXT_EDGE",
    "WINDOW_CORNER_RADIUS",
    "BORDER_RADIUS",
    "DISPLAY_MODE",
    "SHOW_BILINGUAL",
)


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _snap():
    return {key: getattr(config, key) for key in STYLE_KEYS}


def _restore(snap):
    for key, value in snap.items():
        setattr(config, key, value)


def _close_window(win):
    win._state_timer.stop()
    for attr in (
        "settings_window",
        "history_window",
        "tv_window",
        "cinema_bar",
        "word_popup",
        "ai_analysis_popup",
        "container",
    ):
        widget = getattr(win, attr, None)
        if widget is not None:
            widget.close()
    _app().processEvents()


def _new_window(tmp_path, monkeypatch):
    _app()
    state_path = tmp_path / "window_state.json"
    monkeypatch.setattr(subtitle_window, "STATE_FILE", str(state_path))
    win = SubtitleWindow()
    win._state_timer.stop()
    return win, state_path


def test_style_controls_render_and_persist_via_tuning(tmp_path, monkeypatch):
    snap = _snap()
    win = None
    restored = None
    try:
        win, state_path = _new_window(tmp_path, monkeypatch)
        panel = win.settings_window

        panel.font_size_slider["slider"].setValue(34)
        panel.source_color_palette["buttons"]["#b8f2b0"].click()
        panel.target_color_palette["buttons"]["#9fe7f5"].click()
        panel.background_color_palette["buttons"]["#0f1b2d"].click()
        panel.bg_opacity_slider["slider"].setValue(round(210 / panel.bg_opacity_slider["step"]))
        panel.text_edge_row["buttons"]["shadow"].click()
        panel.corner_radius_row["buttons"][16].click()
        panel.display_mode_row["buttons"]["target"].click()

        assert config.FONT_SIZE == 34
        assert config.SOURCE_TEXT_COLOR == "#b8f2b0"
        assert config.TEXT_COLOR == "#B8F2B0"
        assert config.CHINESE_TEXT_COLOR == "#9fe7f5"
        assert config.BACKGROUND_COLOR == "#0f1b2d"
        assert config.BACKGROUND_OPACITY == 210
        assert config.TEXT_EDGE == "shadow"
        assert config.WINDOW_CORNER_RADIUS == 16
        assert config.BORDER_RADIUS == 16
        assert config.DISPLAY_MODE == "target"
        assert config.SHOW_BILINGUAL is False

        win.sentence_pairs.clear()
        win._add_pair("Quelle", "译文")
        html = win._last_html
        assert "译文" in html
        assert "Quelle" not in html
        assert "#9fe7f5" in html

        style = win.window.styleSheet()
        assert "rgba(15, 27, 45, 210)" in style
        assert "border-radius: 16px" in style
        assert isinstance(win.window.graphicsEffect(), QGraphicsDropShadowEffect)

        tuning = collect_tuning()
        for key in (
            "SOURCE_TEXT_COLOR",
            "CHINESE_TEXT_COLOR",
            "BACKGROUND_COLOR",
            "TEXT_EDGE",
            "WINDOW_CORNER_RADIUS",
            "DISPLAY_MODE",
        ):
            assert key in TUNING_KEYS
            assert tuning[key] == getattr(config, key)

        win._save_state_if_changed()
        assert state_path.exists()
        saved = json.loads(state_path.read_text(encoding="utf-8"))
        assert saved["tuning"]["DISPLAY_MODE"] == "target"
        assert saved["tuning"]["WINDOW_CORNER_RADIUS"] == 16

        _close_window(win)
        win = None
        config.FONT_SIZE = 22
        config.SOURCE_TEXT_COLOR = "#F0F0F0"
        config.TEXT_COLOR = "#F0F0F0"
        config.CHINESE_TEXT_COLOR = "#ffe894"
        config.BACKGROUND_COLOR = "#000000"
        config.BACKGROUND_OPACITY = 235
        config.TEXT_EDGE = "none"
        config.WINDOW_CORNER_RADIUS = 8
        config.BORDER_RADIUS = 8
        config.DISPLAY_MODE = "bilingual"
        config.SHOW_BILINGUAL = True

        restored = SubtitleWindow()
        restored._state_timer.stop()
        assert config.FONT_SIZE == 34
        assert config.SOURCE_TEXT_COLOR == "#b8f2b0"
        assert config.CHINESE_TEXT_COLOR == "#9fe7f5"
        assert config.BACKGROUND_COLOR == "#0f1b2d"
        assert config.TEXT_EDGE == "shadow"
        assert config.WINDOW_CORNER_RADIUS == 16
        assert config.DISPLAY_MODE == "target"
        assert restored.settings_window.display_mode_row["buttons"]["target"].isChecked()
        assert restored.settings_window.font_size_slider["slider"].value() == 34
    finally:
        if win is not None:
            _close_window(win)
        if restored is not None:
            _close_window(restored)
        _restore(snap)


def test_source_only_mode_shows_source_and_hides_translation(tmp_path, monkeypatch):
    snap = _snap()
    win = None
    try:
        win, _state_path = _new_window(tmp_path, monkeypatch)
        win.settings_window.display_mode_row["buttons"]["source"].click()
        win.sentence_pairs.clear()
        win._add_pair("Nur Original", "只应隐藏的译文")

        assert config.DISPLAY_MODE == "source"
        assert config.SHOW_BILINGUAL is False
        assert "Nur Original" in win._last_html
        assert "只应隐藏的译文" not in win._last_html
    finally:
        if win is not None:
            _close_window(win)
        _restore(snap)


def test_show_bilingual_and_display_mode_stay_consistent():
    snap = _snap()
    try:
        apply_tuning({"SHOW_BILINGUAL": False})
        assert config.DISPLAY_MODE == "target"
        assert config.SHOW_BILINGUAL is False

        apply_tuning({"DISPLAY_MODE": "source"})
        assert config.DISPLAY_MODE == "source"
        assert config.SHOW_BILINGUAL is False

        config.SHOW_BILINGUAL = True
        sync_display_mode_from_show_bilingual()
        assert config.DISPLAY_MODE == "bilingual"
        assert config.SHOW_BILINGUAL is True
    finally:
        _restore(snap)
