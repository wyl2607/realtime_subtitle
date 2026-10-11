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
