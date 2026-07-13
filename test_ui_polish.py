"""UI 丝滑体验单测：chrome 淡入淡出 / status 5s 自消 / _render 文档缓存。

运行: venv\\Scripts\\python.exe -m pytest test_ui_polish.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114（见 main.py / test_hittest.py）。
⚠️ QApplication 必须持有模块级引用，否则会被立即 GC → 建 QWidget 时 qFatal 秒退。
"""
import torch  # noqa: F401  先于 PyQt5
import sys
import os
import tempfile

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication, QGraphicsOpacityEffect
from PyQt5.QtCore import QPropertyAnimation

import config
import subtitle_window
from subtitle_window import SubtitleWindow


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退
_WIN = None
_TMPDIR = None
_ORIG_STATE_FILE = subtitle_window.STATE_FILE
_ORIG_FONT_SIZE = config.FONT_SIZE


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _window():
    """懒建真实 SubtitleWindow；STATE_FILE 指到临时路径，避免写坏真 window_state.json。"""
    global _WIN, _TMPDIR, _APP
    if _WIN is not None:
        return _WIN
    _app()
    _TMPDIR = tempfile.mkdtemp(prefix="rs_ui_polish_")
    subtitle_window.STATE_FILE = os.path.join(_TMPDIR, "window_state.json")
    _WIN = SubtitleWindow()
    _APP = _WIN.app  # 确保模块级持有
    # 关掉会写盘/4s 自动藏 chrome 的副作用，测试可控
    _WIN._state_timer.stop()
    return _WIN


def _pump(n=30, ms_each=20):
    """推进事件循环，让属性动画跑到终态。"""
    app = _app()
    for _ in range(n):
        app.processEvents()
        # QTest 不一定总在环境里；用 processEvents 足够让短动画收束
        from PyQt5.QtCore import QThread
        QThread.msleep(ms_each)


def test_chrome_fade_objects_are_reused_not_recreated():
    """动画/效果对象一次创建，反复 hover 仍是同一实例。"""
    win = _window()
    drag_fx = win._drag_opacity
    btn_fx = win._btn_opacity
    drag_anim = win._drag_fade
    btn_anim = win._btn_fade
    assert isinstance(drag_fx, QGraphicsOpacityEffect)
    assert isinstance(btn_fx, QGraphicsOpacityEffect)
    assert isinstance(drag_anim, QPropertyAnimation)
    assert isinstance(btn_anim, QPropertyAnimation)

    win._set_controls_visible(False)
    win._set_controls_visible(True)
    win._set_controls_visible(False)
    assert win._drag_opacity is drag_fx
    assert win._btn_opacity is btn_fx
    assert win._drag_fade is drag_anim
    assert win._btn_fade is btn_anim


def test_chrome_fade_final_visibility():
    """淡入/淡出终态 isVisible 正确；淡出 finished 后才隐藏。"""
    win = _window()

    win._set_controls_visible(True)
    _pump(n=20, ms_each=15)
    assert win.drag_bar.isVisible()
    assert win.btn_bar.isVisible()
    assert win._drag_opacity.opacity() >= 0.99
    assert win._btn_opacity.opacity() >= 0.99

    win._set_controls_visible(False)
    # 淡出刚开始：仍应 visible（HTCAPTION 门控语义）
    assert win.drag_bar.isVisible()
    assert win.btn_bar.isVisible()
    _pump(n=30, ms_each=20)
    assert not win.drag_bar.isVisible()
    assert not win.btn_bar.isVisible()
    assert win._drag_opacity.opacity() <= 0.01
    assert win._btn_opacity.opacity() <= 0.01


def test_chrome_rapid_toggle_no_crash():
    """快速反复进出 hover 10 次不异常，终态与最后一次一致。"""
    win = _window()
    for i in range(10):
        win._set_controls_visible(i % 2 == 0)
        _app().processEvents()
    # 最后一次 i=9 → False
    win._set_controls_visible(False)
    _pump(n=30, ms_each=20)
    assert not win.drag_bar.isVisible()
    assert not win.btn_bar.isVisible()

    win._set_controls_visible(True)
    _pump(n=20, ms_each=15)
    assert win.drag_bar.isVisible()
    assert win.btn_bar.isVisible()


def test_status_auto_clear_on_timer():
    """_show_status 后 status_line 有值；模拟超时后清空。"""
    win = _window()
    # 先喂一点内容，否则 _render 在 blocks 为空时直接 return，status 上不了屏
    win._add_pair("Hallo", "你好")
    win._show_status("👻 穿透已开启")
    assert win.status_line == "👻 穿透已开启"
    assert win._status_clear_timer.isActive()

    # 直接触发超时回调（不真等 5s）
    win._status_clear_timer.stop()
    win._clear_status()
    assert win.status_line == ""
    # 屏上不应再挂 status：_last_html 不应含该提示
    assert "穿透已开启" not in (win._last_html or "")


def test_status_timer_resets_on_new_status():
    """新 status 重置单发计时器。"""
    win = _window()
    win._add_pair("Welt", "世界")
    win._show_status("first")
    assert win._status_clear_timer.isActive()
    win._show_status("second")
    assert win.status_line == "second"
    assert win._status_clear_timer.isActive()
    # 超时后清空
    win._status_clear_timer.timeout.emit()
    assert win.status_line == ""


def test_doc_cache_reused_across_renders():
    """连续两次 _render 后 _doc_cache 是同一对象。"""
    win = _window()
    win._add_pair("Eins", "一")
    win._render()
    doc1 = win._doc_cache
    assert doc1 is not None
    win._update_live("Zwei", "")
    win._render()
    assert win._doc_cache is doc1


def test_doc_cache_rebuilds_on_font_change():
    """改 config.FONT_SIZE 后 _render 重建文档。"""
    win = _window()
    win._add_pair("Drei", "三")
    win._render()
    doc1 = win._doc_cache
    key1 = win._doc_cache_key
    assert doc1 is not None

    snap = config.FONT_SIZE
    try:
        config.FONT_SIZE = snap + 4
        win._render()
        assert win._doc_cache is not doc1
        assert win._doc_cache_key != key1
        assert win._doc_cache_key[1] == snap + 4
    finally:
        config.FONT_SIZE = snap


def test_hit_test_build_doc_is_independent():
    """点词路径 _build_doc 不共享缓存实例。"""
    win = _window()
    win._add_pair("Testwort", "测试词")
    win._render()
    cached = win._doc_cache
    fresh = win._build_doc(win._last_html)
    assert fresh is not cached
