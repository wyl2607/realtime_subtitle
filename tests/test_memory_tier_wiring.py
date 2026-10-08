import os

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"

import realtime_subtitle.app as main  # noqa: E402
import realtime_subtitle.config as config  # noqa: E402


class _FakeWindow:
    def __init__(self):
        self.statuses = []
        self.modes = []

    def show_status(self, msg):
        self.statuses.append(msg)

    def notify_mode_applied(self, name):
        self.modes.append(name)


class _FakeTranslator:
    def __init__(self):
        self.calls = []
        self.bits_calls = []
        self._translate_backend = "ollama"

    def request_warm_model(self, old_model=None, new_model=None):
        self.calls.append((old_model, new_model))

    def request_whisper_bits(self, bits):
        self.bits_calls.append(bits)


def _make_app(baseline="qwen3.5:4b", current_mode="直播"):
    app = main.SubtitleApp.__new__(main.SubtitleApp)
    app.subtitle_window = _FakeWindow()
    app.translator = _FakeTranslator()
    app._baseline_ollama_model = baseline
    app._baseline_whisper_bits = 16
    app._memory_tier_by_name = {}
    app._current_mode = current_mode
    app._mode_before_perf = None
    app._memory_tier_error_printed = False
    return app


def test_on_memory_tier_switches_model_and_warms_in_normal_mode(monkeypatch):
    monkeypatch.setattr(config, "AUTO_TIERS", [(16, "qwen3.5:9b"), (16, "qwen3.5:4b"), (8, "qwen3.5:4b"), (8, "qwen3.5:2b"), (4, "qwen3.5:2b")])
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5:4b")
    app = _make_app(baseline="qwen3.5:4b", current_mode="直播")
    app._memory_tier_by_name = {"8bit+qwen3.5:2b": (8, "qwen3.5:2b")}

    app._on_memory_tier("8bit+qwen3.5:2b")

    assert app._baseline_ollama_model == "qwen3.5:2b"
    assert app._baseline_whisper_bits == 8
    assert config.OLLAMA_MODEL == "qwen3.5:2b"
    assert app.translator.calls == [("qwen3.5:4b", "qwen3.5:2b")]
    assert app.translator.bits_calls == [8]
    assert app.subtitle_window.statuses == [
        "🧠 内存紧张：识别精度降到 8bit（准确度不变），翻译模型降到 qwen3.5:2b"
    ]


def test_on_memory_tier_in_perf_mode_only_updates_baseline(monkeypatch):
    monkeypatch.setattr(config, "AUTO_TIERS", [(16, "qwen3.5:9b"), (16, "qwen3.5:4b"), (8, "qwen3.5:4b"), (8, "qwen3.5:2b"), (4, "qwen3.5:2b")])
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5:4b")
    app = _make_app(baseline="qwen3.5:4b", current_mode="性能")
    app._memory_tier_by_name = {"8bit+qwen3.5:2b": (8, "qwen3.5:2b")}

    app._on_memory_tier("8bit+qwen3.5:2b")

    assert app._baseline_ollama_model == "qwen3.5:2b"
    assert app._baseline_whisper_bits == 8
    assert config.OLLAMA_MODEL == "qwen3.5:4b"
    assert app.translator.calls == []
    assert app.translator.bits_calls == [8]
    assert app.subtitle_window.statuses == [
        "🧠 内存紧张：识别精度降到 8bit（准确度不变），翻译模型降到 qwen3.5:2b"
    ]


def test_leaving_perf_mode_uses_memory_adjusted_baseline(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5:4b")
    monkeypatch.setattr(config, "GAME_MODE_OLLAMA_MODEL", "qwen3.5:4b", raising=False)
    monkeypatch.setattr(config, "PRESETS", {"性能": {}, "直播": {"TRANSLATION_STYLE": "balanced"}})
    app = _make_app(baseline="qwen3.5:2b", current_mode="性能")

    assert app._apply_mode("直播") is True

    assert config.OLLAMA_MODEL == "qwen3.5:2b"
    assert app.translator.calls == [("qwen3.5:4b", "qwen3.5:2b")]
    assert app._current_mode == "直播"


def test_auto_tiers_are_sliced_from_startup_tuple():
    tiers = [(16, "qwen3.5:9b"), (16, "qwen3.5:4b"), (8, "qwen3.5:4b"), (8, "qwen3.5:2b"), (4, "qwen3.5:2b")]

    assert main._auto_tiers_from_start((16, "qwen3.5:4b"), tiers) == [
        (16, "qwen3.5:4b"),
        (8, "qwen3.5:4b"),
        (8, "qwen3.5:2b"),
        (4, "qwen3.5:2b"),
    ]
    assert main._auto_tiers_from_start((16, "qwen3.5:9b"), tiers, (16, "qwen3.5:4b")) == [
        (16, "qwen3.5:4b"),
        (8, "qwen3.5:4b"),
        (8, "qwen3.5:2b"),
        (4, "qwen3.5:2b"),
    ]
    assert main._auto_tiers_from_start((16, "custom:model"), tiers) == []
    assert main._auto_tiers_from_start((16, "qwen3.5:4b"), tiers, mlx_backend=False) == [(16, "qwen3.5:4b")]


def test_build_memory_governor_disabled_when_startup_model_not_in_tiers(monkeypatch, capsys):
    monkeypatch.setattr(config, "AUTO_TIER_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_TIERS", [(16, "qwen3.5:9b"), (16, "qwen3.5:4b")])
    monkeypatch.setattr(config, "AUTO_TIER_MAX", None)
    app = _make_app(baseline="custom:model")

    assert app._build_memory_governor() is None
    assert "(16, 'custom:model') 不在自动分档列表" in capsys.readouterr().out


def test_build_memory_governor_apple_backend_uses_whisper_bits_only(monkeypatch):
    import realtime_subtitle.asr.backends as backends

    monkeypatch.setattr(backends, "selected_whisper_backend", lambda: "mlx")
    monkeypatch.setattr(config, "AUTO_TIER_ENABLED", True)
    monkeypatch.setattr(config, "AUTO_TIERS", [(16, "qwen3.5:9b"), (16, "qwen3.5:4b"), (8, "qwen3.5:4b"), (8, "qwen3.5:2b"), (4, "qwen3.5:2b")])
    monkeypatch.setattr(config, "AUTO_TIER_MAX", None)
    app = _make_app(baseline="qwen3.5:4b")
    app.translator._translate_backend = "apple"

    governor = app._build_memory_governor()

    assert governor.tiers == ["fp16", "8bit", "4bit"]
    assert app._memory_tier_by_name == {
        "fp16": (16, None),
        "8bit": (8, None),
        "4bit": (4, None),
    }
    gb = 1024 ** 3
    assert governor.tier_cost_bytes["8bit"] == int(config.WHISPER_BITS_COST_GB[8] * gb)


def test_on_memory_tier_apple_backend_does_not_switch_ollama(monkeypatch):
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5:4b")
    app = _make_app(baseline="qwen3.5:4b", current_mode="直播")
    app.translator._translate_backend = "apple"
    app._memory_tier_by_name = {"8bit": (8, None)}

    app._on_memory_tier("8bit")

    assert app._baseline_ollama_model == "qwen3.5:4b"
    assert config.OLLAMA_MODEL == "qwen3.5:4b"
    assert app.translator.calls == []
    assert app.translator.bits_calls == [8]
    assert app.subtitle_window.statuses == ["🧠 内存紧张：识别精度降到 8bit（准确度不变）"]
