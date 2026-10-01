"""
macOS Apple Silicon MLX-Whisper 适配器

为什么这个文件存在：
  streaming_asr.py 和 offline.py 都通过 faster-whisper 的 WhisperModel 接口访问
  Whisper——model.transcribe() / model.detect_language()。macOS 没有 CUDA，
  用不了 ctranslate2；Apple Silicon 有 ANE/GPU，mlx-whisper 能原生利用 Metal。
  本适配器把 mlx-whisper 0.4.3 的实际 API 包在和 WhisperModel 相同的外皮下，
  让上层代码完全不感知平台差异。

☠️ 本文件只在 sys.platform == "darwin" 时被加载：
  - translator_queue._ensure_ml_deps() 在 darwin 下返回 MlxWhisperModel 类
  - 任何 Windows 路径都**不经过**这里，Windows 行为零改动

mlx-whisper 0.4.3 的实际 API（已在 Mac M4 上验证）：
  - mlx_whisper.transcribe 是**函数**，不是模块，但 transcribe.ModelHolder
    可以通过 importlib.import_module("mlx_whisper.transcribe") 拿到
  - 模型缓存在 ModelHolder 的类属性（model / model_path）里，同路径不重载
  - beam search 未实现，beam_size 必须丢掉（传进去 transcribe 会炸）
  - MEL 是 channels-last：(n_frames, n_mels)，n_mels 随模型维数变化（128/80）
  - mx.clear_cache() 在 0.4.3 是顶层函数（mx.metal.clear_cache 已废弃）
"""

import sys
import importlib
import logging
import numpy as np

# 只在 darwin 下运行，但 import 保护放在调用方(_ensure_ml_deps)那一层，
# 这里仍写 guard 以防模块被误导入
if sys.platform != "darwin":
    raise ImportError("backend.py 只在 macOS 下使用")

import mlx.core as mx
import mlx_whisper

# 通过 importlib 拿 transcribe 子模块（mlx_whisper.transcribe 名字被函数遮掉了）
_transcribe_mod = importlib.import_module("mlx_whisper.transcribe")
_ModelHolder = _transcribe_mod.ModelHolder  # 模型缓存（类级别 model/model_path）

# 30 秒窗口的帧数（mlx-whisper 内部常量 N_FRAMES = CHUNK_LENGTH * SAMPLE_RATE / HOP_LENGTH）
_N_FRAMES = 3000  # 30s × 100 帧/秒（mel hop 160 samples @16kHz）

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部数据类：模拟 faster-whisper 的 Segment / Word / TranscriptionInfo
# ---------------------------------------------------------------------------

class _Word:
    """模拟 faster-whisper 的 Word namedtuple 字段"""
    __slots__ = ("word", "start", "end", "probability")

    def __init__(self, word: str, start: float, end: float, probability: float = 1.0):
        self.word = word
        self.start = float(start)
        self.end = float(end)
        self.probability = float(probability)


class _Segment:
    """模拟 faster-whisper 的 Segment namedtuple 字段"""
    __slots__ = ("id", "seek", "start", "end", "text", "tokens",
                 "avg_logprob", "compression_ratio", "no_speech_prob", "words")

    def __init__(self, seg: dict, word_timestamps: bool):
        self.id = seg.get("id", 0)
        self.seek = 0
        self.start = float(seg.get("start", 0.0))
        self.end = float(seg.get("end", 0.0))
        self.text = str(seg.get("text", ""))
        self.tokens = seg.get("tokens", [])
        self.avg_logprob = float(seg.get("avg_logprob", -1.0))
        self.compression_ratio = float(seg.get("compression_ratio", 1.0))
        self.no_speech_prob = float(seg.get("no_speech_prob", 0.0))

        if word_timestamps and seg.get("words"):
            self.words = [
                _Word(
                    word=w.get("word", ""),
                    start=float(w.get("start", self.start)),
                    end=float(w.get("end", self.end)),
                    probability=float(w.get("probability", 1.0)),
                )
                for w in seg["words"]
            ]
        else:
            # word_timestamps=False 时调用方不会访问 .words，设空列表以防万一
            self.words = []


class _TranscriptionInfo:
    """模拟 faster-whisper 的 TranscriptionInfo，供 offline.py 读 .language"""
    __slots__ = ("language", "language_probability", "duration",
                 "duration_after_vad", "all_language_probs",
                 "transcription_options", "vad_options")

    def __init__(self, language: str, language_probability: float,
                 duration: float, all_probs: dict | None = None):
        self.language = language
        self.language_probability = float(language_probability)
        self.duration = float(duration)
        self.duration_after_vad = float(duration)
        self.all_language_probs = all_probs or {}
        self.transcription_options = None
        self.vad_options = None


# ---------------------------------------------------------------------------
# VAD 门：能量/RMS 静音检测
# ---------------------------------------------------------------------------

def _is_silence(audio: np.ndarray, threshold: float) -> bool:
    """简单能量门：RMS 低于阈值判为静音，跳过模型调用。

    ☠️ 行为与 faster-whisper 的 Silero VAD（onnx 神经网络）不同：
    Silero 做帧级语音活动检测并能识别非语音噪声；这里只做全局 RMS 比较，
    音乐/环境音量足够大时 RMS 超阈值但仍无语音——这种情况下两者结果不同。
    代价是偶尔调模型在纯音乐段上做推理（mlx-whisper 自身也有 hallucination
    抑制，加上 no_speech_prob 过滤，影响可控）。对比 Silero，静音段的漏检
    不影响最终字幕质量，只略微多一次无效推理。
    """
    if len(audio) == 0:
        return True
    rms = float(np.sqrt(np.mean(audio.astype(np.float64) ** 2)))
    return rms < threshold


# ---------------------------------------------------------------------------
# 主适配器类
# ---------------------------------------------------------------------------

class MlxWhisperModel:
    """macOS 专用 mlx-whisper 适配器，接口对齐 faster-whisper.WhisperModel。

    ☠️ 构造参数故意接收 **kwargs 以兼容调用方的 device=/compute_type= 等关键字：
    translator_queue._ensure_ml_deps() 返回本类后，外层以
    WhisperModel(model_size, device=..., compute_type=..., local_files_only=...)
    的形式构造——这些 kwargs 在 MLX 下无意义，吸收丢弃即可，不能让它们炸掉。
    """

    def __init__(self, model_size_or_path: str, **kwargs):
        # local_files_only 控制是否允许从 HuggingFace 下载；用 huggingface_hub
        # 把 HF repo 名解析成本地缓存路径，再把**路径字符串**传给 mlx-whisper，
        # 这样 mlx-whisper 内部不需要再做任何网络请求，也不需要改 os.environ
        local_files_only = bool(kwargs.get("local_files_only", False))
        self.model_path = self._resolve_model_path(model_size_or_path, local_files_only)

        # 预加载模型（填充 ModelHolder 缓存）；后续 transcribe 调用相同路径时不重载
        # ☠️ 必须在构造时就 get_model，而不是懒加载：streaming_asr 在构造后
        # 立刻开始推理，第一次推理带冷加载延迟会破坏延迟预算
        try:
            _ModelHolder.get_model(self.model_path, mx.float16)
            print(f"✅ MLX-Whisper 模型已加载: {self.model_path}")
        except Exception as e:
            # 加载失败时清掉缓存，让调用方的异常处理路径决定是否重试
            _ModelHolder.model = None
            _ModelHolder.model_path = None
            raise RuntimeError(f"MLX-Whisper 模型加载失败: {e}") from e

    @staticmethod
    def _resolve_model_path(model_size_or_path: str, local_files_only: bool) -> str:
        """把模型名（如 'large-v3-turbo'）映射到 HF repo 或本地路径字符串。

        优先读 config.MLX_WHISPER_MODEL 覆盖（非 None 时直接用，跳过自动映射）。

        mlx-whisper 接受的 path_or_hf_repo 格式：
          - HF repo 名：'mlx-community/whisper-large-v3-turbo'
          - 本地目录路径

        ☠️ 不改 os.environ（有线程安全风险）；改为用 huggingface_hub.snapshot_download
        来解析本地缓存路径——它在 local_files_only=True 时只查磁盘不发网络请求。
        """
        import realtime_subtitle.config as config

        # config.MLX_WHISPER_MODEL 非 None：用户显式指定 HF repo 或本地路径，直接用
        mlx_override = getattr(config, "MLX_WHISPER_MODEL", None)
        if mlx_override:
            repo = mlx_override
        else:
            # 标准 faster-whisper 模型名 → mlx-community 对应的 HF repo
            _MODEL_MAP = {
                "tiny":            "mlx-community/whisper-tiny",
                "tiny.en":         "mlx-community/whisper-tiny.en",
                "base":            "mlx-community/whisper-base",
                "base.en":         "mlx-community/whisper-base.en",
                "small":           "mlx-community/whisper-small",
                "small.en":        "mlx-community/whisper-small.en",
                "medium":          "mlx-community/whisper-medium",
                "medium.en":       "mlx-community/whisper-medium.en",
                "large-v2":        "mlx-community/whisper-large-v2",
                "large-v3":        "mlx-community/whisper-large-v3",
                "large-v3-turbo":  "mlx-community/whisper-large-v3-turbo",
            }
            repo = _MODEL_MAP.get(model_size_or_path, model_size_or_path)

        try:
            from huggingface_hub import snapshot_download
            local_path = snapshot_download(
                repo_id=repo,
                local_files_only=local_files_only,
            )
            return local_path
        except Exception:
            # 解析失败时直接用原始名（mlx-whisper 也能接受 HF repo 名下载）
            return repo


    # -----------------------------------------------------------------------
    # transcribe：对齐 faster-whisper 的调用签名（streaming_asr.py:388 / offline.py:739）
    # -----------------------------------------------------------------------

    def transcribe(
        self,
        audio,
        *,
        language: str | None = None,
        task: str = "transcribe",
        initial_prompt: str | None = None,
        beam_size: int | None = None,      # ☠️ mlx-whisper 未实现 beam search，必须忽略
        word_timestamps: bool = False,
        condition_on_previous_text: bool = True,
        vad_filter: bool = False,
        temperature=None,
        **decode_options,
    ):
        """转录音频，返回 (segments_iter, info)，接口对齐 faster-whisper。

        ☠️ beam_size 被静默丢弃——mlx-whisper 0.4.3 只实现了 greedy 解码，
        传 beam_size 进 transcribe() 会抛 TypeError。调用方 streaming_asr 传
        beam_size=config.WHISPER_BEAM_SIZE（默认3），这里必须拦住。

        vad_filter=True 用简单 RMS 门代替 Silero VAD（见 _is_silence 的注释）：
        静音时直接返回空 segments，不调模型，节省 Apple GPU 算力。
        """
        import realtime_subtitle.config as config

        energy_threshold = getattr(config, "ENERGY_THRESHOLD_SPEECH", 0.01)

        # 把输入统一成 numpy float32
        audio_np = self._to_numpy(audio)

        # VAD 门：静音快速返回
        if vad_filter and _is_silence(audio_np, energy_threshold):
            info = _TranscriptionInfo(
                language=language or "und",
                language_probability=1.0 if language else 0.0,
                duration=len(audio_np) / 16000,
            )
            return iter([]), info

        # 组装 transcribe 参数，只传 mlx-whisper 0.4.3 实际支持的关键字
        kwargs = dict(
            path_or_hf_repo=self.model_path,
            verbose=None,
            condition_on_previous_text=condition_on_previous_text,
            word_timestamps=word_timestamps,
            initial_prompt=initial_prompt or None,
            task=task,
        )
        # 自动语言：先用 detect_language 做一次真检测（一次编码器前向，M4 上
        # ~0.1 秒），再把结果锁进 transcribe。☠️ mlx-whisper 0.4.3 的 transcribe
        # 结果里只有 "language"，**没有**语言概率字段——不先检测就只能报 0.0
        # 或编一个常量，离线工具的"自动识别源语言"就没有置信度可看。
        auto_probs = {}
        if language:
            kwargs["language"] = language
        else:
            det_lang, det_prob, auto_probs = self.detect_language(audio=audio_np, vad_filter=False)
            if det_lang and det_lang != "und":
                kwargs["language"] = det_lang

        # temperature：调用方不传时用 mlx-whisper 默认多步退火序列
        if temperature is not None:
            kwargs["temperature"] = temperature

        # hallucination_silence_threshold 仅在 word_timestamps 时有意义
        if word_timestamps:
            kwargs["hallucination_silence_threshold"] = 2.0

        # ☠️ 不传 beam_size（见函数 docstring）
        # ☠️ 不传 fp16（decode option，默认 True 即 float16，符合 M4 最优）

        result = mlx_whisper.transcribe(audio_np, **kwargs)

        # 自动语言检测时从结果里取语言概率
        detected_lang = result.get("language", language or "und")
        if language:
            # 语言已锁定：概率标为 1.0（与 faster-whisper 锁定语言时一致）
            lang_prob = 1.0
        else:
            lang_prob = float(auto_probs.get(detected_lang, 0.0))

        # 音频时长（秒）
        duration = len(audio_np) / 16000

        info = _TranscriptionInfo(
            language=detected_lang,
            language_probability=lang_prob,
            duration=duration,
            all_probs=auto_probs,
        )

        # 把 mlx-whisper 返回的 dict-list 转成 _Segment 对象迭代器
        raw_segs = result.get("segments", [])
        segments = [_Segment(s, word_timestamps) for s in raw_segs]
        return iter(segments), info

    # -----------------------------------------------------------------------
    # detect_language：对齐 streaming_asr.py:518 的调用签名
    # -----------------------------------------------------------------------

    def detect_language(
        self,
        audio=None,
        vad_filter: bool = False,
    ):
        """对音频做语言检测，返回 (lang, prob, all_probs)。

        调用方（streaming_asr.py:518）：
            lang, prob, _all_probs = self.model.detect_language(audio=audio, vad_filter=True)
        然后把 (lang, prob) 交给置信度门（LANGUAGE_SWITCH_MIN_PROB）过滤。

        ☠️ 静音时返回 ("und", 0.0, {})——置信度 0.0 低于任何合理的 MIN_PROB（默认
        0.85），调用方的置信度门会直接丢弃，不会触发语言切换。不用 fallback 到
        整个 transcribe（那样会把静音假报成 1.0 概率的某语言）。

        MLX-Whisper 检测语言的步骤（对照实际 API）：
          1. load_models.load_model 拿模型对象
          2. audio.log_mel_spectrogram(audio, model.dims.n_mels) → mel
          3. mel 是 channels-last (n_frames, n_mels)；pad/trim 到 N_FRAMES
          4. decoding.detect_language(model, mel) → (tokens, [lang_probs_dict])
        """
        import realtime_subtitle.config as config

        energy_threshold = getattr(config, "ENERGY_THRESHOLD_SPEECH", 0.01)
        audio_np = self._to_numpy(audio)

        # VAD 门：静音 → 返回"未识别"结果（置信度 0.0 让调用方置信度门过滤）
        if vad_filter and _is_silence(audio_np, energy_threshold):
            return "und", 0.0, {}

        try:
            from mlx_whisper import decoding, audio as mlx_audio

            # 走 ModelHolder 的缓存（和 transcribe 同一份权重），不要 load_model
            # 重新读盘——实测每次多 0.6 秒，而这条路径在识别线程里定期跑。
            model = _ModelHolder.get_model(self.model_path, mx.float16)
            n_mels = model.dims.n_mels  # large-v3/turbo = 128，其他大多数 = 80

            # ☠️ 顺序是「先算 mel，再按帧补齐/截断」：mlx 的 pad_or_trim 只接受
            # mx.array，直接喂 numpy 音频会 TypeError（被下面的 except 吞成
            # ("und", 0.0)，自动切语言就悄无声息地永远不触发——2026-10-01 在 M4
            # 上实测踩到）。mel 是 channels-last (n_frames, n_mels)，所以沿
            # axis=-2 补到 N_FRAMES（30 秒窗口）；dtype 要和 float16 权重一致。
            mel = mlx_audio.log_mel_spectrogram(audio_np.astype(np.float32), n_mels=n_mels)
            mel = mlx_audio.pad_or_trim(mel, mlx_audio.N_FRAMES, axis=-2).astype(mx.float16)

            _tokens, probs_list = decoding.detect_language(model, mel)
            all_probs = probs_list if isinstance(probs_list, dict) else (probs_list[0] if probs_list else {})

            if not all_probs:
                return "und", 0.0, {}

            best_lang = max(all_probs, key=lambda k: all_probs[k])
            best_prob = float(all_probs[best_lang])
            return best_lang, best_prob, all_probs

        except Exception as e:
            log.warning("MLX detect_language 失败: %s", e)
            return "und", 0.0, {}

    # -----------------------------------------------------------------------
    # 显式卸载（由 translator_queue._unload_our_models 调用）
    # -----------------------------------------------------------------------

    def unload(self):
        """释放 MLX 模型缓存和 Metal 显存。

        ☠️ 不依赖 __del__：Python 的 GC 时机不确定，在 __del__ 里清缓存在
        进程退出时已经是半死状态，mx.clear_cache() 未必跑得到。
        让 translator_queue._unload_our_models 在 executor 排干之后显式调用。
        """
        try:
            _ModelHolder.model = None
            _ModelHolder.model_path = None
            mx.clear_cache()
            print("🧹 MLX-Whisper 模型缓存已清空（Metal 显存已释放）")
        except Exception as e:
            log.warning("MLX unload 失败: %s", e)

    # -----------------------------------------------------------------------
    # 内部工具
    # -----------------------------------------------------------------------

    @staticmethod
    def _to_numpy(audio) -> np.ndarray:
        """把各种格式（numpy / mlx array / list / 文件路径字符串）统一成 float32 numpy。"""
        if isinstance(audio, str):
            # offline.py 传文件路径字符串
            import mlx_whisper.audio as mlx_audio
            return mlx_audio.load_audio(audio)
        if hasattr(audio, "numpy"):
            # mlx array 或 torch tensor
            return np.array(audio).astype(np.float32)
        return np.asarray(audio, dtype=np.float32)
