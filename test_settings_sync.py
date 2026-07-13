"""设置面板同步 / 游戏模式滑块 / WordPopup 多屏定位 单测。

运行: venv\\Scripts\\python.exe -m pytest test_settings_sync.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114（见 main.py / test_hittest.py）。
"""
import torch  # noqa: F401  先于 PyQt5
import sys

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QPoint

import config
from subtitle_window import SettingsWindow, WordPopup, _screen_area_at


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def test_refresh_from_config_syncs_sliders_and_labels_without_writeback():
    """改 config → refresh_from_config → 滑块/标签同步，且 blockSignals 不把值写回乱改。"""
    _app()
    snap_chunk = config.CHUNK_SUBMIT_SECONDS
    snap_font = config.FONT_SIZE
    try:
        win = SettingsWindow()
        # 面板已按启动时 config 初始化；再改 config 模拟游戏模式热切
        config.CHUNK_SUBMIT_SECONDS = 1.2
        config.FONT_SIZE = 28
        # 故意先把滑块拨到别的值，确认 refresh 会覆盖
        win.chunk_submit_slider['slider'].blockSignals(True)
        win.chunk_submit_slider['slider'].setValue(
            round(0.3 / win.chunk_submit_slider['step']))
        win.chunk_submit_slider['slider'].blockSignals(False)
        win.font_size_slider['slider'].blockSignals(True)
        win.font_size_slider['slider'].setValue(
            round(14 / win.font_size_slider['step']))
        win.font_size_slider['slider'].blockSignals(False)

        win.refresh_from_config()

        # 滑块内部是 int 刻度 = value/step
        assert win.chunk_submit_slider['slider'].value() == round(
            1.2 / win.chunk_submit_slider['step'])
        assert win.font_size_slider['slider'].value() == round(
            28 / win.font_size_slider['step'])
        # 数值标签手动同步（blockSignals 后 valueChanged 不走）
        assert win.chunk_submit_slider['label'].text() == f"{1.2:.3f}"
        assert win.font_size_slider['label'].text() == f"{28:.3f}"
        # blockSignals 生效：refresh 不应通过回调改写 config
        assert config.CHUNK_SUBMIT_SECONDS == 1.2
        assert config.FONT_SIZE == 28
    finally:
        config.CHUNK_SUBMIT_SECONDS = snap_chunk
        config.FONT_SIZE = snap_font


def test_set_game_mode_toggles_chunk_submit_slider_enabled():
    _app()
    win = SettingsWindow()
    assert win.chunk_submit_slider['slider'].isEnabled()
    assert win.draft_cb.isEnabled()

    win.set_game_mode(True)
    assert not win.chunk_submit_slider['slider'].isEnabled()
    assert not win.draft_cb.isEnabled()
    assert "游戏模式" in (win.chunk_submit_slider['slider'].toolTip() or "")

    win.set_game_mode(False)
    assert win.chunk_submit_slider['slider'].isEnabled()
    assert win.draft_cb.isEnabled()


def test_word_popup_show_at_stays_on_screen_near_global_pos():
    """单屏环境下弹窗落在 global_pos 所在屏 availableGeometry 内且贴近点击点。"""
    app = _app()
    popup = WordPopup()
    screen = QApplication.primaryScreen()
    assert screen is not None
    area = screen.availableGeometry()
    # 屏中偏下一点，给上方弹窗留空间
    global_pos = QPoint(area.center().x(), area.center().y() + area.height() // 6)

    popup.show_at(global_pos, "📖 <b>test</b><br>hello", timeout_ms=500)
    app.processEvents()

    geo = popup.frameGeometry()
    assert area.contains(geo.topLeft()) or area.intersects(geo)
    # 整窗应在该屏 availableGeometry 内
    assert geo.left() >= area.left()
    assert geo.top() >= area.top()
    assert geo.right() <= area.right() + 1
    assert geo.bottom() <= area.bottom() + 1
    # 贴近点击位置（曼哈顿距离中心 < 300）
    dx = abs(geo.center().x() - global_pos.x())
    dy = abs(geo.center().y() - global_pos.y())
    assert max(dx, dy) < 300 or (dx * dx + dy * dy) ** 0.5 < 300

    # screenAt 路径与 helper 一致
    at_area = _screen_area_at(global_pos)
    assert at_area is not None
    assert at_area == area

    popup.hide()


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
