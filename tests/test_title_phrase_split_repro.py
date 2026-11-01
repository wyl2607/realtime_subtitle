"""复现测试：标题式短语 "Nachrichten des Tages" 被拆成两个句对。

现象：德语实时字幕里，标题式短语「Nachrichten des Tages」偶尔被拆成两个句对上屏：
先单独出「Nachrichten」（或「Nachrichten.」），过一会儿再出「des Tages ...」。

根因分析（不改源码，只写测试复现）：
- 文本切句规则本身不会拆它：`_boundary_is_real` 看到下一个词是小写 "des" 时会否决句尾（规则 ③a）。
- 拆分来自**时间放行**：识别线程在 "des" 还没被提交（可能还在 unstable 里、甚至还没识别出来）时，
  按时间把 "Nachrichten." 放行了。
- 两条放行路径：
  1. D1 样本钟放行：`_process_items` 第 2130-2139 行，当 unstable 为空且音频时间超过 SENTENCE_HOLD_SEC
  2. flush_pending：空闲冲刷

本测试构造时序：第 1 轮识别提交 "Nachrichten."、unstable 为空（"des" 还没识别出来），
样本钟已过 SENTENCE_HOLD_SEC，触发 D1 放行。期望：不应拆成两对。
"""
import os
os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"

import pytest
import numpy as np

import realtime_subtitle.config as config
import realtime_subtitle.translate.translator_queue as translator_queue
from realtime_subtitle.asr.streaming_asr import TimedText
from realtime_subtitle.translate.language_switch import DecodeHealth, LanguageVote
from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator
from threading import Lock


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
    """推进一轮识别，喂入 0.5 秒音频（16000Hz * 0.5 = 8000 样本）。"""
    t._process_items([(np.zeros(8000, dtype=np.float32), capture_time)])


def _timed(text, *spans):
    """造带词时间的提交（Whisper 的词带前导空格，对齐时空白会被忽略）。"""
    words = [(t0, t1, (" " if i else "") + w)
             for i, (w, t0, t1) in enumerate(spans)]
    return TimedText(text, words=words)


def _held_sentence(text, last_word_end, *spans):
    """造一个以句号结尾、带词时间的句子，用于测试扣留放行。"""
    words = [(t0, t1, (" " if i else "") + w)
             for i, (w, t0, t1) in enumerate(spans)]
    return TimedText(text, words=words)


@pytest.mark.xfail(strict=True, reason="标题短语 'Nachrichten des Tages' 被错误拆分：D1 样本钟放行在 'des' 识别出来前触发，导致先出 'Nachrichten.' 再出 'des Tages'")
def test_title_phrase_not_split_when_lookahead_missing(monkeypatch):
    """标题式短语 'Nachrichten des Tages' 不应被拆成两个句对。

    时序构造：
    - 第 1 轮：committed="Nachrichten."，unstable=""（"des" 还没识别出来）
      样本钟已推进到句号后 0.6 秒以上（> SENTENCE_HOLD_SEC）
    - 第 2 轮：committed=" des Tages"，unstable=""

    当前代码下：第 1 轮结束时 _held_since 被设置，unstable 为空，
    样本钟检查发现音频时间已过 SENTENCE_HOLD_SEC，触发 _release_held_boundary(force=True, lookahead="")，
    导致 "Nachrichten." 被单独放行。第 2 轮 "des Tages" 成为新的 pending_text，最终也被放行。
    结果：两个句对 ["Nachrichten.", "des Tages"]。

    期望：应作为一个整句 "Nachrichten des Tages" 放行（或至少不拆成两对）。
    """
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    monkeypatch.setattr(config, "SENTENCE_HOLD_SEC", 0.6, raising=False)

    # 第 1 轮：识别出 "Nachrichten."，unstable 为空（"des" 还没出来）
    # 词时间："Nachrichten" 结束于 2.0s，句号在 2.1s
    # 样本钟推进到 2.7s（> 2.1 + 0.6），触发样本钟放行
    round1_committed = _held_sentence("Nachrichten.", 2.1,
                                      ("Nachrichten", 1.5, 2.0),
                                      (".", 2.0, 2.1))
    round1_unstable = ""  # 关键："des" 还没识别出来

    # 第 2 轮：识别出 " des Tages"
    round2_committed = _timed(" des Tages",
                              ("des", 2.2, 2.4),
                              ("Tages", 2.4, 2.7))
    round2_unstable = ""

    t = _translator([
        (round1_committed, round1_unstable),
        (round2_committed, round2_unstable),
    ])

    # 预设样本钟：第 1 轮结束时已过 SENTENCE_HOLD_SEC
    # 句号结束 2.1s + 0.6s = 2.7s
    t._sample_clock_samples = int(2.7 * config.SAMPLE_RATE)

    _round(t, capture_time=1.0)

    # 断言：不应拆成两个句对
    # 当前代码会产生 ["Nachrichten."] 在 _tx_queue，pending_text 为空
    # 第 2 轮后会再产生 ["des Tages"]
    assert t._tx_queue == [], f"第1轮不应放行单独的句子，当前队列: {[str(s) for s in t._tx_queue]}"

    # 推进样本钟并跑第 2 轮
    t._sample_clock_samples = int(3.5 * config.SAMPLE_RATE)
    _round(t, capture_time=2.0)

    # 最终不应有两个独立句对
    all_sentences = [str(s) for s in t._tx_queue]
    assert all_sentences != ["Nachrichten.", "des Tages"], f"被错误拆分为两对: {all_sentences}"
    # 理想情况下应合并为一个句对，或者至少不拆分


@pytest.mark.xfail(strict=True, reason="标题短语 'Nachrichten des Tages' 无句号时也可能被拆分：flush_pending 或 MAX_PENDING_WORDS 路径")
def test_title_phrase_without_period_not_split(monkeypatch):
    """无句号的标题式短语 'Nachrichten des Tages' 也不应被拆分。

    时序构造：
    - 第 1 轮：committed="Nachrichten"，unstable=" des Tages"
    - 第 2 轮：committed=" des Tages"，unstable=""

    无句号时不走 held boundary 逻辑，但可能走 flush_pending 或残句兜底。
    """
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    monkeypatch.setattr(config, "IDLE_FLUSH_SEC", 2.0, raising=False)
    monkeypatch.setattr(config, "MAX_PENDING_WORDS", 24, raising=False)

    round1_committed = _timed("Nachrichten",
                              ("Nachrichten", 1.5, 2.0))
    round1_unstable = " des Tages"

    round2_committed = _timed(" des Tages",
                              ("des", 2.2, 2.4),
                              ("Tages", 2.4, 2.7))
    round2_unstable = ""

    t = _translator([
        (round1_committed, round1_unstable),
        (round2_committed, round2_unstable),
    ])

    _round(t, capture_time=1.0)

    # 第 1 轮不应放行任何句子（无句号，不触发切分）
    assert t._tx_queue == [], f"第1轮无句号不应放行，当前队列: {[str(s) for s in t._tx_queue]}"
    assert t.pending_text == "Nachrichten des Tages", f"pending_text 应包含完整短语: {t.pending_text!r}"

    # 模拟空闲 2 秒后触发 flush_pending
    import time
    original_time = time.time
    now = [100.0]
    monkeypatch.setattr(time, "time", lambda: now[0])
    t.last_audio_time = 98.0  # 2 秒前有音频

    t.flush_pending()

    all_sentences = [str(s) for s in t._tx_queue]
    # flush_pending(final=True) 会把整个 pending_text 作为一个句子送翻译
    # 但如果中间有什么逻辑导致拆分，这里会捕获
    assert all_sentences != ["Nachrichten", "des Tages"], f"flush 阶段被错误拆分: {all_sentences}"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])