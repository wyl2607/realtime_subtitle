"""设置面板可读性：字号、滚动、数值格式、小屏几何。

对应 Grok 接手包批次 B。不 import main.py（单实例 Mutex 会 sys.exit）。
torch 必须先于 PyQt5，QApplication 必须持有模块级引用。
"""
import torch  # noqa: F401  先于 PyQt5
import sys

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication, QScrollArea, QLabel
from PyQt5.QtCore import Qt

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
        assert win.chinese_only_cb.focusPolicy() != Qt.NoFocus
        assert win.draft_cb.focusPolicy() != Qt.NoFocus
        assert win.cinema_german_cb.focusPolicy() != Qt.NoFocus
        win.show()
        QApplication.instance().processEvents()
        win.chinese_only_cb.setFocus(Qt.TabFocusReason)
        QApplication.instance().processEvents()
        assert win.chinese_only_cb.hasFocus()
    finally:
        win.close()
