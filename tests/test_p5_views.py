"""P5 远端句替换：历史窗/电视窗/影院条也要按同一句 id 去掉最初的本机句。

背景（.governance/release-notes-v3.2.0-beta.md 已知问题）：远端句替换本机句
原本只作用于悬浮窗，三个辅助视图仍保留最初的本机句；随后远端句又被追加一遍
→ 历史窗/电视窗同一内容显示两遍、影院条残留旧文本。替换语义和 LineStore 一致：
删掉本机句、远端句按新（负数）key 重新上屏，后续节点译文更新按那个 key 找。

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 先于 PyQt6 加载（Qt6 下已不再复现 WinError 1114，保留理由见 CLAUDE.md 第 4 节第 1 条）。
⚠️ QApplication 必须持模块级引用，否则会被立即 GC → 建 QWidget 时 qFatal 秒退。
"""
import sys

import torch  # noqa: F401  先于 PyQt6

sys.stdout.reconfigure(encoding="utf-8")

from PyQt6.QtWidgets import QApplication

import realtime_subtitle.ui.design_tokens as design_tokens
import realtime_subtitle.ui.subtitle_window as sw_mod
from realtime_subtitle.app import SubtitleApp
from realtime_subtitle.ui.cinema_bar import CinemaBar
from realtime_subtitle.ui.popups import HistoryWindow
from realtime_subtitle.ui.tv_window import TVWindow
from realtime_subtitle.ui.history_store import SubtitleHistoryStore


_APP = None  # 必须持有引用，理由同上


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _shown_bar(store=None):
    _app()
    bar = CinemaBar(store if store is not None else SubtitleHistoryStore())
    bar.resize(600, 200)
    bar.show()
    return bar


# ---------------------------------------------------------------------------
# 共享 store 的删除事件（按 id 移除，找不到静默忽略）
# ---------------------------------------------------------------------------

def test_history_delete_removes_only_matching():
    _app()
    store = SubtitleHistoryStore()
    HistoryWindow(store)
    store.add("Hallo", "你好", key="a")
    store.add("Welt", "世界", key="b")

    store.drop(["a"])
    assert [row.key for row in store.rows()] == ["b"]

    # 未知 id 静默忽略，不新增、不崩
    store.drop(["missing"])
    assert [row.key for row in store.rows()] == ["b"]

    # 空集合不做事
    store.drop([])
    store.drop(None)
    assert [row.key for row in store.rows()] == ["b"]


def test_tv_delete_rebuilds_only_matching():
    _app()
    store = SubtitleHistoryStore()
    win = TVWindow(store)
    win.resize(400, 300)
    win.show()
    try:
        store.add("第一句", "第一句", key="a")
        store.add("第二句", "第二句", key="b")

        store.drop(["a"])
        assert [row.key for row in store.rows()] == ["b"]
        assert win.text.toPlainText() == "第二句"

        store.drop(["missing"])
        assert win.text.toPlainText() == "第二句"
    finally:
        win.hide()


def test_cinema_delete_clears_current_only():
    store = SubtitleHistoryStore()
    bar = _shown_bar(store)
    store.add("Hallo", "你好", key="a")

    store.drop(["b"])  # 不是当前句：影院条不受影响
    assert bar.chinese_label.text() == "你好"

    store.drop(["a"])
    assert bar._current_key is None
    assert bar.chinese_label.text() == ""
    assert bar.german_label.text() == ""


# ---------------------------------------------------------------------------
# 端到端接线：删除走 store 并通知三个辅助视图
# ---------------------------------------------------------------------------

def _wired_views(window):
    window.tv_window.resize(400, 300)
    window.tv_window.show()
    window.cinema_bar.resize(600, 200)
    window.cinema_bar.show()


def _app_with(window):
    app = SubtitleApp.__new__(SubtitleApp)
    app.subtitle_window = window
    return app


def test_remote_replacement_keeps_single_entry_per_view(tmp_path, monkeypatch):
    """本机句 → 远端替换：三个视图各只留一条，且显示的是远端文本。"""
    _app()
    monkeypatch.setattr(sw_mod, "STATE_FILE", str(tmp_path / "window_state.json"))
    window = sw_mod.SubtitleWindow()
    _wired_views(window)
    try:
        window.add_pair("Hallo Welt", "你好世界", 0.0, 2.0, "local-1", 0)
        assert len(window.history) == 1
        assert window.cinema_bar._current_key == "local-1"

        app = _app_with(window)
        app._ui_drop_keys(["local-1"])
        app._ui_show_line("Hallo Welt", "云端世界", 0.0, 2.0, -1, "remote", 0)

        assert [row.key for row in window.history.rows()] == ["-1"]
        assert window.history.last().dst == "云端世界"
        assert window.cinema_bar._current_key == "-1"
        assert window.cinema_bar.chinese_label.text() == "云端世界"
    finally:
        window.container.close()


def test_replaced_untranslated_marker_clears_in_all_views(tmp_path, monkeypatch):
    """未定稿（⟳）本机句被远端句替换后，三个视图的失败标记一并消失。"""
    _app()
    monkeypatch.setattr(sw_mod, "STATE_FILE", str(tmp_path / "window_state.json"))
    window = sw_mod.SubtitleWindow()
    _wired_views(window)
    marker = design_tokens.TEXT_UNTRANSLATED
    try:
        window.add_pair("Hallo Welt", "翻译中…", 0.0, 2.0, "local-1", 0)
        window.on_translate_failed("Hallo Welt", 0.0, 2.0, "local-1", 0, "boom")
        assert window.history.last().dst == marker
        window.tv_window.show()
        window.tv_window.rebuild_from_store()
        assert window.tv_window.text.toPlainText() == f"Hallo Welt {marker}"
        assert window.cinema_bar.chinese_label.text() == marker

        app = _app_with(window)
        app._ui_drop_keys(["local-1"])
        app._ui_show_line("Hallo Welt", "云端世界", 0.0, 2.0, -1, "remote", 0)

        assert all(row.dst != marker for row in window.history.rows())
        assert window.cinema_bar.chinese_label.text() == "云端世界"
    finally:
        window.container.close()


def test_delete_on_shadow_window_with_store_is_safe():
    """测试替身只要提供 store，应用删除路径就无需辅助视图。"""
    _app()

    class _Window:
        def __init__(self):
            self.history = SubtitleHistoryStore()
            self.history.add("a", "b", key="k", t0=0.0, t1=1.0)

    window = _Window()
    app = _app_with(window)
    app._ui_drop_keys(["k"])
    assert window.history.rows() == []
