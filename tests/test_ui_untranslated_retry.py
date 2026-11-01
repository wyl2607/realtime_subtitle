"""UI 失败句渲染「未翻译 ⟳」与重试、设计令牌测试。

验证 spec 003 P2b + spec 006 D1：
1. 令牌默认值等于 config 默认值
2. 失败句标记渲染与颜色
3. 只显译文模式不吞失败句（原文+标记均上屏）
4. 点击「未翻译 ⟳」调用 retry_translation，成功变 design token
5. 过期 key 提示「这句已过期，无法重试」
6. revision 旧代数在消费端被拒
7. reason == "dropped" 同样渲染标记并可重试
8. update_dst 多窗同步（主窗、历史窗、电视窗、影院条）
"""
import os
import sys
import tempfile
import torch  # noqa: F401  必须先于 PyQt6 导入
from PyQt6.QtWidgets import QApplication

import realtime_subtitle.config as config
import realtime_subtitle.ui.design_tokens as design_tokens
import realtime_subtitle.ui.subtitle_window as subtitle_window
from realtime_subtitle.ui.subtitle_window import SubtitleWindow

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
    _TMPDIR = tempfile.mkdtemp(prefix="rs_ui_retry_")
    subtitle_window.STATE_FILE = os.path.join(_TMPDIR, "window_state.json")
    _WIN = SubtitleWindow()
    _APP = _WIN.app
    _WIN._state_timer.stop()
    return _WIN


def _pump_until(pred, timeout_ms=3000):
    from PyQt6.QtCore import QThread
    app = _app()
    waited = 0
    while waited < timeout_ms:
        app.processEvents()
        if pred():
            return True
        QThread.msleep(10)
        waited += 10
    return pred()


def test_token_defaults_match_config():
    """令牌默认值等于 config 默认值，确保 design_tokens 与 config 单一真相源。"""
    assert config.SOURCE_TEXT_COLOR.lower() == design_tokens.COLOR_SUBTITLE_SOURCE.lower()
    assert config.CHINESE_TEXT_COLOR.lower() == design_tokens.COLOR_SUBTITLE_TRANSLATION.lower()
    assert config.DRAFT_TEXT_COLOR.lower() == design_tokens.COLOR_SUBTITLE_DRAFT.lower()
    assert config.BACKGROUND_COLOR.lower() == design_tokens.COLOR_BACKGROUND.lower()
    assert config.BACKGROUND_OPACITY == round(design_tokens.OPACITY_BACKGROUND_DEFAULT * 255)
    assert config.FONT_SIZE == design_tokens.TYPE_SUBTITLE_DEFAULT
    assert config.FONT_SIZE_MIN == design_tokens.TYPE_SUBTITLE_MIN
    assert config.FONT_SIZE_MAX == design_tokens.TYPE_SUBTITLE_MAX
    assert config.WINDOW_CORNER_RADIUS == design_tokens.RADIUS_SMALL
    assert config.PADDING == f"{design_tokens.SPACE_WINDOW_PAD_V}px {design_tokens.SPACE_WINDOW_PAD_H}px"


def test_failed_sentence_renders_untranslated_marker():
    """失败句渲染 TEXT_UNTRANSLATED 标记与 COLOR_UNTRANSLATED 颜色。"""
    win = _window()
    win.history.clear()

    win.on_translate_failed("Das ist ein Test", 0, 1000, "k_fail_1", 0, "timeout")
    assert _pump_until(lambda: win.history.get("k_fail_1") is not None)

    html = win._last_html
    assert design_tokens.TEXT_UNTRANSLATED in html
    assert design_tokens.COLOR_UNTRANSLATED in html
    assert 'href="retry:k_fail_1"' in html
    assert "Das ist ein Test" in html


def test_target_only_mode_does_not_swallow_failed_sentence():
    """「只显译文」模式下失败句改显示原文 + 该标记，绝不整句隐形。"""
    win = _window()
    win.history.clear()
    orig_mode = config.DISPLAY_MODE
    orig_bilingual = config.SHOW_BILINGUAL
    try:
        config.DISPLAY_MODE = "target"
        config.SHOW_BILINGUAL = False

        win.on_translate_failed("Untersuchung läuft", 0, 1000, "k_target_fail", 0, "timeout")
        assert _pump_until(lambda: win.history.get("k_target_fail") is not None)

        html = win._last_html
        assert "Untersuchung läuft" in html, "只显译文模式下失败句不能吞掉德语原文"
        assert design_tokens.TEXT_UNTRANSLATED in html

        # 电视窗也不吞句
        win.tv_window.show()
        win.tv_window.rebuild_from_store()
        assert "Untersuchung läuft" in win.tv_window.text.toPlainText()

        # 影院条显式展示时不吞句（德语行与未翻译标记行均可见）
        win.cinema_bar.show()
        win.cinema_bar.show_last()
        assert win.cinema_bar.german_label.text() == "Untersuchung läuft"
        assert win.cinema_bar.chinese_label.text() == design_tokens.TEXT_UNTRANSLATED
        win.cinema_bar.hide()
    finally:
        config.DISPLAY_MODE = orig_mode
        config.SHOW_BILINGUAL = orig_bilingual


def test_retry_translation_success_flow():
    """点击「未翻译 ⟳」调用 retry_translation，返回 True 时变重试令牌，成功后原地更新。"""
    win = _window()
    win.history.clear()

    retried_keys = []
    win.on_retry_translation = lambda k: (retried_keys.append(k), True)[1]

    win.on_translate_failed("Satz zum Wiederholen", 0, 1000, "k_retry_ok", 0, "timeout")
    assert _pump_until(lambda: win.history.get("k_retry_ok") is not None)

    # 触发重试点击
    win._handle_retry_click("k_retry_ok")
    assert retried_keys == ["k_retry_ok"]
    assert design_tokens.TEXT_TRANSLATING in win._last_html

    # 重试成功：带同一 key 重新到达
    win.add_pair("Satz zum Wiederholen", "重试成功译文", 0, 1000, "k_retry_ok", 0)
    assert _pump_until(lambda: "重试成功译文" in win._last_html)
    assert len(win.history) == 1, "同一 key 原地更新，不得新增一行"
    assert "重试成功译文" in win._last_html


def test_retry_translation_expired_hint():
    """retry_translation 返回 False 时，短暂提示「这句已过期，无法重试」。"""
    win = _window()
    win.history.clear()

    win.on_retry_translation = lambda k: False

    win.on_translate_failed("Alte abgelaufene Zeile", 0, 1000, "k_expired", 0, "timeout")
    assert _pump_until(lambda: win.history.get("k_expired") is not None)

    win._handle_retry_click("k_expired")
    assert win.status_line == "这句已过期，无法重试"
    assert "这句已过期，无法重试" in win._last_html


def test_stale_language_revision_rejected():
    """按 CLAUDE.md 第 42 条：跨语言的旧事件在消费端拦，旧代数 translate_failed 被拒。"""
    win = _window()
    win.history.clear()
    win.language_revision = 3

    # 发送代数 2 的事件（旧语言）
    win.on_translate_failed("Alter Satz", 0, 1000, "k_stale", 2, "timeout")
    _pump_until(lambda: False, timeout_ms=50)

    assert win.history.get("k_stale") is None


def test_dropped_reason_renders_marker():
    """reason == 'dropped' 也显示同样标记（用户能看到有句子被丢了）。"""
    win = _window()
    win.history.clear()

    win.on_translate_failed("Zu voll im Puffer", 0, 1000, "k_dropped", win.language_revision, "dropped")
    assert _pump_until(lambda: win.history.get("k_dropped") is not None)

    html = win._last_html
    assert design_tokens.TEXT_UNTRANSLATED in html
    assert 'href="retry:k_dropped"' in html


def test_update_dst_syncs_all_windows():
    """update_dst 按 key 原地更新译文：主窗、历史窗、电视窗、影院条一致。"""
    win = _window()
    win.history.clear()

    win.add_pair("Erste Zeile", "初版译文", 0, 1000, "k_multi", win.language_revision)
    assert _pump_until(lambda: "初版译文" in win._last_html)

    win.history_window.show()
    win.tv_window.show()
    win.tv_window.rebuild_from_store()
    win.update_dst("k_multi", "修正后的终版译文")
    assert "修正后的终版译文" in win._last_html

    # 历史窗/电视窗：靠 update 事件刷新，不手动重建
    for view in (win.history_window, win.tv_window):
        text = view.text.toPlainText()
        assert "修正后的终版译文" in text and "初版译文" not in text
    win.history_window.hide()

    # 影院条
    win.cinema_bar.show()
    win.cinema_bar.show_last()
    win.update_dst("k_multi", "修正后的终版译文")
    assert win.cinema_bar.chinese_label.text() == "修正后的终版译文"
    win.cinema_bar.hide()
