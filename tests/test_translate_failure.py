from collections import OrderedDict, deque
from threading import Lock
from types import SimpleNamespace
import json
import pytest
import requests

import realtime_subtitle.config as config
from realtime_subtitle.translate.translator_queue import (
    TimedSentence,
    WhisperQueueTranslator,
)


def _make_test_translator():
    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._tx_lock = Lock()
    t._stats_lock = Lock()
    t._tx_queue = []
    t._tx_retry_queue = []
    t._failed_sentences = OrderedDict()
    t._tx_inflight = []
    t._tx_epoch = 0
    t._lang_revision = 0
    t.closing = False
    t.context_history = deque(maxlen=6)
    t._stat_dict = 0
    t._stat_tx = []
    t._stat_draft = 0
    t._draft_last_text = ""
    t.pending_text = ""
    t._last_unstable = ""
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t._tx_slow_until = 0.0
    t._ollama_hot = True
    t._sentence_seq = 0
    t._sample_clock_samples = 0
    t._held_since = 0.0
    t._release_pending = False
    t._stat_held_misfire = 0
    t._asr_busy = False
    t._asr_backlog_n = 0
    t._stat_note_tx = lambda elapsed: None
    t.on_draft = None
    t.on_display = None
    t.on_pair = None
    t.on_translate_failed = None
    t.on_status = None
    t._save_transcript = lambda g, zh: None
    t._tx_executor = SimpleNamespace(submit=lambda fn, *a, **k: None)
    return t


def test_timeout_retry_success_emits_pair_once_no_failed():
    """超时→自动重试成功：只发一次 on_pair，不发 on_translate_failed。"""
    t = _make_test_translator()
    pairs = []
    failed_events = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t.on_translate_failed = lambda src, t0, t1, key, rev, reason: failed_events.append(
        (src, t0, t1, key, rev, reason)
    )

    sent = TimedSentence("Guten Morgen", t0=0.0, t1=1.5, key="k1", revision=0)
    t._tx_queue.append(sent)

    call_count = 0

    def mock_translate(sentence, context, on_partial=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            raise requests.ReadTimeout("Ollama timeout")
        return "早上好"

    t._translate_single_sentence = mock_translate

    # 第 1 次 worker 执行：初试超时 -> 立即发 on_pair 让原文先上屏，缓冲到 _tx_retry_queue，不 emit failed
    t._translation_worker()
    assert len(t._tx_queue) == 0
    assert len(t._tx_retry_queue) == 1
    assert pairs == [("Guten Morgen", "", "k1", 0)]
    assert failed_events == []
    assert t._tx_retry_queue[0].key == "k1"

    # 第 2 次 worker 执行：从重试队列取 -> 成功 -> 按同一 key 再次 emit on_pair，不 emit failed
    t._translation_worker()
    assert len(t._tx_queue) == 0
    assert len(t._tx_retry_queue) == 0
    assert len(pairs) == 2
    assert pairs[0] == ("Guten Morgen", "", "k1", 0)
    assert pairs[1] == ("Guten Morgen", "早上好", "k1", 0)
    assert failed_events == []


def test_timeout_retry_failure_emits_failed_timeout_once():
    """超时→自动重试仍失败：首试发 on_pair('', ...)，重试失败发 on_translate_failed(reason='timeout')。"""
    t = _make_test_translator()
    pairs = []
    failed_events = []
    saved_transcripts = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t.on_translate_failed = lambda src, t0, t1, key, rev, reason: failed_events.append(
        (src, t0, t1, key, rev, reason)
    )
    t._save_transcript = lambda src, tgt: saved_transcripts.append((src, tgt))

    sent = TimedSentence("Hallo Welt", t0=1.0, t1=2.0, key="kw1", revision=1)
    t._tx_queue.append(sent)

    def mock_translate(sentence, context, on_partial=None):
        raise requests.ReadTimeout("Still timeout")

    t._translate_single_sentence = mock_translate

    # 第 1 次 worker 执行：初试超时 -> 立即发 on_pair('', ...)，进重试队列
    t._translation_worker()
    assert len(t._tx_retry_queue) == 1
    assert pairs == [("Hallo Welt", "", "kw1", 1)]
    assert failed_events == []

    # 第 2 次 worker 执行：重试仍超时 -> 终态失败
    t._translation_worker()
    assert len(t._tx_retry_queue) == 0
    assert len(failed_events) == 1
    assert failed_events[0] == ("Hallo Welt", 1.0, 2.0, "kw1", 1, "timeout")
    assert len(pairs) == 1
    assert pairs[0] == ("Hallo Welt", "", "kw1", 1)
    assert saved_transcripts == [("Hallo Welt", "")]


def test_error_retry_success_emits_pair_once_no_failed():
    """普通错误→自动重试成功：首试发 on_pair('', ...)，重试成功按同一 key 发译文。"""
    t = _make_test_translator()
    pairs = []
    failed_events = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t.on_translate_failed = lambda src, t0, t1, key, rev, reason: failed_events.append(
        (src, t0, t1, key, rev, reason)
    )

    sent = TimedSentence("Wie geht es dir?", t0=2.0, t1=3.5, key="ke1", revision=0)
    t._tx_queue.append(sent)

    call_count = 0

    def mock_translate(sentence, context, on_partial=None):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            t._last_tx_failure_reason = "error"
            return sentence  # 失败返回原文
        return "你好吗？"

    t._translate_single_sentence = mock_translate

    t._translation_worker()
    assert len(t._tx_retry_queue) == 1
    assert pairs == [("Wie geht es dir?", "", "ke1", 0)]
    assert failed_events == []

    t._translation_worker()
    assert len(t._tx_retry_queue) == 0
    assert len(pairs) == 2
    assert pairs[0] == ("Wie geht es dir?", "", "ke1", 0)
    assert pairs[1] == ("Wie geht es dir?", "你好吗？", "ke1", 0)
    assert failed_events == []


def test_error_retry_failure_emits_failed_error_once():
    """普通错误→自动重试仍失败：首试发 on_pair('', ...)，终态发一次 on_translate_failed(reason='error')。"""
    t = _make_test_translator()
    pairs = []
    failed_events = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t.on_translate_failed = lambda src, t0, t1, key, rev, reason: failed_events.append(
        (src, t0, t1, key, rev, reason)
    )

    sent = TimedSentence("Schwerer Fehler", t0=0.5, t1=1.8, key="ke2", revision=2)
    t._tx_queue.append(sent)

    def mock_translate(sentence, context, on_partial=None):
        t._last_tx_failure_reason = "error"
        return sentence

    t._translate_single_sentence = mock_translate

    t._translation_worker()
    assert len(t._tx_retry_queue) == 1
    assert pairs == [("Schwerer Fehler", "", "ke2", 2)]
    assert failed_events == []

    t._translation_worker()
    assert len(t._tx_retry_queue) == 0
    assert len(failed_events) == 1
    assert failed_events[0] == ("Schwerer Fehler", 0.5, 1.8, "ke2", 2, "error")
    assert pairs == [("Schwerer Fehler", "", "ke2", 2)]


def test_queue_backlog_dropped_emits_failed_dropped_no_retry(monkeypatch):
    """队列积压丢弃：锁外发 on_translate_failed(reason='dropped')，回调中不得持有 _tx_lock。"""
    monkeypatch.setattr(config, "TRANSLATE_QUEUE_MAX_CHARS", 30)
    t = _make_test_translator()
    failed_events = []
    lock_states = []

    def on_failed(src, t0, t1, key, rev, reason):
        # 回调被调用时，_tx_lock 必须未被持有
        lock_states.append(t._tx_lock.locked())
        failed_events.append((src, t0, t1, key, rev, reason))

    t.on_translate_failed = on_failed

    # 塞入超过 30 字符的句子，通过 _enqueue_sentences 触发丢弃
    s1 = TimedSentence("12345678901234567890", t0=0.0, t1=1.0, key="d1", revision=0)
    s2 = TimedSentence("abcdefghijklmnopqrst", t0=1.0, t1=2.0, key="d2", revision=0)
    s3 = TimedSentence("restliche Zeichen hier", t0=2.0, t1=3.0, key="d3", revision=0)

    t._enqueue_sentences([s1, s2, s3])

    assert len(t._tx_queue) == 1
    assert t._tx_queue[0].key == "d3"
    # 丢弃的句子不应进重试队列
    assert len(t._tx_retry_queue) == 0

    assert len(failed_events) == 2
    assert failed_events[0] == ("12345678901234567890", 0.0, 1.0, "d1", 0, "dropped")
    assert failed_events[1] == ("abcdefghijklmnopqrst", 1.0, 2.0, "d2", 0, "dropped")
    # 严格保证回调执行时未持锁
    assert lock_states == [False, False]

    # 丢弃的句子保留在 _failed_sentences 中供手动重试
    assert "d1" in t._failed_sentences
    assert "d2" in t._failed_sentences


def test_retry_translation_success_and_unknown_key():
    """retry_translation: 未知 key 返回 False；已知失败 key 重新入主队列尾部返回 True。"""
    t = _make_test_translator()
    submitted = []
    t._tx_executor = SimpleNamespace(submit=lambda fn, *a, **k: submitted.append(fn))

    # 未知 key
    assert t.retry_translation("nonexistent") is False
    assert len(t._tx_queue) == 0

    # 存入失败句
    t._record_failed_sentence("Das ist wichtig", 5.0, 6.0, "k_manual", 3)
    assert "k_manual" in t._failed_sentences

    # 手动重试
    ok = t.retry_translation("k_manual")
    assert ok is True
    # 从失败缓冲移除并入主队列
    assert "k_manual" not in t._failed_sentences
    assert len(t._tx_queue) == 1
    queued = t._tx_queue[0]
    assert str(queued) == "Das ist wichtig"
    assert queued.t0 == 5.0
    assert queued.t1 == 6.0
    assert queued.key == "k_manual"
    assert queued.revision == 3
    assert len(submitted) == 1


def test_language_switch_clears_retry_queue_and_stale_revision():
    """语言切换清空重试队列，旧代数在飞任务作废。"""
    t = _make_test_translator()
    t._tx_retry_queue = [
        TimedSentence("Alte Sprache", t0=0.0, t1=1.0, key="old_k", revision=0)
    ]
    t._tx_queue = [
        TimedSentence("Noch ein alter Satz", t0=1.0, t1=2.0, key="old_k2", revision=0)
    ]

    t.clear_context()
    assert len(t._tx_retry_queue) == 0
    assert len(t._tx_queue) == 0

    # 测试代数作废机制：在飞 worker 拿到旧 epoch 后发生切语言
    pairs = []
    failed_events = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t.on_translate_failed = lambda src, t0, t1, key, rev, reason: failed_events.append(
        (src, t0, t1, key, rev, reason)
    )
    t._tx_queue = [
        TimedSentence("Inflight Satz", t0=2.0, t1=3.0, key="inf_k", revision=0)
    ]

    def mock_delayed_translate(sentence, context, on_partial=None):
        # 模拟翻译期间切了语言（epoch 增加）
        t._tx_epoch += 1
        return "旧翻译"

    t._translate_single_sentence = mock_delayed_translate
    t._translation_worker()

    # 因为 epoch 改变，旧翻译不上屏、不入库
    assert pairs == []
    assert failed_events == []


def test_unregistered_callback_preserves_legacy_behavior():
    """未设置 on_translate_failed 回调时，保持旧行为：不进重试队列，立即发 on_pair 与存档。"""
    t = _make_test_translator()
    assert t.on_translate_failed is None

    pairs = []
    saved = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t._save_transcript = lambda src, tgt: saved.append((src, tgt))

    sent = TimedSentence("Legacy Test Satz", t0=1.0, t1=2.0, key="leg1", revision=0)
    t._tx_queue.append(sent)

    def mock_fail(sentence, context, on_partial=None):
        raise requests.ReadTimeout("Timeout")

    t._translate_single_sentence = mock_fail
    t._translation_worker()

    # 没有进入重试队列
    assert len(t._tx_retry_queue) == 0
    # 立即发 on_pair("", ...) 并存档
    assert pairs == [("Legacy Test Satz", "", "leg1", 0)]
    assert saved == [("Legacy Test Satz", "")]
    # 仍记录到 _failed_sentences 供 retry_translation
    assert "leg1" in t._failed_sentences


def test_retry_queue_low_priority_runs_only_when_main_queue_idle():
    """重试队列是低优先级：主队列有内容时绝不消费重试队列，主队列空闲时才取。"""
    t = _make_test_translator()
    pairs = []
    t.on_pair = lambda src, tgt, t0, t1, key, rev: pairs.append((src, tgt, key, rev))
    t.on_translate_failed = lambda *a, **k: None

    t._tx_queue.append(TimedSentence("Main Satz 1", t0=1.0, t1=2.0, key="m1", revision=0))
    t._tx_retry_queue.append(TimedSentence("Retry Satz 1", t0=0.0, t1=1.0, key="r1", revision=0))

    translated = []

    def mock_translate(sentence, context, on_partial=None):
        translated.append(sentence)
        return f"翻_{sentence}"

    t._translate_single_sentence = mock_translate

    # 第 1 轮：应该消费主队列，留下重试队列
    t._translation_worker()
    assert translated == ["Main Satz 1"]
    assert len(t._tx_queue) == 0
    assert len(t._tx_retry_queue) == 1

    # 第 2 轮：主队列已空，消费重试队列
    t._translation_worker()
    assert translated == ["Main Satz 1", "Retry Satz 1"]
    assert len(t._tx_queue) == 0
    assert len(t._tx_retry_queue) == 0


def test_failed_sentences_ring_buffer_caps_at_50():
    """最近失败句缓存容量上限 N=50，超过时丢弃最旧的。"""
    t = _make_test_translator()
    for i in range(60):
        t._record_failed_sentence(f"sentence_{i}", float(i), float(i + 1), f"k_{i}", 0)

    assert len(t._failed_sentences) == 50
    # 前 10 条已被移出
    for i in range(10):
        assert f"k_{i}" not in t._failed_sentences
        assert t.retry_translation(f"k_{i}") is False
    # 后 50 条保留
    for i in range(10, 60):
        assert f"k_{i}" in t._failed_sentences


def test_retry_uses_translate_single_sentence_and_timeout(monkeypatch):
    """重试必须走 _translate_single_sentence，共用 _translate_timeout 与 num_ctx（第 21、25、29 条）。"""
    t = _make_test_translator()
    t.on_translate_failed = lambda *a, **k: None

    called_with = []
    recorded_timeouts = []

    sent = TimedSentence("Zu pruefender Satz", t0=1.0, t1=2.0, key="r_rule", revision=0)
    t._tx_retry_queue.append(sent)

    def spy_translate(sentence, context, on_partial=None):
        called_with.append((sentence, context))
        recorded_timeouts.append(t._translate_timeout())
        return "规则测试通过"

    t._translate_single_sentence = spy_translate

    t._translation_worker()
    assert len(called_with) == 1
    assert called_with[0][0] == "Zu pruefender Satz"
    assert len(recorded_timeouts) == 1
    assert recorded_timeouts[0] == t._translate_timeout()


def test_translate_single_sentence_failure_reasons_and_payload(monkeypatch):
    """验证 _translate_single_sentence 内部超时/错误识别及 Ollama 参数（num_ctx 等）。"""
    t = _make_test_translator()
    t.ollama_session = SimpleNamespace()
    t._circuit_open = lambda: False
    t._ollama_identity_ok = lambda: True
    t._await_model_ready = lambda: None
    t._note_tx_result = lambda ok, hard=False: None

    captured_payloads = []

    # 1. ReadTimeout -> reason="timeout", payload contains num_ctx
    def mock_post_timeout(*args, **kwargs):
        captured_payloads.append(kwargs.get("json", {}))
        raise requests.ReadTimeout("Mock timeout")

    t.ollama_session.post = mock_post_timeout
    res = t._translate_single_sentence("Ein Test", "")
    assert res == "Ein Test"
    assert t._last_tx_failure_reason == "timeout"
    assert len(captured_payloads) >= 1
    assert captured_payloads[0]["options"]["num_ctx"] == getattr(config, "OLLAMA_NUM_CTX", 4096)

    # 2. HTTP 500 -> reason="error"
    mock_resp_500 = SimpleNamespace(
        status_code=500,
        close=lambda: None,
    )
    t.ollama_session.post = lambda *args, **kwargs: mock_resp_500
    res = t._translate_single_sentence("Ein Test", "")
    assert res == "Ein Test"
    assert t._last_tx_failure_reason == "error"

    # 3. HTTP 200 with empty body -> reason="error"
    mock_resp_empty = SimpleNamespace(
        status_code=200,
        iter_lines=lambda: [b'{"response": "", "done": true}'],
        close=lambda: None,
    )
    t.ollama_session.post = lambda *args, **kwargs: mock_resp_empty
    res = t._translate_single_sentence("Ein Test", "")
    assert t._last_tx_failure_reason == "error"

    # 4. 熔断生效 -> reason="error"
    t._circuit_open = lambda: True
    res = t._translate_single_sentence("Ein Test", "")
    assert res == "Ein Test"
    assert t._last_tx_failure_reason == "error"


def _make_stream_translator(frames):
    t = _make_test_translator()
    results = []
    response = SimpleNamespace(status_code=200, closed=False)

    def iter_lines():
        for frame in frames:
            if isinstance(frame, Exception):
                raise frame
            yield json.dumps(frame).encode()

    response.iter_lines = iter_lines
    response.close = lambda: setattr(response, "closed", True)
    t.ollama_session = SimpleNamespace(post=lambda *a, **kw: response)
    t._circuit_open = lambda: False
    t._ollama_identity_ok = lambda: True
    t._await_model_ready = lambda: None
    t._note_tx_result = lambda ok, hard=False: results.append(ok)
    t._ollama_down_notified = 0.0
    return t, response, results


@pytest.mark.parametrize("tail", [
    [{"error": "generation failed"}],
    [{"error": "", "done": True}],
    [],
    [{"done": False}],
    [requests.ConnectionError("stream disconnected")],
    [{"done": True, "done_reason": "length"}],
], ids=["error", "empty-error", "eof", "done-false", "disconnect", "length"])
@pytest.mark.parametrize("queue", ["initial", "retry", "no-callback"])
def test_stream_failure_never_succeeds_or_enters_context(tail, queue):
    """硬约束 HC-T01：部分文字不能掩盖错误帧、未完成断流或 length 截断。"""
    t, response, results = _make_stream_translator([
        {"response": "尚未完成的译文"}, *tail,
    ])
    t.context_history.append("此前已成功的原文。")
    pairs = []
    t.on_pair = lambda src, tgt, *a: pairs.append((src, tgt))
    if queue != "no-callback":
        t.on_translate_failed = lambda *a: None
    sentence = TimedSentence("Dieser Satz muss fertig werden.", t0=1, t1=2,
                             key="hc-t01", revision=0)
    (t._tx_retry_queue if queue == "retry" else t._tx_queue).append(sentence)

    t._translation_worker()

    why = ("违反硬约束 HC-T01：错误、断流或 length 截断不是完整译文；"
           "必须记录失败原因及 ok=False、仅显示原文，并禁止写入上下文。")
    assert results == [False], why
    assert t._last_tx_failure_reason == "error", why
    assert list(t.context_history) == ["此前已成功的原文。"], why
    assert all(not tgt for _, tgt in pairs), why
    assert response.closed, why


def test_stream_completed_translation_still_succeeds():
    """硬约束 HC-T01：正常 done:true 的完整翻译必须仍可成功并写入上下文。"""
    t, response, results = _make_stream_translator([
        {"response": "完整"}, {"response": "译文", "done": True, "done_reason": "stop"},
    ])
    t._tx_queue.append(TimedSentence("Ein vollstaendiger Satz.", key="complete"))
    pairs = []
    t.on_pair = lambda src, tgt, *a: pairs.append(tgt)

    t._translation_worker()

    why = "违反硬约束 HC-T01：正常完成帧不能被误判失败；必须保留完整译文和上下文。"
    assert results == [True] and pairs == ["完整译文"], why
    assert list(t.context_history) == ["Ein vollstaendiger Satz."], why
    assert response.closed, why


def test_stream_size_fuse_still_accepts_partial_translation(monkeypatch):
    """硬约束 HC-T01：主动超长截断是有意的，不能当作未收到 done 的失败。"""
    import realtime_subtitle.translate.translator_queue as tq

    monkeypatch.setattr(tq, "_MAX_STREAM_CHARS", 4)
    t, response, results = _make_stream_translator([
        {"response": "超长但保留的译文"}, {"error": "不应读到此帧"},
    ])

    out = t._translate_single_sentence("Dieser Satz ist lang.", "")

    why = "违反硬约束 HC-T01：超长保险丝必须继续保留已收文本；不得改成普通断流失败。"
    assert results == [True] and out == "超长但保留的译文", why
    assert response.closed, why


def test_stream_closing_is_neither_success_nor_failure():
    """硬约束 HC-T01：退出时中止流式读取，不得计入翻译成败或上下文。"""
    t, response, results = _make_stream_translator([])

    def closing_stream():
        yield b'{"response": "partial"}'
        t.closing = True
        yield b'{"done": true}'

    response.iter_lines = closing_stream
    t._tx_queue.append(TimedSentence("Beim Beenden abbrechen.", key="closing"))
    t._translation_worker()

    why = "违反硬约束 HC-T01：主动退出不是请求结果；必须关闭连接且不记成功、失败或上下文。"
    assert results == [] and t._last_tx_failure_reason is None, why
    assert not t.context_history, why
    assert response.closed, why


@pytest.mark.parametrize("cap", [30, 1], ids=["normal-budget", "one-oversized-sentence"])
def test_retry_queue_is_bounded_and_reports_oldest_drops(monkeypatch, cap):
    """硬约束 HC-T02：持续失败时重试队列必须有界，最旧句必须进入终态失败。"""
    monkeypatch.setattr(config, "TRANSLATE_QUEUE_MAX_CHARS", cap)
    t, _, _ = _make_stream_translator([{"error": "Ollama unavailable"}])
    failed = []

    def on_failed(*event):
        assert not t._tx_lock.locked(), (
            "违反硬约束 HC-T02：锁内失败回调可能死锁；必须放锁后通知丢弃。")
        failed.append(event)

    t.on_translate_failed = on_failed
    sentences = [TimedSentence(f"Neuer Satz {i:02d}.", t0=i, t1=i + 1,
                               key=f"retry-{i}", revision=0) for i in range(8)]
    keep = 2 if cap == 30 else 1
    why = ("违反硬约束 HC-T02：持续失败不能无限积累重试；必须沿用字符上限、"
           "丢最旧句并记录可手动重试的失败句，保留一条超长句以免饥饿。")
    for sentence in sentences:
        t._tx_queue.append(sentence)
        t._translation_worker()
        assert len(t._tx_retry_queue) <= keep, why

    assert [s.key for s in t._tx_retry_queue] == [s.key for s in sentences[-keep:]], why
    expected = [(str(s), s.t0, s.t1, s.key, s.revision) for s in sentences[:-keep]]
    assert failed == [(*item, "dropped") for item in expected], why
    assert list(t._failed_sentences.values()) == expected, why
    assert t._tx_dropped == len(expected), why
    assert t.retry_translation(sentences[0].key), why
    assert t._tx_queue[-1].key == sentences[0].key, why
