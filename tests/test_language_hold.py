"""确认换语言期间暂停出字幕（config.LANGUAGE_HOLD_OUTPUT）。

CLAUDE.md 第 4 节第 31 条原来记着"一旦投票开始攒连击就暂停出字幕"的取舍：
好处是垃圾窗口变空白，坏处是投票没走完会白丢几秒真字幕。实现用"扣住"
代替"丢弃"绕开了那个坏处，这里盯三条结局：

  - 切换成立 → 扣住的句子一句都不许进翻译队列（它们是旧参数解码的废话）
  - 连击中断 → 扣住的句子原样、按原顺序放行（一句都不许丢）
  - 检测一直拿不到结果 → 到上限按超时放行，同一轮连击不再重新扣

☠️ 边界：没有真实音频/Whisper/Ollama。检测结果由假 processor 直接给出，
"实测垃圾窗口到底变空了没有"仍需真机开 AUTO_DETECT_LANGUAGE 放一段
换语言的视频看一遍。
"""
import os
from threading import Lock

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"

import realtime_subtitle.config as config  # noqa: E402
from realtime_subtitle.translate.translator_queue import (  # noqa: E402
    DecodeHealth, LanguageVote, WhisperQueueTranslator,
)


class _Proc:
    """假识别器：detect_language 按脚本依次吐结果。"""

    def __init__(self, script=()):
        self.script = list(script)
        self.inits = 0

    def detect_language(self, min_seconds=3.0):
        return self.script.pop(0) if self.script else None

    def buffer_seconds(self):
        return 0.0

    def init(self):
        self.inits += 1


class _Exec:
    def submit(self, fn, *a, **k):
        return None


def _setup(monkeypatch, *, auto=True, hold=True, max_sec=None, streak=3):
    monkeypatch.setattr(config, "LANGUAGE_PAIRS", [("de", "zh"), ("en", "zh")], raising=False)
    monkeypatch.setattr(config, "LANGUAGE_CYCLE", None, raising=False)
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh", raising=False)
    monkeypatch.setattr(config, "AUTO_DETECT_LANGUAGE", auto, raising=False)
    monkeypatch.setattr(config, "LANGUAGE_HOLD_OUTPUT", hold, raising=False)
    monkeypatch.setattr(config, "LANGUAGE_HOLD_MAX_SEC", max_sec, raising=False)
    monkeypatch.setattr(config, "LANGUAGE_SWITCH_STREAK", streak, raising=False)
    monkeypatch.setattr(config, "LANGUAGE_SWITCH_MIN_PROB", 0.85, raising=False)
    monkeypatch.setattr(config, "LANGUAGE_DETECT_INTERVAL", 4.0, raising=False)
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True, raising=False)


def _bare(script=()):
    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    t._asr_lock = Lock()
    t._asr_scheduled = True       # request_switch_language 看到它就不再 submit
    t._asr_executor = _Exec()
    t._asr_backlog_n = 0
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
    t.on_language_applied = None
    t.on_pair = None
    t.displays = []
    t.on_display = lambda committed, unstable: t.displays.append((committed, unstable))
    t.drafts = []
    t.on_draft = lambda text, rev: t.drafts.append(text)
    t.context_history = []
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._tx_epoch = 0
    t._tx_dropped = 0
    t._tx_executor = _Exec()
    t.pending_text = ""
    t._held_since = 0.0
    t._last_unstable = ""
    t._draft_last_text = ""
    t._draft_last_time = 0.0
    t.processor = _Proc(script)
    return t


def _detect_round(t, now):
    """模拟一轮识别末尾：检测（强制到点）→ 更新扣住状态。"""
    t._lang_detect_next = 0.0
    t._maybe_detect_language()
    t._update_lang_hold(now=now)


def test_vote_breaking_releases_every_held_sentence_in_order(monkeypatch):
    _setup(monkeypatch)
    t = _bare([("en", 0.99), ("de", 0.99)])

    t._enqueue_sentences(["Vorher gesagt."])           # 扣住之前的，照常进队列
    _detect_round(t, now=100.0)                          # en 连击 1 → 开始扣
    assert t._lang_hold_active()
    t._enqueue_sentences(["Eins.", "Zwei."])
    t._enqueue_sentences(["Drei."])
    assert t._tx_queue == ["Vorher gesagt."], "扣住期间不许进翻译队列"

    _detect_round(t, now=104.0)                          # 又检测回 de → 连击断
    assert not t._lang_hold_active()
    assert t._tx_queue == ["Vorher gesagt.", "Eins.", "Zwei.", "Drei."]
    assert config.SOURCE_LANGUAGE == "de"


def test_confirmed_switch_discards_held_sentences(monkeypatch):
    """走真实的 _maybe_detect_language → request_switch_language → _process_inbox。"""
    _setup(monkeypatch)
    t = _bare([("en", 0.99), ("en", 0.99), ("en", 0.99)])

    _detect_round(t, now=100.0)
    t._enqueue_sentences(["China und China."])
    _detect_round(t, now=104.0)
    t._enqueue_sentences(["China, China."])
    _detect_round(t, now=108.0)                          # 第 3 次：投票达成

    # ☠️ 投票达成那一轮 feed() 已把连击清零，但切换要到下一批边界才执行。
    # 这段空档必须继续扣，否则废话会在切换前一刻被放行上屏
    assert t._pending_lang_switch == "en"
    assert t._lang_hold_active()
    t._enqueue_sentences(["Und China."])
    assert t._tx_queue == []

    t._process_inbox()                                   # 消费切换请求

    assert config.SOURCE_LANGUAGE == "en"
    assert t._tx_queue == [], "旧参数解码的废话一句都不许送去翻译"
    assert not t._lang_hold_active()
    assert t._lang_held == []


def test_timeout_releases_and_does_not_rehold_the_same_streak(monkeypatch):
    _setup(monkeypatch, max_sec=10.0)
    # 第一次检测到 en，之后检测一直拿不到结果（音频太短）：连击卡在 1
    t = _bare([("en", 0.99)])

    _detect_round(t, now=100.0)
    t._enqueue_sentences(["Hallo zusammen."])
    t._update_lang_hold(now=105.0)
    assert t._lang_hold_active(), "没到上限，继续扣"

    t._update_lang_hold(now=111.0)
    assert not t._lang_hold_active()
    assert t._tx_queue == ["Hallo zusammen."]

    # 连击仍是 1，但这一轮已经超时放行过：不许每 10 秒扣一次、放一次
    _detect_round(t, now=112.0)
    assert t._lang_vote.streak == 1
    assert not t._lang_hold_active()


def test_idle_flush_path_also_releases_on_timeout(monkeypatch):
    """说话人停了就没有检测了，只剩 flush_pending 那个每秒一次的定时器。"""
    _setup(monkeypatch, max_sec=10.0)
    t = _bare([("en", 0.99)])
    _detect_round(t, now=100.0)
    t._enqueue_sentences(["Bis gleich."])
    t.last_audio_time = 0.0
    t._idle_flushed = True       # 让 flush_pending 在扣住判定之后直接返回

    monkeypatch.setattr("time.time", lambda: 200.0)
    t.flush_pending()

    assert t._tx_queue == ["Bis gleich."]


def test_live_line_hides_the_fragment_and_shows_a_hint(monkeypatch):
    _setup(monkeypatch)
    t = _bare([("en", 0.99)])
    t.pending_text = "China und"
    t._last_unstable = "China"

    _detect_round(t, now=100.0)
    t._emit_display()

    committed, unstable = t.displays[-1]
    assert "China" not in committed
    assert "确认中" in unstable
    assert config.LANGUAGE_NAMES["en"] in unstable


def test_hold_start_clears_the_stale_draft(monkeypatch):
    """草稿翻的正是被藏起来的那段残句，不撤掉就会孤零零挂在正常句子旁边。"""
    _setup(monkeypatch)
    t = _bare([("en", 0.99)])
    t._tx_queue = ["Ein normaler Satz."]   # live 行不会变空，UI 不会自己清草稿

    _detect_round(t, now=100.0)

    assert t.drafts == [""]


def test_no_draft_translation_while_holding(monkeypatch):
    _setup(monkeypatch)
    t = _bare([("en", 0.99)])
    _detect_round(t, now=100.0)
    t.drafts.clear()                 # 开始扣住时那条撤草稿的空串不算
    t.pending_text = "China und China und China"

    t._maybe_draft()

    assert t.drafts == []


def test_hold_disabled_passes_through(monkeypatch):
    _setup(monkeypatch, hold=False)
    t = _bare([("en", 0.99)])
    _detect_round(t, now=100.0)
    t._enqueue_sentences(["Eins."])
    assert not t._lang_hold_active()
    assert t._tx_queue == ["Eins."]


def test_autodetect_off_never_holds_even_with_a_stale_streak(monkeypatch):
    """面板里中途关掉自动检测：残留的连击不能让字幕继续被扣着。"""
    _setup(monkeypatch)
    t = _bare([("en", 0.99)])
    _detect_round(t, now=100.0)
    assert t._lang_hold_active()
    t._enqueue_sentences(["Eins."])

    monkeypatch.setattr(config, "AUTO_DETECT_LANGUAGE", False)
    t._update_lang_hold(now=101.0)

    assert not t._lang_hold_active()
    assert t._tx_queue == ["Eins."]


def test_manual_switch_while_holding_discards_and_clears(monkeypatch):
    _setup(monkeypatch)
    t = _bare([("en", 0.99)])
    _detect_round(t, now=100.0)
    t._enqueue_sentences(["China."])

    t._apply_pending_lang_switch("en", source="manual")

    assert not t._lang_hold_active()
    assert t._tx_queue == []


def test_default_max_hold_covers_one_full_vote(monkeypatch):
    """默认上限必须比正常走完一轮投票（STREAK × INTERVAL）更长，否则还没
    确认完就超时放行，扣住等于没扣。"""
    _setup(monkeypatch)
    t = _bare()
    full_vote = config.LANGUAGE_SWITCH_STREAK * config.LANGUAGE_DETECT_INTERVAL
    assert t._lang_hold_max_sec() > full_vote
