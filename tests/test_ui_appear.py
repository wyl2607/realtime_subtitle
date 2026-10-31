"""P4「出现体验」单测：粘住可见集合 / minHold / 新句淡入。

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）；import 前设
REALTIME_SUBTITLE_NO_SINGLETON=1。
⚠️ QApplication 必须持模块级引用，否则被 GC 后建 QWidget 直接 qFatal。
⚠️ 异步断言只用 _pump_until 等条件成立，不写固定等待。
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
os.environ.setdefault("REALTIME_SUBTITLE_NO_SINGLETON", "1")

import torch  # noqa: F401,E402  先于 PyQt6（项目惯例）
import sys  # noqa: E402

from PyQt6.QtWidgets import QApplication  # noqa: E402

import realtime_subtitle.config as config  # noqa: E402
import realtime_subtitle.ui.design_tokens as design_tokens  # noqa: E402
import realtime_subtitle.ui.subtitle_window as subtitle_window  # noqa: E402
from realtime_subtitle.ui.subtitle_window import SubtitleWindow  # noqa: E402

_APP = None
_WINDOWS = []
_SAMPLES = [
    ("Heute sprechen wir über das Wetter in Norddeutschland.", "今天我们聊聊德国北部的天气。"),
    ("Am Morgen ist der Himmel noch fast vollständig bedeckt.", "清晨天空还几乎完全被云遮住。"),
    ("Gegen Mittag soll die Sonne endlich wieder herauskommen.", "接近中午阳光应该会重新露出来。"),
    ("Die Temperaturen liegen zwischen acht und vierzehn Grad.", "气温大约在八度到十四度之间。"),
]


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


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
    app.processEvents()
    return pred()


def _new_window():
    _app()
    original = subtitle_window.SubtitleWindow._load_state
    subtitle_window.SubtitleWindow._load_state = staticmethod(lambda: {})
    try:
        win = SubtitleWindow()
    finally:
        subtitle_window.SubtitleWindow._load_state = staticmethod(original)
    win._state_timer.stop()
    win._status_clear_timer.stop()
    win._save_state_if_changed = lambda: None
    _WINDOWS.append(win)
    _APP = win.app
    return win


def _reset_view(win):
    win.sentence_pairs.clear()
    win.live_committed = ""
    win.live_unstable = ""
    win.live_draft = ""
    win.status_line = ""
    win._visible_pair_keys = []
    win._shown_at.clear()
    win._ever_shown.clear()
    win._hold_deadline = None
    win._hold_timer.stop()
    win._stop_fade()


def _height_for_pairs(win, count):
    """把窗口高度调到刚好装下 count 句（live 行为空）。"""
    blocks = [
        win._pair_html(german, chinese, dim=(index < count - 1),
                       top_gap=(index > 0), key=f"cal{index}")
        for index, (german, chinese) in enumerate(_SAMPLES[:count])
    ]
    doc = win._doc_for_render()
    doc.setHtml(win._join_render_blocks(blocks, []))
    return int(doc.size().height() + 40 + 2)


def _size_window(win, pairs):
    win.container.setGeometry(0, 0, 1200, 200)
    win._on_container_resize()
    win.container.setGeometry(0, 0, 1200, _height_for_pairs(win, pairs))
    win._on_container_resize()
    _app().processEvents()


def _add(win, german, chinese, key, revision=0):
    win._add_pair(german, chinese, {
        "revision": revision, "t0": None, "t1": None, "key": key, "source": "local",
    })


def _defaults():
    return (
        config.FONT_SIZE,
        config.SHOW_BILINGUAL,
        config.MAX_SENTENCE_PAIRS,
        config.CHINESE_TEXT_COLOR,
        config.TEXT_COLOR,
    )


def _restore(saved):
    (config.FONT_SIZE, config.SHOW_BILINGUAL, config.MAX_SENTENCE_PAIRS,
     config.CHINESE_TEXT_COLOR, config.TEXT_COLOR) = saved


def test_live_and_draft_growth_does_not_evict_visible_pairs():
    """live 行变高/清空、草稿变长都不能让已上屏句对消失（顶部裁切可以）。"""
    saved = _defaults()
    try:
        config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
        config.SHOW_BILINGUAL = True
        config.MAX_SENTENCE_PAIRS = 20
        win = _new_window()
        _size_window(win, 2)
        _reset_view(win)

        _add(win, *_SAMPLES[0], "s0")
        _add(win, *_SAMPLES[1], "s1")
        keys = list(win._visible_pair_keys)
        assert keys == ["s0", "s1"]

        long_de = " ".join(_SAMPLES[0][0].split() * 3)
        long_zh = "很长的草稿译文，" * 12
        win._update_live(long_de, long_de)
        assert win._visible_pair_keys == keys
        win._update_draft(long_zh)
        assert win._visible_pair_keys == keys
        win._update_draft("")
        assert win._visible_pair_keys == keys
        win._update_live("", "")
        assert win._visible_pair_keys == keys
    finally:
        _restore(saved)


def test_update_dst_does_not_change_visible_pair_keys():
    saved = _defaults()
    try:
        config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
        config.SHOW_BILINGUAL = True
        config.MAX_SENTENCE_PAIRS = 20
        win = _new_window()
        _size_window(win, 2)
        _reset_view(win)

        _add(win, *_SAMPLES[0], "s0")
        _add(win, *_SAMPLES[1], "s1")
        keys = list(win._visible_pair_keys)

        win.update_dst("s0", "换成一版明显更长的润色译文，" * 6)
        assert win._visible_pair_keys == keys
    finally:
        _restore(saved)


def test_min_hold_keeps_oldest_until_deadline_then_releases():
    """窗口只够 2 句时连续来 3 句：最旧句未满 3s 先保留，到期后释放。"""
    saved = _defaults()
    try:
        config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
        config.SHOW_BILINGUAL = True
        config.MAX_SENTENCE_PAIRS = 20
        win = _new_window()
        _size_window(win, 2)
        _reset_view(win)

        clock = [0.0]
        win._now = lambda: clock[0]

        _add(win, *_SAMPLES[0], "s0")
        clock[0] = 2.4
        _add(win, *_SAMPLES[1], "s1")
        clock[0] = 2.6
        _add(win, *_SAMPLES[2], "s2")

        assert "s0" in win._visible_pair_keys
        assert win._hold_deadline is not None
        assert abs(win._hold_deadline - design_tokens.MOTION_MIN_HOLD_SEC) < 1e-6

        clock[0] = design_tokens.MOTION_MIN_HOLD_SEC + 0.05
        win._on_hold_release()

        assert "s0" not in win._visible_pair_keys
        assert win._hold_deadline is None
        win._hold_timer.stop()
    finally:
        _restore(saved)


def test_real_hold_timer_releases_without_manual_call(monkeypatch):
    """真实 QTimer 路径：把 minHold 缩短到 0.2s，等条件成立即释放。"""
    saved = _defaults()
    try:
        config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
        config.SHOW_BILINGUAL = True
        config.MAX_SENTENCE_PAIRS = 20
        monkeypatch.setattr(design_tokens, "MOTION_MIN_HOLD_SEC", 0.2)
        win = _new_window()
        _size_window(win, 2)
        _reset_view(win)

        _add(win, *_SAMPLES[0], "s0")
        _add(win, *_SAMPLES[1], "s1")
        _add(win, *_SAMPLES[2], "s2")
        assert "s0" in win._visible_pair_keys

        assert _pump_until(lambda: "s0" not in win._visible_pair_keys)
        assert win._hold_deadline is None
    finally:
        _restore(saved)


def test_new_key_fades_in_and_old_keys_do_not_refade():
    saved = _defaults()
    try:
        config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
        config.SHOW_BILINGUAL = True
        config.MAX_SENTENCE_PAIRS = 20
        win = _new_window()
        _size_window(win, 2)
        _reset_view(win)

        _add(win, *_SAMPLES[0], "s0")
        assert win._fade_key == "s0"
        assert _pump_until(lambda: win._fade_key is None)
        assert win._fade_alpha == 1.0

        _add(win, *_SAMPLES[1], "s1")
        assert win._fade_key == "s1"
        assert _pump_until(lambda: win._fade_key is None)

        # 淡入结束后：最新句全不透明，上一句 dim。
        blocks = [part for part in win._last_html.split("<div ") if part]
        assert "opacity:" not in blocks[-1]
        assert "opacity:0.60" in blocks[-2]

        # update_dst 只是原位换译文，不触发淡入。
        win.update_dst("s0", "换成一版更长的润色译文，" * 6)
        assert win._fade_key is None

        # 再渲染一次也不会让旧 key 重新淡入。
        win._render()
        assert win._fade_key is None
        assert "s0" in win._ever_shown and "s1" in win._ever_shown
    finally:
        _restore(saved)


def test_language_switch_resets_hold_deadline_and_does_not_release_new_sentence():
    saved = _defaults()
    try:
        config.FONT_SIZE = design_tokens.TYPE_SUBTITLE_DEFAULT
        config.SHOW_BILINGUAL = True
        config.MAX_SENTENCE_PAIRS = 20
        win = _new_window()
        _size_window(win, 2)
        _reset_view(win)

        clock = [0.0]
        win._now = lambda: clock[0]
        _add(win, *_SAMPLES[0], "s0")
        clock[0] = 2.4
        _add(win, *_SAMPLES[1], "s1")
        clock[0] = 2.6
        _add(win, *_SAMPLES[2], "s2")
        assert win._hold_deadline is not None

        # 语言切换：旧截止点清掉，计时锚到此刻。
        win._on_language_applied("de", "zh", 1)
        assert win._hold_deadline is None
        assert not win._hold_timer.isActive()

        # 即使推进过了旧截止时刻，刚上屏的新语言句子也不能被旧计时放走。
        clock[0] = design_tokens.MOTION_MIN_HOLD_SEC + 0.05
        win._on_hold_release()
        assert "s0" in win._visible_pair_keys
        win._hold_timer.stop()
    finally:
        _restore(saved)
