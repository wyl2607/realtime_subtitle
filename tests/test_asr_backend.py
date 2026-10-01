"""
tests/test_asr_backend.py — ASR Lane 单元测试

测试策略：
  - 用 fake mlx_whisper 模块替换真实实现（sys.modules mock），
    精确镜像 mlx-whisper 0.4.3 的实际模块布局
  - darwin 路径：测试适配器行为（路径传递、无重载、beam_size 丢弃、VAD 门、
    detect_language、auto 语言概率、unload 清缓存）
  - win32 路径：确认 _ensure_ml_deps 在 win32 下**不**触碰 MLX 代码

在 Windows CI 上运行时：darwin 特定的测试自动 skip（不报错、不 xfail）。
"""

import sys
import types
import importlib
import numpy as np
import pytest


# ─── fake mlx_whisper 模块家族（镜像 mlx-whisper 0.4.3 实际布局）────────────

def _make_fake_mlx_whisper():
    """返回一套完整的 fake mlx_whisper 包，结构对应 0.4.3：

    mlx_whisper                  - 顶层包
      .transcribe(audio, ...)    - 顶层函数（同时也是子模块名；函数遮住模块）
      .transcribe.ModelHolder    - 通过 importlib.import_module("mlx_whisper.transcribe") 拿到
      .load_models.load_model    - 加载模型对象
      .decoding.detect_language  - 语言检测
      .audio.log_mel_spectrogram - mel 计算
      .audio.load_audio          - 音频文件加载
      .audio.pad_or_trim         - 30s 裁剪
    """

    # ── mlx_whisper.transcribe：子模块（提供 ModelHolder） ──────────────────
    transcribe_mod = types.ModuleType("mlx_whisper.transcribe")

    class ModelHolder:
        model = None
        model_path = None

        @classmethod
        def get_model(cls, path, dtype):
            if cls.model_path != path:
                # 模拟"路径变了才重新加载"
                cls.model_path = path
                cls.model = object()  # 哑对象代表已加载的模型
            return cls.model

    transcribe_mod.ModelHolder = ModelHolder

    # ── mlx_whisper.audio 子模块 ─────────────────────────────────────────────
    audio_mod = types.ModuleType("mlx_whisper.audio")

    def log_mel_spectrogram(audio, n_mels=80):
        # 返回 (n_mels, n_frames) 形状的 numpy 数组（mlx-whisper 实际行为）
        n_frames = max(1, len(audio) // 160)
        return np.zeros((n_mels, n_frames), dtype=np.float32)

    def pad_or_trim(audio, length):
        if len(audio) >= length:
            return audio[:length]
        return np.pad(audio, (0, length - len(audio)))

    def load_audio(path):
        return np.zeros(16000, dtype=np.float32)

    audio_mod.log_mel_spectrogram = log_mel_spectrogram
    audio_mod.pad_or_trim = pad_or_trim
    audio_mod.load_audio = load_audio

    # ── mlx_whisper.decoding 子模块 ──────────────────────────────────────────
    decoding_mod = types.ModuleType("mlx_whisper.decoding")

    def detect_language(model, mel, tokenizer=None):
        # 返回 (tokens, [lang_probs_dict])；通道不用管（test 只看 probs）
        probs = {"de": 0.92, "en": 0.05, "fr": 0.03}
        return ([], [probs])

    decoding_mod.detect_language = detect_language

    # ── mlx_whisper.load_models 子模块 ──────────────────────────────────────
    load_models_mod = types.ModuleType("mlx_whisper.load_models")

    class _FakeModel:
        class dims:
            n_mels = 128  # large-v3-turbo 是 128

    def load_model(path_or_hf_repo, dtype=None):
        return _FakeModel()

    load_models_mod.load_model = load_model

    # ── mlx_whisper 顶层包（transcribe 是函数） ──────────────────────────────
    pkg = types.ModuleType("mlx_whisper")

    # transcribe 函数（0.4.3 里同名函数遮住子模块）
    _transcribe_call_log = []

    def transcribe(audio, *, path_or_hf_repo="", verbose=None,
                   temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),
                   compression_ratio_threshold=2.4, logprob_threshold=-1.0,
                   no_speech_threshold=0.6, condition_on_previous_text=True,
                   initial_prompt=None, word_timestamps=False,
                   prepend_punctuations="", append_punctuations="",
                   clip_timestamps="0", hallucination_silence_threshold=None,
                   task="transcribe", language=None, **decode_options):
        _transcribe_call_log.append({
            "path_or_hf_repo": path_or_hf_repo,
            "word_timestamps": word_timestamps,
            "decode_options": decode_options,
            "hallucination_silence_threshold": hallucination_silence_threshold,
        })
        seg = {
            "id": 0, "start": 0.0, "end": 1.0,
            "text": " Hallo Welt",
            "avg_logprob": -0.3,
            "compression_ratio": 1.2,
            "no_speech_prob": 0.01,
            "words": [
                {"word": " Hallo", "start": 0.0, "end": 0.5, "probability": 0.99},
                {"word": " Welt",  "start": 0.5, "end": 1.0, "probability": 0.98},
            ] if word_timestamps else [],
        }
        return {
            "text": " Hallo Welt",
            "segments": [seg],
            "language": language or "de",
            "language_probs": {"de": 0.92, "en": 0.05},
        }

    pkg.transcribe = transcribe
    pkg._transcribe_call_log = _transcribe_call_log

    # ── 子模块注册 ────────────────────────────────────────────────────────────
    pkg.transcribe_mod = transcribe_mod   # 内部访问用
    pkg.audio = audio_mod
    pkg.decoding = decoding_mod
    pkg.load_models = load_models_mod

    return pkg, transcribe_mod, audio_mod, decoding_mod, load_models_mod


# ─── fake mlx.core ───────────────────────────────────────────────────────────

def _make_fake_mlx_core():
    mx = types.ModuleType("mlx.core")
    mx.float16 = "float16"
    _cache_cleared = [0]

    def clear_cache():
        _cache_cleared[0] += 1

    mx.clear_cache = clear_cache
    mx._cache_cleared = _cache_cleared
    return mx


# ─── pytest fixtures ─────────────────────────────────────────────────────────

@pytest.fixture()
def fake_mlx(monkeypatch):
    """在 sys.modules 里注入 fake mlx 家族，yield 后清理。"""
    pkg, transcribe_mod, audio_mod, decoding_mod, load_models_mod = _make_fake_mlx_whisper()
    mx = _make_fake_mlx_core()

    fake_mlx_pkg = types.ModuleType("mlx")
    fake_mlx_pkg.core = mx

    monkeypatch.setitem(sys.modules, "mlx", fake_mlx_pkg)
    monkeypatch.setitem(sys.modules, "mlx.core", mx)
    monkeypatch.setitem(sys.modules, "mlx_whisper", pkg)
    monkeypatch.setitem(sys.modules, "mlx_whisper.transcribe", transcribe_mod)
    monkeypatch.setitem(sys.modules, "mlx_whisper.audio", audio_mod)
    monkeypatch.setitem(sys.modules, "mlx_whisper.decoding", decoding_mod)
    monkeypatch.setitem(sys.modules, "mlx_whisper.load_models", load_models_mod)

    # fake huggingface_hub：snapshot_download 直接返回 repo 名（无网络）
    hf_mod = types.ModuleType("huggingface_hub")

    def snapshot_download(repo_id, local_files_only=False):
        if local_files_only and not repo_id.startswith("/"):
            # 模拟"本地没缓存"的场景用 local_path=False 触发，这里默认成功
            pass
        return f"/fake/hf_cache/{repo_id.replace('/', '_')}"

    hf_mod.snapshot_download = snapshot_download
    monkeypatch.setitem(sys.modules, "huggingface_hub", hf_mod)

    yield {
        "pkg": pkg,
        "transcribe_mod": transcribe_mod,
        "audio_mod": audio_mod,
        "decoding_mod": decoding_mod,
        "load_models_mod": load_models_mod,
        "mx": mx,
        "hf": hf_mod,
    }


@pytest.fixture()
def backend_module(fake_mlx, monkeypatch):
    """加载 asr/backend.py，强制认为当前平台是 darwin。"""
    monkeypatch.setattr(sys, "platform", "darwin")

    # 若模块已缓存，重新加载以让 platform guard 生效
    mod_name = "realtime_subtitle.asr.backend"
    if mod_name in sys.modules:
        del sys.modules[mod_name]

    import realtime_subtitle.asr.backend as backend
    yield backend

    # 清理，防止下个测试拿到脏缓存
    if mod_name in sys.modules:
        del sys.modules[mod_name]


@pytest.fixture()
def model(backend_module, monkeypatch):
    """构造一个 MlxWhisperModel 实例（large-v3-turbo）。"""
    # 让 config.MLX_WHISPER_MODEL 为 None（走自动映射路径）
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "MLX_WHISPER_MODEL", None, raising=False)
    return backend_module.MlxWhisperModel("large-v3-turbo")


# ─── Darwin 专属测试 ──────────────────────────────────────────────────────────

pytestmark_darwin = pytest.mark.skipif(
    sys.platform != "darwin",
    reason="MLX backend 只在 macOS 上运行；Windows CI 跳过"
)


@pytestmark_darwin
def test_string_path_passed_to_transcribe(model, fake_mlx):
    """transcribe 调用时传的 path_or_hf_repo 必须是字符串，且是解析后的本地路径。"""
    audio = np.zeros(16000, dtype=np.float32)
    segs, info = model.transcribe(audio, language="de", word_timestamps=True)
    list(segs)  # 消耗迭代器

    call_log = fake_mlx["pkg"]._transcribe_call_log
    assert len(call_log) == 1
    path = call_log[0]["path_or_hf_repo"]
    assert isinstance(path, str), f"path_or_hf_repo 必须是 str，实际是 {type(path)}"
    # huggingface_hub.snapshot_download 应该把 HF repo 名转成本地路径
    assert path.startswith("/fake/hf_cache/"), f"路径未走 snapshot_download: {path}"


@pytestmark_darwin
def test_no_reload_across_calls(fake_mlx, backend_module, monkeypatch):
    """相同模型路径多次调用 transcribe，ModelHolder.get_model 只被调用一次（缓存复用）。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "MLX_WHISPER_MODEL", None, raising=False)

    # 记录 get_model 被调用的次数
    call_count = [0]
    orig_get_model = fake_mlx["transcribe_mod"].ModelHolder.get_model

    @classmethod
    def counting_get_model(cls, path, dtype):
        call_count[0] += 1
        return orig_get_model.__func__(cls, path, dtype)

    fake_mlx["transcribe_mod"].ModelHolder.get_model = counting_get_model

    m = backend_module.MlxWhisperModel("large-v3-turbo")
    audio = np.zeros(16000, dtype=np.float32)

    # 第一次构造 + 两次 transcribe（路径相同）
    m.transcribe(audio, language="de")
    m.transcribe(audio, language="de")

    # 构造时调了一次 get_model（预加载）；之后 transcribe 直接用已加载的模型，
    # 不应该再调 get_model（mlx_whisper.transcribe 内部走缓存）
    assert call_count[0] == 1, (
        f"get_model 被调了 {call_count[0]} 次，应该只有构造时的 1 次"
    )


@pytestmark_darwin
def test_beam_size_dropped(model, fake_mlx):
    """beam_size 参数必须被丢弃（不传给 mlx_whisper.transcribe）。

    mlx-whisper 0.4.3 未实现 beam search，传进去会 TypeError。
    """
    audio = np.zeros(16000, dtype=np.float32)
    # 调用方 streaming_asr.py:393 传 beam_size=config.WHISPER_BEAM_SIZE（默认 3）
    segs, info = model.transcribe(audio, language="de", beam_size=3, word_timestamps=True)
    list(segs)

    call_log = fake_mlx["pkg"]._transcribe_call_log
    assert len(call_log) == 1
    # beam_size 不能出现在 decode_options 里
    decode_opts = call_log[0]["decode_options"]
    assert "beam_size" not in decode_opts, (
        f"beam_size 不应传给 mlx-whisper，但出现在 decode_options: {decode_opts}"
    )


@pytestmark_darwin
def test_vad_gate_skips_model_on_silence(model, fake_mlx, monkeypatch):
    """vad_filter=True 且音频能量极低时，应直接返回空 segments，不调 mlx_whisper.transcribe。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.01, raising=False)

    # 全零音频（RMS = 0.0 < 0.01）
    silent_audio = np.zeros(16000, dtype=np.float32)
    segs, info = model.transcribe(silent_audio, vad_filter=True)
    seg_list = list(segs)

    assert seg_list == [], f"静音应返回空 segments，但得到 {seg_list}"
    # transcribe 不应被调用
    assert len(fake_mlx["pkg"]._transcribe_call_log) == 0, (
        "静音情况下 mlx_whisper.transcribe 不应被调用"
    )


@pytestmark_darwin
def test_vad_gate_passes_speech(model, fake_mlx, monkeypatch):
    """有声音时 vad_filter=True 不应阻止模型调用。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.001, raising=False)

    # 有能量的音频
    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    segs, info = model.transcribe(audio, language="de", vad_filter=True)
    list(segs)

    assert len(fake_mlx["pkg"]._transcribe_call_log) == 1


@pytestmark_darwin
def test_hallucination_silence_threshold_with_word_timestamps(model, fake_mlx, monkeypatch):
    """word_timestamps=True 时应传 hallucination_silence_threshold。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)  # 不用 VAD 门

    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    segs, info = model.transcribe(audio, language="de", word_timestamps=True)
    list(segs)

    call = fake_mlx["pkg"]._transcribe_call_log[-1]
    assert call["hallucination_silence_threshold"] is not None, (
        "word_timestamps=True 时应传 hallucination_silence_threshold"
    )


@pytestmark_darwin
def test_detect_language_mel_shape(fake_mlx, backend_module, monkeypatch):
    """detect_language 传给 decoding.detect_language 的 mel 维度必须正确（n_mels 来自模型）。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "MLX_WHISPER_MODEL", None, raising=False)
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)

    # 记录 decoding.detect_language 收到的 mel
    received_mels = []
    orig_detect = fake_mlx["decoding_mod"].detect_language

    def spy_detect(model, mel, tokenizer=None):
        received_mels.append(mel)
        return orig_detect(model, mel, tokenizer)

    fake_mlx["decoding_mod"].detect_language = spy_detect

    m = backend_module.MlxWhisperModel("large-v3-turbo")
    audio = (np.random.randn(16000 * 5) * 0.1).astype(np.float32)
    lang, prob, all_probs = m.detect_language(audio=audio, vad_filter=False)

    assert len(received_mels) == 1
    mel = received_mels[0]
    # log_mel_spectrogram 返回 (n_mels, n_frames)；fake model.dims.n_mels = 128
    assert mel.shape[0] == 128, f"mel 第一维应是 n_mels=128，实际 {mel.shape}"


@pytestmark_darwin
def test_detect_language_vad_silence_returns_und(model, monkeypatch):
    """detect_language + vad_filter=True 在静音时应返回 ('und', 0.0, {})。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.01, raising=False)

    silent = np.zeros(16000 * 5, dtype=np.float32)
    lang, prob, all_probs = model.detect_language(audio=silent, vad_filter=True)

    assert lang == "und", f"静音应返回 'und'，实际 {lang!r}"
    assert prob == 0.0, f"静音概率应为 0.0，实际 {prob}"
    assert all_probs == {}, f"静音 all_probs 应为空 dict，实际 {all_probs}"


@pytestmark_darwin
def test_auto_language_prob_is_real(model, fake_mlx, monkeypatch):
    """language=None（自动检测）时 info.language_probability 应来自实际检测，不是假常量 1.0。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)

    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    # fake transcribe 返回 language_probs = {"de": 0.92, "en": 0.05}
    segs, info = model.transcribe(audio, language=None)
    list(segs)

    assert info.language_probability != 1.0 or info.language == "de", (
        "自动检测时 language_probability 应来自真实 language_probs，"
        f"实际 language={info.language!r}, prob={info.language_probability}"
    )
    # 具体值：de 的概率是 0.92
    if info.language == "de":
        assert abs(info.language_probability - 0.92) < 0.01


@pytestmark_darwin
def test_locked_language_prob_is_1(model, monkeypatch):
    """language 已指定时 info.language_probability 应为 1.0（行为与旧实现一致）。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)

    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    segs, info = model.transcribe(audio, language="de")
    list(segs)

    assert info.language_probability == 1.0, (
        f"语言锁定时 prob 应为 1.0，实际 {info.language_probability}"
    )


@pytestmark_darwin
def test_unload_clears_model_holder_and_cache(fake_mlx, backend_module, monkeypatch):
    """unload() 必须清空 ModelHolder.model/model_path 并调用 mx.clear_cache()。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "MLX_WHISPER_MODEL", None, raising=False)

    m = backend_module.MlxWhisperModel("large-v3-turbo")
    holder = fake_mlx["transcribe_mod"].ModelHolder
    mx = fake_mlx["mx"]

    # 构造后 ModelHolder 应该有缓存
    assert holder.model is not None
    assert holder.model_path is not None

    before_clear = mx._cache_cleared[0]
    m.unload()

    assert holder.model is None, "unload 后 ModelHolder.model 应为 None"
    assert holder.model_path is None, "unload 后 ModelHolder.model_path 应为 None"
    assert mx._cache_cleared[0] == before_clear + 1, (
        f"unload 应调用 mx.clear_cache() 一次，实际调用了 "
        f"{mx._cache_cleared[0] - before_clear} 次"
    )


@pytestmark_darwin
def test_word_timestamps_segments_have_words(model, monkeypatch):
    """word_timestamps=True 时，_Segment 对象应有 .words 列表，每个 word 有 .word/.start/.end。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)

    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    segs, info = model.transcribe(audio, language="de", word_timestamps=True)
    seg_list = list(segs)

    assert seg_list, "应至少有一个 segment"
    seg = seg_list[0]
    assert hasattr(seg, "words"), "segment 应有 .words"
    assert len(seg.words) > 0, "word_timestamps=True 时 words 不应为空"
    w = seg.words[0]
    assert hasattr(w, "word")
    assert hasattr(w, "start")
    assert hasattr(w, "end")


@pytestmark_darwin
def test_no_speech_prob_accessible(model, monkeypatch):
    """segment 应有 .no_speech_prob（streaming_asr.py:371 读取）。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)

    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    segs, info = model.transcribe(audio, language="de", word_timestamps=False)
    seg_list = list(segs)

    assert seg_list
    assert hasattr(seg_list[0], "no_speech_prob")
    assert hasattr(seg_list[0], "avg_logprob")


@pytestmark_darwin
def test_info_language_accessible(model, monkeypatch):
    """info 对象应有 .language（offline.py:757 读取）。"""
    import realtime_subtitle.config as config
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.0, raising=False)

    audio = (np.random.randn(16000) * 0.1).astype(np.float32)
    _segs, info = model.transcribe(audio, language="de", word_timestamps=False)

    assert hasattr(info, "language"), "info 应有 .language"
    assert isinstance(info.language, str)


# ─── Windows（win32）路径未被 MLX 代码触碰 ───────────────────────────────────

def test_win32_path_does_not_import_mlx(monkeypatch):
    """Windows 下 _ensure_ml_deps 不应导入 mlx 或 mlx_whisper。"""
    monkeypatch.setattr(sys, "platform", "win32")

    # 确保 _WhisperModel 缓存为空，强制重新执行 _ensure_ml_deps
    import realtime_subtitle.translate.translator_queue as tq
    monkeypatch.setattr(tq, "_WhisperModel", None)

    # fake faster_whisper（Windows 路径依赖）
    fw_mod = types.ModuleType("faster_whisper")

    class FakeWhisperModel:
        pass

    fw_mod.WhisperModel = FakeWhisperModel
    monkeypatch.setitem(sys.modules, "faster_whisper", fw_mod)

    # fake torch（Windows 路径在 _ensure_ml_deps 里先 import torch）
    torch_mod = types.ModuleType("torch")
    monkeypatch.setitem(sys.modules, "torch", torch_mod)

    # 去掉 mlx 相关模块（如果在环境里存在的话），确保测试不依赖真实 mlx
    for key in list(sys.modules.keys()):
        if key.startswith("mlx"):
            monkeypatch.delitem(sys.modules, key, raising=False)

    result = tq._ensure_ml_deps()

    # Windows 下应该返回 FakeWhisperModel（faster-whisper 路径）
    assert result is FakeWhisperModel, (
        f"win32 下 _ensure_ml_deps 应返回 WhisperModel，实际返回 {result}"
    )
    # mlx_whisper 不应被导入
    assert "mlx_whisper" not in sys.modules, (
        "win32 下 mlx_whisper 不应被 import"
    )
