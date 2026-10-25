"""识别轮次用未稳定尾部确认被扣留的句尾。"""
from threading import Lock

import numpy as np

import realtime_subtitle.config as config
import realtime_subtitle.translate.translator_queue as translator_queue
from realtime_subtitle.asr.streaming_asr import TimedText
from realtime_subtitle.translate.language_switch import DecodeHealth, LanguageVote
from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator


class _Processor:
    """每轮按脚本交出已提交和未稳定文本，不加载真实模型。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.audio = 0
        self.last_avg_logprob = None

    def insert_audio_chunk(self, audio):
        self.audio += len(audio)

    def buffer_seconds(self):
        return self.audio / float(config.SAMPLE_RATE)

    def process_iter(self):
        return self.rounds.pop(0)


class _Executor:
    def submit(self, *args, **kwargs):
        return None


def _translator(rounds):
    """参考 test_pipeline_helpers：只接通 _process_items 所需的真实管线。"""
    t = object.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._tx_epoch = 0
    t._lang_revision = 0
    t._sample_clock_samples = 0
    t._sentence_seq = 0
    t.pending_text = ""
    t.pending_words = []
    t._held_since = 0.0
    t._release_pending = False
    t._last_unstable = ""
    t._draft_last_text = ""
    t._stat_held_release = 0
    t._stat_held_misfire = 0
    t._stats_lock = Lock()
    t._lang_vote = LanguageVote()
    t._decode_health = DecodeHealth()
    t._lang_rescue = False
    t._lang_detect_next = 0.0
    t._lang_in_cooldown = False
    t._lang_hold_since = 0.0
    t._lang_held = []
    t._lang_hold_lang = None
    t._lang_hold_spent = False
    t.on_display = None
    t.on_draft = None
    t.on_status = None
    t.on_language_applied = None
    t.closing = False
    t._tx_executor = _Executor()
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t.last_audio_time = 0.0
    t.last_capture_end = None
    t._asr_busy = False
    t._check_decode_health = lambda: None
    t._maybe_detect_language = lambda: None
    t._update_lang_hold = lambda: None
    t._maybe_draft = lambda: None
    t._stat_note_asr = lambda *args, **kwargs: None
    t.processor = _Processor(rounds)
    return t


def _round(t, capture_time=1.0):
    t._process_items([(np.zeros(1600, dtype=np.float32), capture_time)])


def test_uppercase_unstable_releases_held_sentence_in_same_asr_round(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Die Urkunde wurde."), "Es leben noch")])

    _round(t)

    assert [str(s) for s in t._tx_queue] == ["Die Urkunde wurde."]
    assert t._held_since == 0.0
    assert t._release_pending is True
    assert t._stat_held_release == 1


def test_lowercase_unstable_keeps_boundary_held_until_the_words_commit(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    now = [100.0]
    monkeypatch.setattr(translator_queue.time, "time", lambda: now[0])
    t = _translator([
        (TimedText("Die Urkunde wurde."), "fortgesetzt"),
        (TimedText(" fortgesetzt. Danach"), "geht es weiter"),
    ])

    _round(t)
    now[0] += config.SENTENCE_HOLD_SEC + 1

    assert t._tx_queue == []
    assert t._held_since == 100.0

    _round(t, capture_time=2.0)

    assert [str(s) for s in t._tx_queue] == ["Die Urkunde wurde. fortgesetzt."]
    assert t._release_pending is False


def test_empty_unstable_releases_after_hold_timeout_once(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    now = [100.0]
    monkeypatch.setattr(translator_queue.time, "time", lambda: now[0])
    t = _translator([
        (TimedText("Die Urkunde wurde."), ""),
        (TimedText(""), ""),
        (TimedText(""), ""),
        (TimedText(""), ""),
    ])

    _round(t)
    now[0] += config.SENTENCE_HOLD_SEC - 0.01
    _round(t, capture_time=2.0)

    assert t._tx_queue == []
    assert t._held_since == 100.0

    now[0] += 0.02
    _round(t, capture_time=3.0)

    assert [str(s) for s in t._tx_queue] == ["Die Urkunde wurde."]
    assert t._held_since == 0.0
    assert t._stat_held_release == 1

    _round(t, capture_time=4.0)
    assert [str(s) for s in t._tx_queue] == ["Die Urkunde wurde."]


def test_abbreviation_veto_still_blocks_uppercase_unstable_lookahead(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Das gilt bzw."), "Danach geht es weiter")])

    _round(t)

    assert t._tx_queue == []
    assert t.pending_text == "Das gilt bzw."


def test_chinese_boundary_never_enters_held_lookahead_path(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "zh", raising=False)
    t = _translator([(TimedText("你好。"), "后面还有")])

    _round(t)

    assert [str(s) for s in t._tx_queue] == ["你好。"]
    assert t._held_since == 0.0
