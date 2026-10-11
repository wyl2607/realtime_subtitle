from threading import Lock
from types import SimpleNamespace

import numpy as np

import realtime_subtitle.config as config
from realtime_subtitle.asr.streaming_asr import OnlineASRProcessor, TimedText
from realtime_subtitle.translate.language_switch import DecodeHealth, LanguageVote
from realtime_subtitle.translate.translator_queue import (
    TimedSentence,
    WhisperQueueTranslator,
)


class _FakeModel:
    def __init__(self, words):
        self.words = words

    def transcribe(self, *args, **kwargs):
        segment = SimpleNamespace(
            no_speech_prob=0.0,
            text="".join(w.word for w in self.words),
            words=self.words,
            avg_logprob=-0.2,
            end=max((w.end for w in self.words), default=0.0),
        )
        return iter([segment]), SimpleNamespace()


def _word(start, end, text):
    return SimpleNamespace(start=start, end=end, word=text)


def _bare_translator():
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
    t._stat_held_misfire = 0
    t._last_unstable = ""
    t._draft_last_text = ""
    t.context_history = []
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
    t.on_pair = None
    t.on_draft = None
    t.on_status = None
    t.on_language_applied = None
    return t


def test_process_iter_keeps_text_compatibility_and_committed_word_times(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    words = [_word(0.0, 0.4, " Hallo"), _word(0.4, 0.8, " Welt.")]
    p = OnlineASRProcessor(_FakeModel(words))

    p.insert_audio_chunk(np.zeros(16000, dtype=np.float32))
    first, unstable = p.process_iter()
    assert first == ""
    assert unstable == "Hallo Welt."

    second, unstable = p.process_iter()
    assert second == "Hallo Welt."
    assert isinstance(second, TimedText)
    assert second.words == [(0.0, 0.4, " Hallo"), (0.4, 0.8, " Welt.")]
    assert unstable == ""


def test_finish_keeps_tail_word_times(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    p = OnlineASRProcessor(_FakeModel([]))
    p.transcript_buffer.buffer = [(2.0, 2.3, " Tschuess.")]
    p.audio_buffer = np.zeros(16000 * 3, dtype=np.float32)

    tail = p.finish()

    assert tail == "Tschuess."
    assert isinstance(tail, TimedText)
    assert tail.words == [(2.0, 2.3, " Tschuess.")]


def test_german_committed_words_become_timed_sentence(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _bare_translator()

    t._append_committed(TimedText(
        "Hallo Welt.",
        [(10.0, 10.2, " Hallo"), (10.2, 10.6, " Welt.")],
    ))
    sentences = t._extract_sentences(final=True)

    assert [str(s) for s in sentences] == ["Hallo Welt."]
    assert isinstance(sentences[0], TimedSentence)
    assert (sentences[0].t0, sentences[0].t1) == (10.0, 10.6)
    assert sentences[0].source == "local"
    assert sentences[0].key.startswith("local:10.000-10.600:")


def test_chinese_committed_words_become_timed_sentence_without_spaces(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "zh", raising=False)
    t = _bare_translator()

    t._append_committed(TimedText(
        "你好世界。",
        [(20.0, 20.1, "你"), (20.1, 20.2, "好"),
         (20.2, 20.3, "世"), (20.3, 20.4, "界。")],
    ))
    sentences = t._extract_sentences(final=True)

    assert [str(s) for s in sentences] == ["你好世界。"]
    assert (sentences[0].t0, sentences[0].t1) == (20.0, 20.4)
    assert t.pending_text == ""
    assert t.pending_words == []


def test_clear_context_resets_processor_to_current_sample_clock():
    t = _bare_translator()
    offsets = []
    t._sample_clock_samples = 32000
    t.processor = SimpleNamespace(init=lambda offset=None: offsets.append(offset))

    t.clear_context()

    assert offsets == [2.0]


def test_long_capture_gap_resumes_at_sample_clock_not_zero(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    monkeypatch.setattr(config, "SAMPLE_RATE", 16000, raising=False)
    t = _bare_translator()
    t._sample_clock_samples = 16000
    t.last_audio_time = 0.0
    t.last_capture_end = 100.0
    t._idle_flushed = False
    t._asr_busy = False
    t._asr_backlog_n = 0
    t._stats_lock = Lock()
    t._stat_asr = []
    t._stat_buf_max = 0.0
    t._stat_merge = 0
    t._stat_asr_shortbuf = []
    t._stat_asr_longbuf = []
    t._stat_asr_solo = []
    t._stat_asr_overlap = []
    t._release_pending = False
    t._tx_executor = SimpleNamespace(submit=lambda *args, **kwargs: None)
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t._lookup_inflight_n = 0
    t._inflight_lock = Lock()
    t._draft_last_time = 0.0
    t._draft_last_text = ""
    t.closing = False
    t._check_decode_health = lambda: None
    t._maybe_detect_language = lambda: None
    t._update_lang_hold = lambda: None
    t._maybe_draft = lambda: None
    t._stat_note_asr = lambda *args, **kwargs: None

    offsets = []

    class _Processor:
        last_avg_logprob = None

        def __init__(self):
            self.inserted = []

        def buffer_seconds(self):
            return 1.0

        def finish(self):
            return TimedText("Alt.", [(0.5, 0.8, " Alt.")])

        def init(self, offset=None):
            offsets.append(offset)

        def insert_audio_chunk(self, audio):
            self.inserted.append(len(audio))

        def process_iter(self):
            return TimedText("", []), ""

    t.processor = _Processor()

    chunk = np.zeros(8000, dtype=np.float32)
    t._process_items([(chunk, 110.5)])

    assert offsets == [1.0]
    assert t._sample_clock_samples == 24000


def _pin_clock(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    monkeypatch.setattr(config, "SAMPLE_RATE", 16000, raising=False)


def _clock_pipeline(t):
    """_process_items / _process_inbox 用的最小接线。不提交翻译线程。"""
    t.last_audio_time = 0.0
    t.last_capture_end = None
    t._idle_flushed = False
    t._asr_busy = False
    t._asr_backlog_n = 0
    t._asr_scheduled = True
    t._audio_inbox = []
    t._inbox_dropped = 0
    t._inbox_drop_warned = 0
    t._sample_clock_needs_reanchor = False
    t._asr_error_streak = 0
    t._pending_lang_switch = None
    t._pending_lang_source = None
    t._pending_lang_target = None
    t._stats_lock = Lock()
    t._tx_executor = SimpleNamespace(submit=lambda *args, **kwargs: None)
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t.closing = False
    t._check_decode_health = lambda: None
    t._maybe_detect_language = lambda: None
    t._update_lang_hold = lambda: None
    t._maybe_draft = lambda: None
    t._stat_note_asr = lambda *args, **kwargs: None
    return t


class _SpeakingProcessor:
    """process_iter 不倒空缓冲，这样下一次长间隔仍看得到旧缓冲。"""

    def __init__(self):
        self.buffer_time_offset = 0.0
        self._audio = 0
        self.offsets = []
        self.last_avg_logprob = None

    def buffer_seconds(self):
        return self._audio / float(config.SAMPLE_RATE)

    def insert_audio_chunk(self, audio):
        self._audio += len(audio)

    def finish(self):
        self._audio = 0
        return TimedText("", [])

    def init(self, offset=None):
        self.offsets.append(offset)
        self.buffer_time_offset = 0.0 if offset is None else float(offset)
        self._audio = 0

    def process_iter(self):
        if self._audio <= 0:
            return TimedText("", []), ""
        t0 = self.buffer_time_offset
        # 留下音频：暂停/热切换后的下一次调用还要看到 buffer_seconds()>0。
        return TimedText(
            "Hallo. Mehr",
            [(t0, t0 + 0.5, " Hallo."), (t0 + 0.5, t0 + 0.75, " Mehr")],
        ), ""


class _DropProcessor:
    """丢块重锚：buffered=True 时先交一段旧句子，再按新 offset 出新句子。"""

    def __init__(self, *, buffered):
        self.buffer_time_offset = 0.0
        self._audio = 16000 if buffered else 0
        self.finishes = 0
        self.offsets = []
        self.last_avg_logprob = None

    def buffer_seconds(self):
        return self._audio / float(config.SAMPLE_RATE)

    def insert_audio_chunk(self, audio):
        self._audio += len(audio)

    def finish(self):
        self.finishes += 1
        self._audio = 0
        return TimedText("Alt.", [(0.25, 0.5, " Alt.")])

    def init(self, offset=None):
        self.offsets.append(offset)
        self.buffer_time_offset = 0.0 if offset is None else float(offset)
        self._audio = 0

    def process_iter(self):
        t0 = self.buffer_time_offset
        return TimedText(
            "Hallo. Mehr",
            [(t0, t0 + 0.5, " Hallo."), (t0 + 0.5, t0 + 0.75, " Mehr")],
        ), ""


def _enqueue_three_dropped_blocks(t, monkeypatch):
    """硬顶 2，再塞 5 块：正好丢掉 3 块、每块 16000 样本（3.0 秒，短于 5 秒间隔）。"""
    monkeypatch.setattr(config, "ASR_INBOX_MAX_BLOCKS", 2, raising=False)
    chunk = np.zeros(16000, dtype=np.float32)
    for i in range(5):
        t.enqueue_audio(chunk, float(i))
    return chunk


def test_pause_resume_sentence_t0_follows_sample_clock(monkeypatch):
    """`.paused` 在采集侧丢掉暂停期间的块，这里只留下一段没有样本的墙钟间隔。"""
    _pin_clock(monkeypatch)
    t = _clock_pipeline(_bare_translator())
    proc = _SpeakingProcessor()
    t.processor = proc
    chunk = np.zeros(16000, dtype=np.float32)

    t._process_items([(chunk, 10.0)])
    assert [s.t0 for s in t._tx_queue] == [0.0]
    assert t._sample_clock_samples == 16000

    before = t._sample_clock_samples
    t._process_items([(chunk, 50.0)])

    t0s = [s.t0 for s in t._tx_queue]
    assert t0s == [0.0, 0.5, 1.0]
    assert t0s == sorted(t0s)
    assert t._tx_queue[-1].t0 == before / config.SAMPLE_RATE
    assert t._tx_queue[-1].t0 > t._tx_queue[0].t0
    assert proc.offsets == [1.0]
    assert t._sample_clock_samples == 32000


def test_device_switch_resume_sentence_t0_follows_sample_clock(monkeypatch):
    """采集设备热切换会重开读循环，中间没有样本，恢复口径与暂停相同。"""
    _pin_clock(monkeypatch)
    t = _clock_pipeline(_bare_translator())
    proc = _SpeakingProcessor()
    t.processor = proc
    chunk = np.zeros(16000, dtype=np.float32)

    t._process_items([(chunk, 1.0), (chunk, 2.0)])
    assert [s.t0 for s in t._tx_queue] == [0.0]
    assert proc.offsets == []
    assert t._sample_clock_samples == 32000

    before = t._sample_clock_samples
    t._process_items([(chunk, 80.0)])

    t0s = [s.t0 for s in t._tx_queue]
    assert t0s == [0.0, 0.5, 2.0]
    assert t0s == sorted(t0s)
    assert t._tx_queue[-1].t0 == before / config.SAMPLE_RATE
    assert t._tx_queue[-1].t0 > t._tx_queue[0].t0
    assert proc.offsets == [2.0]
    assert t._sample_clock_samples == 48000


def test_consecutive_asr_errors_reanchor_without_zeroing_offset(monkeypatch):
    monkeypatch.setattr(config, "SAMPLE_RATE", 16000, raising=False)
    t = _clock_pipeline(_bare_translator())
    t._sample_clock_samples = 32000
    t._audio_inbox = [("boom", 1.0)]
    t._asr_error_streak = 1
    proc = _DropProcessor(buffered=False)
    t.processor = proc

    def boom(items):
        raise RuntimeError("asr failed")

    t._process_items = boom
    t._process_inbox()

    assert proc.offsets == [2.0]
    assert proc.buffer_time_offset == 2.0
    assert t._asr_error_streak == 0
    assert t._sample_clock_samples == 32000


def test_inbox_drop_reanchors_empty_buffer_to_sample_clock(monkeypatch):
    _pin_clock(monkeypatch)
    t = _clock_pipeline(_bare_translator())
    proc = _DropProcessor(buffered=False)
    t.processor = proc
    _enqueue_three_dropped_blocks(t, monkeypatch)
    assert t._sample_clock_samples == 48000
    assert t._sample_clock_needs_reanchor is True

    t._process_inbox()

    hallo = [s for s in t._tx_queue if str(s) == "Hallo."]
    assert len(hallo) == 1
    assert hallo[0].t0 == 3.0
    assert hallo[0].t0 == 48000 / config.SAMPLE_RATE
    assert proc.finishes == 0
    assert proc.offsets == [3.0]
    assert t._sample_clock_needs_reanchor is False
    # 留下的 2 块在重锚之后才计入，句子起点停在重锚时的样本钟，不落到 0。
    assert t._sample_clock_samples == 80000
    assert hallo[0].t0 != t._sample_clock_samples / config.SAMPLE_RATE


def test_inbox_drop_finishes_old_buffer_then_reanchors(monkeypatch):
    _pin_clock(monkeypatch)
    t = _clock_pipeline(_bare_translator())
    proc = _DropProcessor(buffered=True)
    t.processor = proc
    _enqueue_three_dropped_blocks(t, monkeypatch)

    t._process_inbox()

    assert [str(s) for s in t._tx_queue] == ["Alt.", "Hallo."]
    assert t._tx_queue[0].t0 == 0.25
    assert t._tx_queue[1].t0 == 3.0
    assert t._tx_queue[1].t0 == 48000 / config.SAMPLE_RATE
    assert proc.finishes == 1
    assert proc.offsets == [3.0]


# 模块级持有：QApplication 没人引用会被 GC，随后建 QWidget 直接 qFatal。
_APP = None
_WIN = None


def test_emit_pair_reaches_subtitle_window_and_rejects_stale_revision(monkeypatch, tmp_path):
    """_emit_pair → 真实 add_pair 槽。过期代数拒收；末项带上 t0/t1/source/key。"""
    import os
    import sys

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import torch  # noqa: F401  先于 PyQt6
    from PyQt6.QtWidgets import QApplication

    import realtime_subtitle.ui.subtitle_window as subtitle_window
    from realtime_subtitle.ui.subtitle_window import SubtitleWindow

    global _APP, _WIN
    app = QApplication.instance() or QApplication(sys.argv)
    _APP = app
    monkeypatch.setattr(subtitle_window, "STATE_FILE", str(tmp_path / "window_state.json"))
    monkeypatch.setattr(config, "FONT_SIZE", config.FONT_SIZE)
    monkeypatch.setattr(config, "TV_FONT_SIZE", config.TV_FONT_SIZE)
    monkeypatch.setattr(config, "BACKGROUND_OPACITY", config.BACKGROUND_OPACITY)

    win = SubtitleWindow()
    _WIN = win
    _APP = win.app
    win._state_timer.stop()

    t = _bare_translator()
    t.on_pair = win.add_pair
    win.language_revision = 2
    t._lang_revision = 1
    t._emit_pair("Alt.", "旧", t0=1.5, t1=2.0, key="local:1.500-2.000:1")
    app.processEvents()
    assert win.sentence_pairs == []

    t._lang_revision = 2
    t._emit_pair("Neu.", "新", t0=3.25, t1=4.5, key="local:3.250-4.500:9")
    app.processEvents()
    last = win.sentence_pairs[-1]
    assert last[0] == "Neu."
    assert last[3] == 3.25
    assert last[4] == 4.5
    assert last[5] == "local"
    assert last[6] == "local:3.250-4.500:9"

    t._lang_revision = 0
    t._emit_pair("Spaet.", "晚", t0=9.0, t1=9.5, key="local:9.000-9.500:10")
    app.processEvents()
    assert len(win.sentence_pairs) == 1
    assert win.sentence_pairs[-1][0] == "Neu."
