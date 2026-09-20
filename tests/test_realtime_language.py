"""批次 5：实时链路的源/目标独立、自动检测不覆盖显式目标、跨语言旧事件不上屏。

对应审核 F03 和 O04。

☠️ 边界：这里**没有真实音频、没有真实 Whisper/Ollama、没有真实悬浮窗**。
O04 那组用真的 QApplication + 真的队列连接跑（`QT_QPA_PLATFORM=offscreen`），
所以"事件顺序 + 拒收"这条链是真的；但"用户看到的画面"仍需真机回归。
"""
import os
import sys
from threading import Lock

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"

# ☠️ torch 必须先于 PyQt5，否则 WinError 1114 (c10.dll)（CLAUDE.md 第 4 节第 1 条）。
# 别用 pytest.importorskip("PyQt5") 顶在前面——那就把顺序弄反了。
import torch  # noqa: F401,E402  先于 PyQt5
from PyQt5.QtWidgets import QApplication  # noqa: E402

import realtime_subtitle.config as config  # noqa: E402
from realtime_subtitle import language_policy  # noqa: E402
from realtime_subtitle.translate.translator_queue import (  # noqa: E402
    DecodeHealth, LanguageVote, WhisperQueueTranslator,
)
from realtime_subtitle.ui.subtitle_render import LiveTextRenderMixin  # noqa: E402
from realtime_subtitle.ui.subtitle_window import SubtitleSignals  # noqa: E402


# ☠️ QApplication 必须有模块级引用，被 GC 掉之后建 QWidget 直接 qFatal 秒退
# （CLAUDE.md 第 4 节第 16 条）
_APP = None


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _pairs(monkeypatch, pairs, source, target):
    monkeypatch.setattr(config, "LANGUAGE_PAIRS", list(pairs), raising=False)
    monkeypatch.setattr(config, "LANGUAGE_CYCLE", None, raising=False)
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", source, raising=False)
    monkeypatch.setattr(config, "TARGET_LANGUAGE", target, raising=False)


def _bare():
    t = object.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._asr_scheduled = True
    t._asr_backlog_n = 0
    t._asr_error_streak = 0
    t._audio_inbox = []
    t._pending_lang_switch = None
    t._pending_lang_source = None
    t._pending_lang_target = None
    t._lang_vote = LanguageVote()
    t._decode_health = DecodeHealth()
    t._lang_rescue = False
    t._lang_detect_next = 0.0
    t._lang_in_cooldown = False
    t._lang_revision = 0
    t.on_status = None
    t.on_display = None
    t.on_language_applied = None
    t.on_pair = None
    t.on_draft = None
    t.context_history = []
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._tx_epoch = 0
    t.pending_text = ""
    t._held_since = 0.0
    t._last_unstable = ""
    t._draft_last_text = ""
    t.processor = type("_P", (), {"init": lambda self: None})()
    return t


# ======================================================================
# F03：源语言和目标语言各自独立
# ======================================================================


def test_two_targets_for_one_source_are_both_reachable(monkeypatch):
    """配 zh→en 和 zh→de 两条时，第二条必须真的能切上。"""
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de")], "zh", "en")
    t = _bare()

    t._apply_pending_lang_switch("zh", source="manual", target="de")

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("zh", "de")


def test_panel_buttons_are_keyed_by_the_whole_pair(monkeypatch):
    """按源语言索引的话，后一个按钮会把前一个挤掉，面板上只剩一个。"""
    from realtime_subtitle.ui.settings_window import SettingsWindow

    _pairs(monkeypatch, [("zh", "en"), ("zh", "de")], "zh", "en")
    _app()
    win = SettingsWindow()
    try:
        assert set(win._lang_pair_buttons) == {("zh", "en"), ("zh", "de")}
        got = []
        win.on_language_change = lambda s, tgt=None: got.append((s, tgt))
        win._lang_pair_buttons[("zh", "de")].click()
        assert got == [("zh", "de")]
    finally:
        win.close()


def test_tuning_restores_the_exact_pair(monkeypatch):
    """重启恢复要恢复到用户上次选的那一条，不是 target_for 的第一条。"""
    from realtime_subtitle.ui import settings_window as sw

    _pairs(monkeypatch, [("zh", "en"), ("zh", "de")], "de", "zh")

    sw.apply_tuning({"SOURCE_LANGUAGE": "zh", "TARGET_LANGUAGE": "de"})

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("zh", "de")


def test_tuning_falls_back_when_the_saved_pair_no_longer_exists(monkeypatch):
    _pairs(monkeypatch, [("zh", "en")], "de", "zh")
    from realtime_subtitle.ui import settings_window as sw

    sw.apply_tuning({"SOURCE_LANGUAGE": "zh", "TARGET_LANGUAGE": "de"})

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("zh", "en")


def test_target_language_is_persisted(monkeypatch):
    from realtime_subtitle.ui import settings_window as sw

    assert "TARGET_LANGUAGE" in sw.TUNING_KEYS
    assert "SOURCE_LANGUAGE" in sw.TUNING_KEYS


# ======================================================================
# 自动检测只改源语言，不覆盖用户显式选过的目标
# ======================================================================


def test_explicit_target_is_recognised_without_an_extra_state_flag(monkeypatch):
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de")], "zh", "en")

    assert language_policy.is_explicit_target("zh", "en") is False  # 就是默认
    assert language_policy.is_explicit_target("zh", "de") is True


def test_auto_detection_keeps_an_explicit_target(monkeypatch):
    """用户在中文下显式选了德语；自动检测切到英语时德语要保住。"""
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de"), ("en", "zh"), ("en", "de")],
           "zh", "de")
    t = _bare()

    t._apply_pending_lang_switch("en", source="auto")

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("en", "de")


def test_auto_detection_drops_a_target_that_would_translate_to_itself(monkeypatch):
    """显式德语 + 自动切到德语 = 德→德：识别对了但模型只会把原句抄一遍。"""
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de"), ("de", "zh")], "zh", "de")
    t = _bare()

    t._apply_pending_lang_switch("de", source="auto")

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("de", "zh")


def test_auto_detection_drops_an_unconfigured_target(monkeypatch):
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de"), ("da", "zh")], "zh", "de")
    t = _bare()

    t._apply_pending_lang_switch("da", source="auto")

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("da", "zh")


def test_manual_switch_uses_the_default_target_for_that_source(monkeypatch):
    """手动只点源语言（热键 Ctrl+Alt+L）时按默认走，不继承上一个目标。"""
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de"), ("en", "zh")], "zh", "de")
    t = _bare()

    t._apply_pending_lang_switch("en", source="manual")

    assert (config.SOURCE_LANGUAGE, config.TARGET_LANGUAGE) == ("en", "zh")


def test_rescue_switch_also_respects_an_explicit_target(monkeypatch):
    _pairs(monkeypatch, [("zh", "en"), ("zh", "de"), ("en", "de")], "zh", "de")
    t = _bare()

    t._apply_pending_lang_switch("en", source="rescue")

    assert config.TARGET_LANGUAGE == "de"


# ======================================================================
# O04：跨语言的旧事件不许污染 UI
# ======================================================================


def test_language_revision_only_moves_on_a_real_switch(monkeypatch):
    _pairs(monkeypatch, [("de", "zh"), ("zh", "de")], "de", "zh")
    t = _bare()

    t._apply_pending_lang_switch("de", source="manual")   # 没变化
    assert t._lang_revision == 0

    t._apply_pending_lang_switch("zh", source="manual")   # 真切了
    assert t._lang_revision == 1


def test_events_carry_the_revision_at_emit_time(monkeypatch):
    _pairs(monkeypatch, [("de", "zh"), ("zh", "de")], "de", "zh")
    t = _bare()
    pairs, drafts = [], []
    t.on_pair = lambda g, zh, rev=0: pairs.append(rev)
    t.on_draft = lambda text, rev=0: drafts.append(rev)

    t._emit_pair("Hallo", "你好")
    t._emit_draft("你好")
    t._apply_pending_lang_switch("zh", source="manual")
    t._emit_pair("你好", "Hallo")

    assert pairs == [0, 1]
    assert drafts == [0]


def test_stale_pair_is_rejected_at_the_last_consumer():
    """真 QApplication + 真队列连接：语言事件先到，旧句对随后到 → 拒收。

    ☠️ 这正是发出端拦不住的那个窗口：worker 查完代数、锁放开、切换插进来、
    然后 worker 才真正回调。所以判据必须在最后消费的槽里。
    """
    app = _app()

    class _Consumer:
        """只装 _add_pair 需要的东西，不建真窗口。"""

        def __init__(self):
            from realtime_subtitle.ui.subtitle_render import LiveTextRenderMixin
            self.__class__ = type("_C", (LiveTextRenderMixin, object), {})
            self.language_revision = 0
            self.accepted = []

        def _is_stale(self, rev):
            return LiveTextRenderMixin._is_stale_language(self, rev)

    consumer = _Consumer()
    signals = SubtitleSignals()
    signals.pair.connect(
        lambda g, zh, rev: None if LiveTextRenderMixin._is_stale_language(consumer, rev)
        else consumer.accepted.append((g, zh)))
    signals.language.connect(
        lambda s, t, rev: setattr(consumer, "language_revision", rev))

    # 发出顺序 = 真实时序：切换先生效，旧语言的句对随后才到
    signals.language.emit("zh", "de", 1)
    signals.pair.emit("Guten Tag", "你好", 0)     # 切换前发出的，代数旧
    signals.pair.emit("你好", "Guten Tag", 1)     # 新语言的
    app.processEvents()

    assert consumer.accepted == [("你好", "Guten Tag")], (
        "切换前发出的旧语言句对不该落在新语言的画面上")


def test_pair_emitted_before_the_switch_still_shows(monkeypatch):
    """别矫枉过正：切换**之前**到达的最后一条旧语言字幕是该显示的。"""
    app = _app()
    consumer = type("_C", (), {"language_revision": 0, "accepted": []})()
    signals = SubtitleSignals()
    signals.pair.connect(
        lambda g, zh, rev: None if LiveTextRenderMixin._is_stale_language(consumer, rev)
        else consumer.accepted.append(g))
    signals.language.connect(
        lambda s, t, rev: setattr(consumer, "language_revision", rev))

    signals.pair.emit("Letzter Satz", "最后一句", 0)
    signals.language.emit("zh", "de", 1)
    app.processEvents()

    assert consumer.accepted == ["Letzter Satz"]
