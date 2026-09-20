"""设置面板可读性：字号、滚动、数值格式、小屏几何。

对应 Grok 接手包批次 B。不 import main.py（单实例 Mutex 会 sys.exit）。
torch 仍然先于 PyQt6（CLAUDE.md 第 4 节第 1 条），QApplication 必须持有模块级引用。
"""
import torch  # noqa: F401  先于 PyQt6
import sys

sys.stdout.reconfigure(encoding="utf-8")

from PyQt6.QtWidgets import QApplication, QScrollArea, QLabel
from PyQt6.QtCore import Qt

import realtime_subtitle.config as config
from realtime_subtitle.ui.settings_window import SettingsWindow


_APP = None


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def test_format_slider_value_follows_step():
    """整数不带三位小数；小数位数跟步长走。"""
    from realtime_subtitle.ui.settings_window import format_slider_value

    assert format_slider_value(24.0, 1) == "24"
    assert format_slider_value(12.0, 1.0) == "12"
    assert format_slider_value(1.2, 0.1) == "1.2"
    assert format_slider_value(2.5, 0.5) == "2.5"
    assert format_slider_value(0.015, 0.001) == "0.015"
    assert format_slider_value(255, 5) == "255"


def test_panel_font_px_starts_at_16_to_18_and_scales_with_dpi():
    """100% 缩放下 16–18px；高 DPI 放大，不二次叠全局 Qt 缩放。"""
    from realtime_subtitle.ui.settings_window import PANEL_FONT_PX_AT_100, panel_font_px

    assert 16 <= PANEL_FONT_PX_AT_100 <= 18
    assert panel_font_px(1.0) == PANEL_FONT_PX_AT_100
    assert panel_font_px(1.5) >= int(PANEL_FONT_PX_AT_100 * 1.5) - 1
    assert panel_font_px(2.0) >= int(PANEL_FONT_PX_AT_100 * 2.0) - 1
    assert panel_font_px(2.0) <= 40


def test_settings_initial_geometry_fits_small_and_scaled_screens():
    """窗口初始大小受可用区约束；1366×768 及 100/150/200% 都不越界。"""
    from realtime_subtitle.ui.window_geometry import settings_initial_geometry

    screens = (
        (0, 0, 1366, 728),
        (0, 0, 1920, 1040),
        (0, 0, 2560, 1392),
    )
    for ax, ay, aw, ah in screens:
        for scale in (1.0, 1.5, 2.0):
            x, y, w, h = settings_initial_geometry(ax, ay, aw, ah, scale)
            assert x >= ax, f"scale={scale} left"
            assert y >= ay, f"scale={scale} top"
            assert x + w <= ax + aw, f"scale={scale} right {x}+{w} vs {ax}+{aw}"
            assert y + h <= ay + ah, f"scale={scale} bottom {y}+{h} vs {ay}+{ah}"
            assert w >= 360 and h >= 360


def test_settings_window_uses_scroll_area_and_fits_current_screen():
    """原生滚动容器；窗口不超出当前屏可用区。"""
    _app()
    win = SettingsWindow()
    try:
        scroll = win.findChild(QScrollArea)
        assert scroll is not None, "设置面板必须用 QScrollArea，小屏才能滚到底"
        assert scroll.widgetResizable() is True
        area = QApplication.primaryScreen().availableGeometry()
        geo = win.frameGeometry()
        assert geo.width() <= area.width()
        assert geo.height() <= area.height()
        # 内容可以比窗口高（靠滚动），但窗口自己不能写死 1060 不管屏幕
        assert win.height() <= area.height()
    finally:
        win.close()


def test_settings_slider_labels_use_step_format_not_three_decimals():
    """字幕字号/句对显示整数；提交节奏保留必要小数。"""
    _app()
    snap = {"FONT_SIZE": config.FONT_SIZE, "CHUNK_SUBMIT_SECONDS": config.CHUNK_SUBMIT_SECONDS,
            "MAX_SENTENCE_PAIRS": config.MAX_SENTENCE_PAIRS}
    try:
        config.FONT_SIZE = 24
        config.CHUNK_SUBMIT_SECONDS = 1.2
        config.MAX_SENTENCE_PAIRS = 8
        win = SettingsWindow()
        try:
            assert win.font_size_slider["label"].text() == "24"
            assert win.max_pairs_slider["label"].text() == "8"
            assert win.chunk_submit_slider["label"].text() == "1.2"
            win.refresh_from_config()
            assert win.font_size_slider["label"].text() == "24"
            assert win.chunk_submit_slider["label"].text() == "1.2"
        finally:
            win.close()
    finally:
        for k, v in snap.items():
            setattr(config, k, v)


def test_subtitle_font_slider_label_is_explicit():
    """「字幕字号」控制字幕正文，不是面板自己的字体。"""
    _app()
    win = SettingsWindow()
    try:
        labels = [c.text() for c in win.findChildren(QLabel) if c.text()]
        assert any("字幕字号" in t for t in labels), labels
        assert not any(t.startswith("字体大小") for t in labels), labels
    finally:
        win.close()


def test_slider_titles_are_not_clipped_by_fixed_120px():
    """标题宽度按字体测量，放大后不能被 120px 截断。"""
    _app()
    win = SettingsWindow()
    try:
        for info in (
            win.chunk_submit_slider,
            win.buffer_trim_slider,
            win.font_size_slider,
            win.cinema_font_slider,
        ):
            title = info["title"]
            fm = title.fontMetrics()
            needed = fm.horizontalAdvance(title.text())
            assert title.minimumWidth() >= needed, (
                f"{title.text()!r} minWidth={title.minimumWidth()} needed={needed}"
            )
            assert title.width() >= needed - 1 or title.sizeHint().width() >= needed
    finally:
        win.close()


def test_panel_controls_use_readable_font_and_click_area():
    """模式按钮、复选框、输入框和面板字体同步放大。"""
    from realtime_subtitle.ui.settings_window import panel_font_px

    _app()
    win = SettingsWindow()
    try:
        expect = panel_font_px()
        assert win.chinese_only_cb.font().pixelSize() >= expect - 1
        assert win.draft_cb.font().pixelSize() >= expect - 1
        live_btn = win._preset_buttons["直播"]
        assert live_btn.font().pixelSize() >= expect - 1
        assert live_btn.sizeHint().height() >= expect + 8
        assert win.chinese_only_cb.sizeHint().height() >= expect
        assert win.device_name_edit.font().pixelSize() >= expect - 1
        # 字幕字号滑块改的是 config.FONT_SIZE，不是面板字体
        snap = config.FONT_SIZE
        try:
            win.font_size_slider["slider"].setValue(
                round(40 / win.font_size_slider["step"]))
            assert config.FONT_SIZE == 40
            assert win.chinese_only_cb.font().pixelSize() >= expect - 1
        finally:
            config.FONT_SIZE = snap
    finally:
        win.close()


def test_settings_checkboxes_keep_keyboard_focus():
    """键盘可以落到复选框上（滚动容器不能吞掉 Tab）。"""
    _app()
    win = SettingsWindow()
    try:
        assert win.chinese_only_cb.focusPolicy() != Qt.FocusPolicy.NoFocus
        assert win.draft_cb.focusPolicy() != Qt.FocusPolicy.NoFocus
        assert win.cinema_german_cb.focusPolicy() != Qt.FocusPolicy.NoFocus
        win.show()
        QApplication.instance().processEvents()
        win.chinese_only_cb.setFocus(Qt.FocusReason.TabFocusReason)
        QApplication.instance().processEvents()
        assert win.chinese_only_cb.hasFocus()
    finally:
        win.close()


def test_language_keys_live_in_tuning_not_presets():
    """识别语言和场景模式正交：持久化走 tuning，四个模式都不能改它。"""
    from realtime_subtitle.ui.settings_window import TUNING_KEYS

    assert "AUTO_DETECT_LANGUAGE" in TUNING_KEYS
    assert "SOURCE_LANGUAGE" in TUNING_KEYS
    for name, params in config.PRESETS.items():
        assert "AUTO_DETECT_LANGUAGE" not in params, name
        assert "SOURCE_LANGUAGE" not in params, name
        assert "TARGET_LANGUAGE" not in params, name


def test_apply_tuning_language_sets_matching_target_and_skips_unknown():
    from realtime_subtitle.ui.settings_window import apply_tuning

    snap = {
        "SOURCE_LANGUAGE": config.SOURCE_LANGUAGE,
        "TARGET_LANGUAGE": config.TARGET_LANGUAGE,
        "AUTO_DETECT_LANGUAGE": config.AUTO_DETECT_LANGUAGE,
    }
    try:
        apply_tuning({"SOURCE_LANGUAGE": "zh", "AUTO_DETECT_LANGUAGE": True})
        assert config.SOURCE_LANGUAGE == "zh"
        assert config.TARGET_LANGUAGE == "de"
        assert config.AUTO_DETECT_LANGUAGE is True
        apply_tuning({"SOURCE_LANGUAGE": "not-a-lang"})
        assert config.SOURCE_LANGUAGE == "zh"
    finally:
        for k, v in snap.items():
            setattr(config, k, v)


def test_collect_tuning_roundtrips_language():
    from realtime_subtitle.ui.settings_window import apply_tuning, collect_tuning

    snap = {k: getattr(config, k) for k in (
        "SOURCE_LANGUAGE", "TARGET_LANGUAGE", "AUTO_DETECT_LANGUAGE")}
    try:
        config.SOURCE_LANGUAGE = "en"
        config.TARGET_LANGUAGE = "zh"
        config.AUTO_DETECT_LANGUAGE = True
        saved = collect_tuning()
        config.SOURCE_LANGUAGE = "de"
        config.TARGET_LANGUAGE = "zh"
        config.AUTO_DETECT_LANGUAGE = False
        apply_tuning(saved)
        assert config.SOURCE_LANGUAGE == "en"
        assert config.TARGET_LANGUAGE == "zh"
        assert config.AUTO_DETECT_LANGUAGE is True
    finally:
        for k, v in snap.items():
            setattr(config, k, v)


def test_settings_language_group_is_separate_from_scene_modes():
    from PyQt6.QtWidgets import QGroupBox

    _app()
    win = SettingsWindow()
    try:
        titles = [g.title() for g in win.findChildren(QGroupBox)]
        assert any("识别语言" in t for t in titles), titles
        assert any(t == "模式" for t in titles), titles
        assert win.auto_detect_cb is not None
        assert "自动" in win.auto_detect_cb.text()
        assert "德语" in win.current_pair_label.text() or "de" in win.current_pair_label.text()
        assert ("zh", "de") in win._lang_pair_buttons or ("de", "zh") in win._lang_pair_buttons
        assert set(win._preset_buttons) == {"直播", "看剧", "性能", "精听"}
    finally:
        win.close()


def test_auto_detect_toggle_does_not_mark_scene_custom():
    _app()
    snap = {"AUTO_DETECT_LANGUAGE": config.AUTO_DETECT_LANGUAGE}
    try:
        config.AUTO_DETECT_LANGUAGE = False
        win = SettingsWindow()
        try:
            win.restore_active_preset("直播")
            win.auto_detect_cb.setChecked(True)
            assert config.AUTO_DETECT_LANGUAGE is True
            assert win._active_preset == "直播"
            assert win._preset_buttons["直播"].isChecked() is True
        finally:
            win.close()
    finally:
        config.AUTO_DETECT_LANGUAGE = snap["AUTO_DETECT_LANGUAGE"]


def test_pair_button_delegates_to_callback_without_writing_config():
    _app()
    snap = {"SOURCE_LANGUAGE": config.SOURCE_LANGUAGE,
            "TARGET_LANGUAGE": config.TARGET_LANGUAGE}
    try:
        config.SOURCE_LANGUAGE = "de"
        config.TARGET_LANGUAGE = "zh"
        win = SettingsWindow()
        try:
            got = []
            win.on_language_change = lambda s, t=None: got.append((s, t))
            win._lang_pair_buttons[("zh", "de")].click()
            assert got == [("zh", "de")], "整对传下去，不能只传源语言"
            assert config.SOURCE_LANGUAGE == "de"
            assert config.TARGET_LANGUAGE == "zh"
        finally:
            win.close()
    finally:
        for k, v in snap.items():
            setattr(config, k, v)


def test_pair_button_without_callback_sets_source_and_target():
    _app()
    snap = {"SOURCE_LANGUAGE": config.SOURCE_LANGUAGE,
            "TARGET_LANGUAGE": config.TARGET_LANGUAGE}
    try:
        config.SOURCE_LANGUAGE = "de"
        config.TARGET_LANGUAGE = "zh"
        win = SettingsWindow()
        try:
            win._lang_pair_buttons[("zh", "de")].click()
            assert config.SOURCE_LANGUAGE == "zh"
            assert config.TARGET_LANGUAGE == "de"
            assert "中文" in win.current_pair_label.text()
        finally:
            win.close()
    finally:
        for k, v in snap.items():
            setattr(config, k, v)


def test_refresh_from_config_syncs_language_controls():
    _app()
    snap = {"SOURCE_LANGUAGE": config.SOURCE_LANGUAGE,
            "TARGET_LANGUAGE": config.TARGET_LANGUAGE,
            "AUTO_DETECT_LANGUAGE": config.AUTO_DETECT_LANGUAGE}
    try:
        win = SettingsWindow()
        try:
            config.SOURCE_LANGUAGE = "en"
            config.TARGET_LANGUAGE = "zh"
            config.AUTO_DETECT_LANGUAGE = True
            win.refresh_from_config()
            assert win.auto_detect_cb.isChecked() is True
            assert win._lang_pair_buttons[("en", "zh")].isChecked() is True
            assert win._lang_pair_buttons[("de", "zh")].isChecked() is False
            assert "英语" in win.current_pair_label.text()
        finally:
            win.close()
    finally:
        for k, v in snap.items():
            setattr(config, k, v)


def test_reset_defaults_restores_language_snapshot():
    from realtime_subtitle.ui.settings_window import snapshot_defaults

    _app()
    keys = ["SOURCE_LANGUAGE", "TARGET_LANGUAGE", "AUTO_DETECT_LANGUAGE"]
    snap = {k: getattr(config, k) for k in keys}
    try:
        factory = snapshot_defaults()
        config.SOURCE_LANGUAGE = "zh"
        config.TARGET_LANGUAGE = "de"
        config.AUTO_DETECT_LANGUAGE = True
        win = SettingsWindow(defaults=factory)
        try:
            win._reset_defaults()
            assert config.SOURCE_LANGUAGE == factory["SOURCE_LANGUAGE"]
            assert config.AUTO_DETECT_LANGUAGE == factory["AUTO_DETECT_LANGUAGE"]
            assert win.auto_detect_cb.isChecked() == bool(factory["AUTO_DETECT_LANGUAGE"])
        finally:
            win.close()
    finally:
        for k, v in snap.items():
            setattr(config, k, v)
