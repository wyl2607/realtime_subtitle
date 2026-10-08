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
