import sys
import types

import numpy as np

import realtime_subtitle.config as config


def _fake_mlx(monkeypatch, calls):
    module = types.ModuleType("mlx_whisper")

    def transcribe(audio, **kwargs):
        calls.append((audio, kwargs))
        return {
            "language": "de",
            "duration": len(audio) / 16000,
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "Hallo Welt",
                    "no_speech_prob": 0.12,
                    "avg_logprob": -0.34,
                    "words": [
                        {"start": 0.0, "end": 0.4, "word": " Hallo", "probability": 0.9},
                        {"start": 0.4, "end": 1.0, "word": " Welt"},
                    ],
                }
            ],
        }

    module.transcribe = transcribe
    monkeypatch.setitem(sys.modules, "mlx_whisper", module)


def test_transcribe_converts_dicts_to_faster_whisper_like_objects(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    calls = []
    _fake_mlx(monkeypatch, calls)
    model = MlxWhisperModel("repo")

    segments, info = model.transcribe(
        np.ones(16000, dtype=np.float32),
        language="de",
        task="transcribe",
        initial_prompt="Kontext",
        beam_size=5,
        word_timestamps=True,
        condition_on_previous_text=True,
        vad_filter=False,
    )

    segment = list(segments)[0]
    assert info.language == "de"
    assert segment.text == "Hallo Welt"
    assert segment.start == 0.0
    assert segment.end == 1.0
    assert segment.no_speech_prob == 0.12
    assert segment.avg_logprob == -0.34
    assert [w.word for w in segment.words] == [" Hallo", " Welt"]
    assert segment.words[0].probability == 0.9
    assert calls[0][1]["path_or_hf_repo"] == "repo"
    assert calls[0][1]["initial_prompt"] == "Kontext"


def test_beam_size_is_accepted_and_ignored(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    calls = []
    _fake_mlx(monkeypatch, calls)

    list(MlxWhisperModel("repo").transcribe(
        np.ones(16000, dtype=np.float32),
        beam_size=99,
        vad_filter=False,
    )[0])

    assert "beam_size" not in calls[0][1]


def test_vad_maps_timestamps_back_to_original_axis(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    calls = []
    _fake_mlx(monkeypatch, calls)

    chunks = [{"start": 32000, "end": 48000}]
    monkeypatch.setattr(
        "faster_whisper.vad.get_speech_timestamps",
        lambda audio, sampling_rate=16000: chunks,
    )

    audio = np.arange(48000, dtype=np.float32)
    segments, _info = MlxWhisperModel("repo").transcribe(audio, vad_filter=True)

    segment = list(segments)[0]
    assert len(calls[0][0]) == 16000
    assert np.array_equal(calls[0][0], audio[32000:48000])
    assert segment.start == 2.0
    assert segment.end == 3.0
    assert segment.words[0].start == 2.0
    assert segment.words[0].end == 2.4


def test_all_silence_returns_empty_segments_without_calling_mlx(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    calls = []
    _fake_mlx(monkeypatch, calls)
    monkeypatch.setattr(
        "faster_whisper.vad.get_speech_timestamps",
        lambda audio, sampling_rate=16000: [],
    )

    segments, info = MlxWhisperModel("repo").transcribe(
        np.zeros(16000, dtype=np.float32),
        language="de",
        vad_filter=True,
    )

    assert list(segments) == []
    assert info.language == "de"
    assert calls == []


def test_detect_language_shares_transcribe_model_and_reads_prob_table(monkeypatch):
    import pytest
    mx = pytest.importorskip("mlx.core")  # Windows CI 没有 mlx
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    seen = {}
    audio_module = types.ModuleType("mlx_whisper.audio")
    audio_module.N_FRAMES = 3000
    audio_module.N_SAMPLES = 480000
    audio_module.log_mel_spectrogram = lambda audio, n_mels, padding: mx.ones((10, n_mels))
    audio_module.pad_or_trim = lambda mel, frames, axis=-2: mel

    class FakeModel:
        dims = types.SimpleNamespace(n_mels=128)

        def detect_language(self, mel):
            seen["mel"] = (mel.shape, mel.dtype)
            # 真实 mlx_whisper 的形状：(语言 token 的 mx.array, {语言码: 概率})
            return mx.array(50261), {"de": 0.7, "en": 0.3}

    class FakeHolder:
        @classmethod
        def get_model(cls, path, dtype):
            seen["holder"] = path
            return FakeModel()

    transcribe_module = types.ModuleType("mlx_whisper.transcribe")
    transcribe_module.ModelHolder = FakeHolder
    monkeypatch.setitem(sys.modules, "mlx_whisper.audio", audio_module)
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", transcribe_module)

    lang, prob, all_probs = MlxWhisperModel("repo").detect_language(
        np.ones(16000, dtype=np.float32), vad_filter=False)

    assert (lang, prob) == ("de", 0.7)
    assert all_probs == {"de": 0.7, "en": 0.3}
    assert seen["holder"] == "repo"  # 和 transcribe 同一份模型，不另加载
    assert seen["mel"] == ((10, 128), mx.float16)


def test_words_without_leading_space_stay_as_is(monkeypatch):
    """中文等无空格语言：别给每个词补空格，否则拼出来的句子全是空格。"""
    from realtime_subtitle.asr.mlx_backend import _word_from_dict

    assert _word_from_dict({"start": 0, "end": 1, "word": "你好"}, None).word == "你好"


def test_create_whisper_model_auto_uses_mlx_on_apple_silicon(monkeypatch):
    from realtime_subtitle.asr import backends

    monkeypatch.setattr(config, "WHISPER_BACKEND", "auto", raising=False)
    monkeypatch.setattr(config, "WHISPER_MLX_REPO", "mlx-repo", raising=False)
    monkeypatch.setattr(backends.sys, "platform", "darwin")
    monkeypatch.setattr(backends.platform, "machine", lambda: "arm64")

    model = backends.create_whisper_model()

    assert model.repo == "mlx-repo"


def test_create_whisper_model_auto_keeps_windows_on_faster_whisper(monkeypatch):
    from realtime_subtitle.asr import backends
    from realtime_subtitle.translate import translator_queue

    calls = []

    class FakeWhisper:
        def __init__(self, *args, **kwargs):
            calls.append((args, kwargs))

    monkeypatch.setattr(config, "WHISPER_BACKEND", "auto", raising=False)
    monkeypatch.setattr(backends.sys, "platform", "win32")
    monkeypatch.setattr(backends.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(translator_queue, "_ensure_ml_deps", lambda: FakeWhisper)

    model = backends.create_whisper_model()

    assert isinstance(model, FakeWhisper)
    assert calls[0][0] == (config.WHISPER_MODEL,)
    assert calls[0][1]["local_files_only"] is True


def test_create_whisper_model_explicit_backend_wins(monkeypatch):
    from realtime_subtitle.asr import backends
    from realtime_subtitle.translate import translator_queue

    class FakeWhisper:
        def __init__(self, *args, **kwargs):
            pass

    monkeypatch.setattr(config, "WHISPER_BACKEND", "faster-whisper", raising=False)
    monkeypatch.setattr(backends.sys, "platform", "darwin")
    monkeypatch.setattr(backends.platform, "machine", lambda: "arm64")
    monkeypatch.setattr(translator_queue, "_ensure_ml_deps", lambda: FakeWhisper)

    assert isinstance(backends.create_whisper_model(), FakeWhisper)
