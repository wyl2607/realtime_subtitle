from threading import Lock

import realtime_subtitle.config as config
from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator


class _NoHttpSession:
    def post(self, *args, **kwargs):
        raise AssertionError("apple 后端不应该发 Ollama HTTP 请求")


class _FakeApple:
    def __init__(self, result):
        self.result = result
        self.calls = []

    def translate(self, text, src, dst, timeout):
        self.calls.append((text, src, dst, timeout))
        return self.result


def _translator(fake_apple):
    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._translate_backend = "apple"
    t.apple_translator = fake_apple
    t.ollama_session = _NoHttpSession()
    t._tx_fail_streak = 0
    t._tx_circuit_until = 0.0
    t._ollama_hot = False
    t._tx_lock = Lock()
    return t


def test_apple_translate_single_sentence_uses_helper_not_http(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de")
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh")
    fake = _FakeApple("晚上 19:10 开始。")
    t = _translator(fake)

    out = t._translate_single_sentence("Es beginnt um 19.10 Uhr.", "")

    assert out == "晚上 19:10 开始。"
    assert fake.calls[0][0] == "Es beginnt um 19:10 Uhr."
    assert fake.calls[0][1:3] == ("de", "zh")
    assert t._tx_fail_streak == 0


def test_apple_translate_failure_returns_original_sentence(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de")
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh")
    fake = _FakeApple(None)
    t = _translator(fake)

    sentence = "Das bleibt sichtbar."
    assert t._translate_single_sentence(sentence, "") == sentence
    assert t._tx_fail_streak == 1


class _RecordingExecutor:
    def __init__(self):
        self.submitted = []

    def submit(self, fn, *args):
        self.submitted.append((fn, args))


def _draft_translator(fake_apple, backend):
    """够 _maybe_draft / _draft_worker 跑起来的最小 translator。"""
    from collections import deque

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._translate_backend = backend
    t.apple_translator = fake_apple
    t.on_draft = lambda *a: None
    t.pending_text = "Der Bundestag hat heute über den Haushalt beraten"
    t._draft_last_text = ""
    t._draft_last_time = 0.0
    t._asr_busy = True
    t._lookup_inflight = False
    t._inflight_lock = Lock()
    t._ollama_hot = True
    t._asr_lock = Lock()
    t._audio_inbox = [(b"\0" * 16000, 1.0), (b"\0" * 16000, 1.5)]
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._tx_retry_queue = []
    t._tx_epoch = 0
    t._lang_revision = 0
    t.context_history = deque(maxlen=6)
    t.closing = False
    t._tx_fail_streak = 0
    t._tx_circuit_until = 0.0
    t._stats_lock = Lock()
    t._stat_draft = 0
    t._executor = _RecordingExecutor()
    t._tx_executor = t._executor
    return t


def test_apple_draft_not_blocked_by_asr_backlog(monkeypatch):
    """Mac 上 ASR 每轮 2.4s、音频块每 0.5s 一个：收件箱必然攒着 2+ 块。

    Apple Translation 不占 MLX 的 GPU，这条让路闸门只该留给 Ollama。
    """
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True)
    t = _draft_translator(_FakeApple("联邦议院今天讨论了预算"), "apple")

    t._maybe_draft()

    assert len(t._executor.submitted) == 1


def test_ollama_draft_still_yields_to_asr_backlog(monkeypatch):
    """回归：Ollama 路径的行为不变——识别攒块时草稿让路。"""
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True)
    t = _draft_translator(_FakeApple("不该被调用"), "ollama")

    t._maybe_draft()

    assert t._executor.submitted == []


def test_apple_draft_worker_runs_while_asr_busy(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de")
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh")
    fake = _FakeApple("联邦议院今天讨论了预算")
    t = _draft_translator(fake, "apple")
    emitted = []
    t.on_draft = lambda text, rev: emitted.append(text)

    t._draft_worker(t.pending_text)

    assert fake.calls, "Apple 后端下草稿不该因 _asr_busy 被丢弃"
    assert emitted == ["联邦议院今天讨论了预算"]


def test_ollama_draft_worker_still_yields_to_asr_busy(monkeypatch):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de")
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh")
    fake = _FakeApple("不该被调用")
    t = _draft_translator(fake, "ollama")

    t._draft_worker(t.pending_text)

    assert fake.calls == []


def test_apple_draft_not_blocked_by_ollama_lookup(monkeypatch):
    """查词/分析走 Ollama，Apple 草稿在另一个进程/线程池，没有让路的理由。"""
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True)
    t = _draft_translator(_FakeApple("联邦议院今天讨论了预算"), "apple")
    t._lookup_inflight = True

    t._maybe_draft()

    assert len(t._executor.submitted) == 1


def test_ollama_draft_still_yields_to_lookup(monkeypatch):
    """回归：查词/AI 分析在飞时，Ollama 草稿仍然让路。"""
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True)
    t = _draft_translator(_FakeApple("不该被调用"), "ollama")
    t._lookup_inflight = True

    t._maybe_draft()

    assert t._executor.submitted == []
