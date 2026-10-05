import sys
import types
from threading import Lock


def _install_fake_mlx(monkeypatch):
    calls = []

    class FakeMx(types.SimpleNamespace):
        float16 = "float16"

        def eval(self, params):
            calls.append(("eval", params))

        def clear_cache(self):
            calls.append(("clear_cache",))

    class FakeNn(types.SimpleNamespace):
        def quantize(self, model, group_size, bits):
            calls.append(("quantize", model.name, group_size, bits))
            model.quantized_bits = bits

    mx = FakeMx()
    nn = FakeNn()
    mlx_pkg = types.ModuleType("mlx")
    monkeypatch.setitem(sys.modules, "mlx", mlx_pkg)
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(sys.modules, "mlx.nn", nn)
    return calls


def _install_fake_transcribe(monkeypatch):
    calls = []

    class FakeModel:
        def __init__(self, name):
            self.name = name
            self.quantized_bits = None

        def parameters(self):
            return f"params:{self.name}"

    class FakeHolder:
        model = FakeModel("initial")
        model_path = "repo"

        @classmethod
        def get_model(cls, path, dtype):
            calls.append(("get_model", path, dtype))
            return cls.model

    def load_model(path, dtype):
        # 记下载新模型那一刻旧模型是否已放掉（降档时先载后放会让峰值多一整份）
        calls.append(("load_model", path, dtype, FakeHolder.model is None))
        return FakeModel(f"loaded-{len(calls)}")

    transcribe = types.ModuleType("mlx_whisper.transcribe")
    transcribe.ModelHolder = FakeHolder
    transcribe.load_model = load_model
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", transcribe)
    return calls, FakeHolder


def test_set_bits_16_to_8_quantizes_modelholder_in_place(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    mlx_calls = _install_fake_mlx(monkeypatch)
    transcribe_calls, holder = _install_fake_transcribe(monkeypatch)
    original = holder.model
    model = MlxWhisperModel("repo")

    elapsed = model.set_bits(8)

    assert elapsed >= 0
    assert model.bits == 8
    assert holder.model is original
    assert holder.model.quantized_bits == 8
    assert ("load_model", "repo", "float16") not in transcribe_calls
    assert ("quantize", "initial", 64, 8) in mlx_calls
    assert holder.model_path == "repo"


def test_set_bits_8_to_4_reloads_then_quantizes(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    mlx_calls = _install_fake_mlx(monkeypatch)
    transcribe_calls, holder = _install_fake_transcribe(monkeypatch)
    original = holder.model
    model = MlxWhisperModel("repo")
    model.bits = 8

    model.set_bits(4)

    assert model.bits == 4
    assert holder.model is not original
    assert holder.model.quantized_bits == 4
    assert ("load_model", "repo", "float16", True) in transcribe_calls  # 先放旧的再载
    assert ("quantize", holder.model.name, 64, 4) in mlx_calls
    assert holder.model_path == "repo"


def test_set_bits_8_to_16_reloads_without_quantizing(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    mlx_calls = _install_fake_mlx(monkeypatch)
    transcribe_calls, holder = _install_fake_transcribe(monkeypatch)
    original = holder.model
    model = MlxWhisperModel("repo")
    model.bits = 8

    model.set_bits(16)

    assert model.bits == 16
    assert holder.model is not original
    assert holder.model.quantized_bits is None
    assert ("load_model", "repo", "float16", True) in transcribe_calls  # 先放旧的再载
    assert [call for call in mlx_calls if call[0] == "quantize"] == []
    assert holder.model_path == "repo"


def test_request_whisper_bits_only_registers_until_asr_batch_boundary(capsys):
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    class FakeModel:
        bits = 16

        def __init__(self):
            self.calls = []

        def set_bits(self, bits):
            self.calls.append(bits)
            self.bits = bits
            return 0.13

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._audio_inbox = []
    t._asr_backlog_n = 0
    t._asr_scheduled = True
    t._pending_lang_switch = None
    t._pending_lang_source = None
    t._pending_lang_target = None
    t._pending_whisper_bits = None
    t._whisper_bits_warned = False
    t.model = FakeModel()

    t.request_whisper_bits(8)

    assert t._pending_whisper_bits == 8
    assert t.model.calls == []

    t._process_inbox()

    assert t._pending_whisper_bits is None
    assert t.model.calls == [8]
    assert "Whisper 精度 16→8bit（0.13秒）" in capsys.readouterr().out


def test_reload_releases_old_model_before_loading_new(monkeypatch):
    from realtime_subtitle.asr.mlx_backend import MlxWhisperModel

    _install_fake_mlx(monkeypatch)
    calls, holder = _install_fake_transcribe(monkeypatch)
    model = MlxWhisperModel("repo")
    model.bits = 8

    model.set_bits(4)

    loads = [c for c in calls if c[0] == "load_model"]
    assert loads and loads[0][3] is True


def test_prequantized_start_only_steps_translation():
    import os
    os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    import realtime_subtitle.app as app
    tiers = [(16, "qwen3.5:9b"), (16, "qwen3.5:4b"), (8, "qwen3.5:4b"), (8, "qwen3.5:2b"), (4, "qwen3.5:2b")]

    assert app._auto_tiers_from_start((4, "qwen3.5:4b"), tiers) == [(4, "qwen3.5:4b"), (4, "qwen3.5:2b")]
