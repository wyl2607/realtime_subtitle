"""SenseVoice 可选节点引擎：只用假模型，不下载权重、不联网。"""
import builtins
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from realtime_subtitle.node import engines


@pytest.fixture
def fake_funasr(monkeypatch):
    state = SimpleNamespace(results=[{"text": "Hello there. Goodbye now."}], loads=[], calls=[])

    class AutoModel:
        def __init__(self, **kwargs):
            state.loads.append(kwargs)

        def generate(self, **kwargs):
            state.calls.append(kwargs)
            return state.results

    module = ModuleType("funasr")
    module.AutoModel = AutoModel
    monkeypatch.setitem(sys.modules, "funasr", module)
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: False)))
    monkeypatch.setattr(engines.config, "ASR_ENGINE", "sensevoice")
    return state


AUDIO = np.zeros(4 * engines.SAMPLE_RATE, dtype=np.float32)


def test_tags_stripped_and_chinese_simplified(fake_funasr, monkeypatch):
    fake_funasr.results = [{"text": "<|zh|><|NEUTRAL|><|Speech|><|woitn|>這是測試。<|HAPPY|>再見。"}]
    monkeypatch.setattr(engines, "to_simplified_if_target", lambda text, lang: (
        text.replace("這是測試", "这是测试").replace("再見", "再见") if lang == "zh" else text))
    engine = engines.select_asr_engine()
    assert fake_funasr.loads == []
    got = engine.transcribe(AUDIO, "zh")
    assert [u.text for u in got] == ["这是测试。", "再见。"]
    assert engine.info()["backend"] == "funasr"
    assert engine.info()["rtf"] is not None


@pytest.mark.parametrize("language", ["de", "fr", "auto", None])
def test_unsupported_language_uses_lazy_whisper(fake_funasr, monkeypatch, language):
    whispers = []

    class Whisper:
        def __init__(self):
            self.loads = 0
            self.calls = []
            whispers.append(self)

        def load(self):
            self.loads += 1

        def transcribe(self, audio, lang):
            assert audio is AUDIO
            self.calls.append(lang)
            return [engines.Utterance(0, 1, "Fallback.")]

    monkeypatch.setattr(engines, "WhisperEngine", Whisper)
    engine = engines.select_asr_engine()
    assert whispers == []
    for _ in range(2):
        assert engine.transcribe(AUDIO, language)[0].text == "Fallback."
    assert len(whispers) == 1
    assert whispers[0].loads == 1
    assert whispers[0].calls == [language, language]
    assert fake_funasr.loads == fake_funasr.calls == []


def test_missing_funasr_falls_back_with_one_warning(monkeypatch, capsys):
    monkeypatch.setattr(engines.config, "ASR_ENGINE", "sensevoice")
    original_import = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "funasr":
            raise ImportError("private error must not be printed")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    assert isinstance(engines.select_asr_engine(), engines.WhisperEngine)
    captured = capsys.readouterr()
    assert captured.out == ""
    assert len(captured.err.splitlines()) == 1
    assert "Whisper" in captured.err
    assert "private" not in captured.err
    warnings = []
    engines.select_asr_engine(warn=warnings.append)
    assert len(warnings) == 1
    assert capsys.readouterr().err == ""


def test_default_does_not_import_funasr(monkeypatch):
    monkeypatch.setattr(engines.config, "ASR_ENGINE", "whisper")
    original_import = builtins.__import__

    def guarded(name, *args, **kwargs):
        assert not name.startswith("funasr")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded)
    assert isinstance(engines.select_asr_engine(), engines.WhisperEngine)


@pytest.mark.parametrize("language", ["zh", "en", "ja", "ko", "yue"])
def test_supported_languages_and_interpolated_times(fake_funasr, language):
    engine = engines.select_asr_engine()
    got = engine.transcribe(AUDIO, language)
    assert [u.text for u in got] == ["Hello there.", "Goodbye now."]
    assert got[0].start == 0
    assert got[0].end == pytest.approx(4 * len("Hello there.") / len("Hello there. Goodbye now."))
    assert got[0].end <= got[1].start < got[1].end == 4
    assert fake_funasr.calls[0]["language"] == language
    assert fake_funasr.calls[0]["input"] is AUDIO


@pytest.mark.parametrize("results, expected", [
    ([{"text": "ignored", "sentence_info": [
        {"text": "Hello there.", "start": -100, "end": 1000},
        {"text": "Goodbye now.", "start": 500, "end": 9000},
    ]}], [(0, 1), (1, 4)]),
    ([{"text": "Hello there.", "timestamp": [[250, 500], [500, 1250]]}], [(0.25, 1.25)]),
    ([{"text": "Hello there.", "start": float("nan"), "end": 1000}], [(0, 4)]),
    ([{"text": "Hello there.", "start": 2000, "end": 1000}], [(0, 4)]),
])
def test_fun_asr_times_are_relative_monotonic_and_clamped(fake_funasr, results, expected):
    fake_funasr.results = results
    got = engines.select_asr_engine().transcribe(AUDIO, "en")
    assert [(u.start, u.end) for u in got] == expected
    previous = 0
    for u in got:
        assert previous <= u.start <= u.end <= 4
        previous = u.end


@pytest.mark.parametrize("cuda, device", [(False, "cpu"), (True, "cuda")])
def test_load_selects_available_device_and_only_loads_once(fake_funasr, monkeypatch, cuda, device):
    monkeypatch.setitem(sys.modules, "torch", SimpleNamespace(
        cuda=SimpleNamespace(is_available=lambda: cuda)))
    engine = engines.select_asr_engine()
    engine.load()
    engine.load()
    assert fake_funasr.loads == [{"model": "iic/SenseVoiceSmall", "device": device,
                                 "trust_remote_code": False, "disable_update": True}]


def test_hallucination_tags_and_empty_audio_are_discarded(fake_funasr, monkeypatch):
    monkeypatch.setattr(engines.config, "HALLUCINATION_BLACKLIST", ["invented noise"])
    fake_funasr.results = [{"text": "<|en|>invented noise"}, {"text": "<|nospeech|><|BGM|>"}]
    engine = engines.select_asr_engine()
    assert engine.transcribe(AUDIO, "en") == []
    assert engine.transcribe(AUDIO[:0], "en") == []
    assert len(fake_funasr.calls) == 1


def test_cantonese_uses_chinese_sentence_boundaries(fake_funasr):
    fake_funasr.results = [{"text": "<|yue|>你好。再见。"}]
    got = engines.select_asr_engine().transcribe(AUDIO, "yue")
    assert [u.text for u in got] == ["你好。", "再见。"]
    assert [(u.start, u.end) for u in got] == [(0, 2), (2, 4)]
