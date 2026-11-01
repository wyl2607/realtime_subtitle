"""📜/📺/🎞 三个视图订阅同一个 SubtitleHistoryStore 的集成测试（spec 003 P3）。"""
import os
import sys

import torch  # noqa: F401

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication, QFileDialog

from realtime_subtitle.ui.cinema_bar import CinemaBar
from realtime_subtitle.ui.history_store import SubtitleHistoryStore
from realtime_subtitle.ui.popups import HistoryWindow
from realtime_subtitle.ui.tv_window import TVWindow


_APP = None


def _app():
    global _APP
    _APP = QApplication.instance() or QApplication(sys.argv)
    return _APP


def _pump_until(predicate, timeout_ms=1500):
    from PyQt6.QtCore import QThread
    waited = 0
    while waited < timeout_ms:
        _app().processEvents()
        if predicate():
            return True
        QThread.msleep(10)
        waited += 10
    return predicate()


def _views():
    _app()
    store = SubtitleHistoryStore()
    history = HistoryWindow(store)
    tv = TVWindow(store)
    cinema = CinemaBar(store)
    tv.resize(500, 300)
    cinema.resize(600, 180)
    history.show()
    tv.show()
    cinema.show()
    return store, history, tv, cinema


def test_shared_store_keeps_all_views_consistent_after_remote_replacement():
    store, history, tv, cinema = _views()
    try:
        store.add("Erste Quelle", "第一译文", key="local", t0=2, t1=3)
        assert _pump_until(lambda: cinema.chinese_label.text() == "第一译文")
        store.update("local", dst="修正译文")
        assert _pump_until(lambda: "修正译文" in tv.text.toPlainText())
        store.drop(["local"])
        store.add("远端来源", "远端译文", key="-1", t0=1, t1=2)
        store.move_by_t0("-1", source="remote")
        assert [row.key for row in store.rows()] == ["-1"]
        assert "远端译文" in history.text.toPlainText()
        assert "远端译文" in tv.text.toPlainText()
        assert cinema.chinese_label.text() == "远端译文"
    finally:
        history.hide()
        tv.hide()
        cinema.hide()


def test_history_search_copy_export_and_language_boundary(tmp_path, monkeypatch):
    store, history, tv, cinema = _views()
    try:
        store.add("Haus", "房子", key="a", wall=1, revision=0)
        store.mark_language("de", "zh", 1)
        store.add("Baum", "树", key="b", wall=2, revision=1)
        assert "切换到 de→zh" in history.text.toPlainText()
        history.search_box.setText("树")
        assert history.text.toPlainText().count("Baum") == 1
        history._copy_all()
        assert "Baum" in QApplication.clipboard().text()
        target = tmp_path / "history.srt"
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(target), "SRT 文件 (*.srt)"))
        history._export_srt()
        assert "Baum" in target.read_text(encoding="utf-8")
    finally:
        history.hide()
        tv.hide()
        cinema.hide()


def test_hidden_tv_rebuilds_from_store_when_opened():
    _app()
    store = SubtitleHistoryStore()
    tv = TVWindow(store)
    store.add("Hallo", "你好", key="a")
    assert tv.text.toPlainText() == ""
    tv.show()
    tv.rebuild_from_store()
    assert _pump_until(lambda: tv.text.toPlainText() == "你好")
    tv.hide()


def test_hidden_history_window_catches_up_when_shown():
    _app()
    store = SubtitleHistoryStore()
    history = HistoryWindow(store)
    store.add("Fenster", "窗户", key="a")
    store.update("a", dst="窗子")
    assert history.text.toPlainText() == ""  # 藏着不重建
    history.show()
    try:
        assert _pump_until(lambda: "窗子" in history.text.toPlainText())
        assert "窗户" not in history.text.toPlainText()
    finally:
        history.hide()


def test_history_export_failure_is_reported_not_raised(tmp_path, monkeypatch):
    _app()
    store = SubtitleHistoryStore()
    history = HistoryWindow(store)
    store.add("Tisch", "桌子", key="a")
    missing = tmp_path / "no-such-dir" / "out.srt"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (str(missing), ""))
    history._export_srt()
    assert "导出失败" in history.hint.text()
