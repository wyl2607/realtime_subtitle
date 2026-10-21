"""MLX Whisper adapter that presents the faster-whisper model surface."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from dataclasses import dataclass
from functools import wraps
import importlib
from types import SimpleNamespace
import gc
import time

import numpy as np


SAMPLING_RATE = 16000

# ☠️ 所有 MLX 调用（加载/识别/测语言/换精度）都必须在**同一个**线程里跑。
# MLX 的 GPU stream 是线程局部的：权重在后台加载线程里建、识别线程里用，会
# 不定期报 "There is no Stream(gpu, 1) in current thread"，那一轮识别直接失败。
# 2026-10-08 实跑撞上：每轮都失败、一句字幕都没有；最小复现（线程 A 加载、
# 线程 B transcribe）同一份代码时好时坏，在加载线程里 mx.eval 权重也挡不住。
# 所以不去猜哪个数组挂在哪个 stream 上，直接让 MLX 只见到一个线程。
_MLX_EXECUTOR = ThreadPoolExecutor(max_workers=1, thread_name_prefix="mlx")


def _on_mlx_thread(fn):
    @wraps(fn)
    def run(*args, **kwargs):
        return _MLX_EXECUTOR.submit(fn, *args, **kwargs).result()
    return run


@dataclass
class MlxWord:
    start: float
    end: float
    word: str
    probability: float | None = None


@dataclass
class MlxSegment:
    start: float
    end: float
    text: str
    words: list[MlxWord]
    no_speech_prob: float = 0.0
    avg_logprob: float = 0.0


class _MlxAlignmentEncoderCache:
    """让词级对齐复用紧邻 decode 已算出的 audio features。

    mlx-whisper 0.4.x 的 ``forward_with_cross_qk`` 又会完整调用一次 encoder。
    它紧跟同一 mel window 的 ``model.decode``，其 encoder 输出语义相同；只在
    这一次 transcribe 调用、且由单一 MLX 线程执行时临时替换，结束立即恢复。
    """

    def __init__(self, model):
        self.model = model
        self._restore = []
        self._audio_features = None

    def __enter__(self):
        try:
            encoder = self.model.encoder
            forward = self.model.forward_with_cross_qk

            @wraps(encoder)
            def cache_encoder(mel):
                features = encoder(mel)
                self._audio_features = features
                return features

            @wraps(forward)
            def reuse_encoder(_mel, tokens):
                # forward_with_cross_qk 只由 add_word_timestamps 在紧随 decode 的
                # 同一个窗口调用；没有可复用结果时保留第三方原实现。
                if self._audio_features is None:
                    return forward(_mel, tokens)
                logits, _cache, cross_qk = self.model.decoder(tokens, self._audio_features)
                return logits, cross_qk

            self._replace("encoder", cache_encoder, encoder)
            self._replace("forward_with_cross_qk", reuse_encoder, forward)
            return self
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_exc):
        for name, original, was_state_item, was_instance_attr in reversed(self._restore):
            if was_state_item or was_instance_attr:
                setattr(self.model, name, original)
            else:
                delattr(self.model, name)

    def _replace(self, name, value, original):
        self._restore.append((
            name,
            original,
            isinstance(self.model, dict) and name in self.model,
            name in vars(self.model),
        ))
        setattr(self.model, name, value)


class _MlxTranscribeProfiler:
    """仅基准模式下记录互斥的 MLX 转写阶段墙钟，退出时恢复原函数。"""

    def __init__(self, module, model, mx):
        self.module = module
        self.model = model
        self.mx = mx
        self.values = {name: 0.0 for name in ("mel", "encoder", "decoder", "alignment")}
        self._restore = []
        self._align_depth = 0
        self._decode_encoder_s = 0.0

    def _replace(self, obj, name, wrapper):
        original = getattr(obj, name)
        was_state_item = isinstance(obj, dict) and name in obj
        was_instance_attr = name in vars(obj)
        setattr(obj, name, wrapper(original))
        self._restore.append((
            obj,
            name,
            original,
            was_state_item,
            was_instance_attr,
        ))

    def _eval(self, value):
        evaluator = getattr(self.mx, "eval", None)
        if evaluator is not None:
            evaluator(value)

    def _timed(self, name, original):
        @wraps(original)
        def run(*args, **kwargs):
            started = time.perf_counter()
            try:
                value = original(*args, **kwargs)
                self._eval(value)
                return value
            finally:
                self.values[name] += time.perf_counter() - started
        return run

    def __enter__(self):
        try:
            self._replace(self.module, "log_mel_spectrogram", lambda original: self._timed("mel", original))

            def record_encoder(original):
                @wraps(original)
                def run(*args, **kwargs):
                    if self._align_depth:
                        return original(*args, **kwargs)
                    started = time.perf_counter()
                    try:
                        value = original(*args, **kwargs)
                        self._eval(value)
                        return value
                    finally:
                        elapsed = time.perf_counter() - started
                        self.values["encoder"] += elapsed
                        self._decode_encoder_s += elapsed
                return run

            def record_decode(original):
                @wraps(original)
                def run(*args, **kwargs):
                    encoder_before = self._decode_encoder_s
                    started = time.perf_counter()
                    try:
                        return original(*args, **kwargs)
                    finally:
                        elapsed = time.perf_counter() - started
                        # model.decode 包住一次 encoder 和所有自回归 decoder 调用；
                        # 把已单独计入的 encoder 扣掉，避免阶段相加时双计。
                        self.values["decoder"] += max(
                            elapsed - (self._decode_encoder_s - encoder_before), 0.0,
                        )
                return run

            def alignment_phase(original):
                @wraps(original)
                def run(*args, **kwargs):
                    started = time.perf_counter()
                    self._align_depth += 1
                    try:
                        return original(*args, **kwargs)
                    finally:
                        self._align_depth -= 1
                        self.values["alignment"] += time.perf_counter() - started
                return run

            self._replace(self.model, "encoder", record_encoder)
            self._replace(self.model, "decode", record_decode)
            self._replace(self.module, "add_word_timestamps", alignment_phase)
            return self
        except Exception:
            self.__exit__()
            raise

    def __exit__(self, *_exc):
        for obj, name, original, was_state_item, was_instance_attr in reversed(self._restore):
            if was_state_item or was_instance_attr:
                setattr(obj, name, original)
            else:
                delattr(obj, name)


class _MlxTranscribeContext:
    """应用一次性对齐优化，并按需附加细粒度基准计时。"""

    def __init__(self, repo, collect_timing):
        self.repo = repo
        self.collect_timing = collect_timing
        self._stack = ExitStack()

    def __enter__(self):
        try:
            module = importlib.import_module("mlx_whisper.transcribe")
            import mlx.core as mx
            model = module.ModelHolder.get_model(self.repo, mx.float16)
        except Exception:
            return None

        try:
            self._stack.enter_context(_MlxAlignmentEncoderCache(model))
            if self.collect_timing:
                return self._stack.enter_context(_MlxTranscribeProfiler(module, model, mx))
            return None
        except Exception:
            # 第三方模型内部接口变了时宁可放弃优化/分段，不影响正常识别。
            self._stack.close()
            return None

    def __exit__(self, *exc):
        return self._stack.__exit__(*exc)


def _timing_record(total_s, vad_s, profiler):
    """分段互斥：alignment 含其内部 cross-attention 与 CPU DTW。"""
    values = getattr(profiler, "values", {})
    mel_s = values.get("mel", 0.0)
    encoder_s = values.get("encoder", 0.0)
    decoder_s = values.get("decoder", 0.0)
    alignment_s = values.get("alignment", 0.0)
    python_s = max(total_s - vad_s - mel_s - encoder_s - decoder_s - alignment_s, 0.0)
    return {
        "total_s": total_s,
        "vad_s": vad_s,
        "mel_s": mel_s,
        "encoder_s": encoder_s,
        "decoder_s": decoder_s,
        "alignment_s": alignment_s,
        "python_s": python_s,
    }


@_on_mlx_thread
def load_mlx_model(repo: str) -> "MlxWhisperModel":
    """解析本地路径并预加载权重，再包成 MlxWhisperModel。"""
    import mlx.core as mx
    from huggingface_hub import snapshot_download
    from mlx_whisper.transcribe import ModelHolder

    # 先只认本地缓存，和 faster-whisper 路径同理：mlx_whisper 默认每次
    # 加载都去 HF 做 etag 检查，网络差时是十几秒超时。本地没有才下载
    try:
        path = snapshot_download(repo, local_files_only=True)
    except Exception:
        print(f"   本地没有 {repo}，从网络下载（首次需要几分钟）...")
        path = snapshot_download(repo)
    # MLX 默认把释放的缓冲留在自己的缓存里复用：turbo 每轮识别后常驻 0.61GB
    # 不还给系统。上限设 0 实测每轮只慢 ~4%（2.36→2.45s），省下的和降一档
    # 8bit 差不多，而且不损准确度——内存压力正是 Mac 上降档的触发源
    _set_mlx_cache_limit(mx, 0)
    # 在后台加载阶段就把权重装进来：mlx_whisper 是第一次 transcribe 才
    # 惰性加载，否则"✅已就绪"之后第一轮识别会卡十几秒（M2 实测 17 秒）。
    # ModelHolder 按路径缓存，transcribe/detect_language 传同一个 path 才命中
    ModelHolder.get_model(path, mx.float16)
    return MlxWhisperModel(path)


class MlxWhisperModel:
    """Small compatibility wrapper for the faster-whisper calls this app uses."""

    def __init__(self, repo: str):
        self.repo = repo
        self.bits = 16
        # 默认不碰第三方模块属性；基准脚本显式打开后才记录分段耗时。
        self.profile_enabled = False
        self.last_timing = None

    @_on_mlx_thread
    def set_bits(self, bits: int) -> float:
        bits = int(bits)
        if bits not in (4, 8, 16):
            raise ValueError(f"unsupported Whisper bits: {bits}")
        if bits == self.bits:
            return 0.0

        import mlx.core as mx
        import mlx.nn as nn
        from mlx_whisper.transcribe import ModelHolder, load_model

        started = time.perf_counter()
        if self.bits == 16 and bits < 16:
            # MLX 的 nn.quantize 只会处理未量化层；fp16 向下可直接改
            # ModelHolder 里的同一份模型，transcribe/detect_language 继续按路径命中。
            model = ModelHolder.get_model(self.repo, mx.float16)
            nn.quantize(model, group_size=64, bits=bits)
            mx.eval(model.parameters())
        else:
            # 8bit→4bit 或升精度都不能在已量化层上再 quantize，必须从 fp16 重载。
            # ☠️ 先放掉旧模型再载新的：降档时内存本来就紧，先载后放会让峰值
            # 多出一整份（q8 0.87GB + fp16 1.6GB），把"警告"推成"严重"。
            # 这里是 ASR 线程的批边界，模型此刻没有别的使用者
            ModelHolder.model = None
            gc.collect()
            _clear_mlx_cache(mx)
            new_model = load_model(self.repo, dtype=mx.float16)
            if bits < 16:
                nn.quantize(new_model, group_size=64, bits=bits)
            mx.eval(new_model.parameters())
            # 保持 model_path == self.repo：transcribe/detect_language 按路径命中缓存
            ModelHolder.model = new_model
            ModelHolder.model_path = self.repo
        self.bits = bits
        gc.collect()
        _clear_mlx_cache(mx)
        return time.perf_counter() - started

    @_on_mlx_thread
    def transcribe(
        self,
        audio,
        language=None,
        task=None,
        initial_prompt=None,
        beam_size=None,
        word_timestamps=True,
        condition_on_previous_text=True,
        vad_filter=True,
        collect_timing=None,
    ):
        # mlx-whisper does not expose beam search; accept the faster-whisper
        # argument so callers can keep using the same configuration.
        del beam_size

        if collect_timing is None:
            collect_timing = self.profile_enabled
        started = time.perf_counter() if collect_timing else None
        vad_started = time.perf_counter() if collect_timing else None
        mlx_audio, ts_map = _apply_vad(audio, vad_filter=vad_filter)
        vad_s = time.perf_counter() - vad_started if vad_started is not None else 0.0
        if vad_filter and len(mlx_audio) == 0:
            if collect_timing:
                self.last_timing = _timing_record(time.perf_counter() - started, vad_s, None)
            return iter(()), SimpleNamespace(language=language or "", duration=0.0)

        import mlx_whisper

        with _MlxTranscribeContext(self.repo, collect_timing) as active_profiler:
            result = mlx_whisper.transcribe(
                mlx_audio,
                path_or_hf_repo=self.repo,
                language=language,
                task=task,
                initial_prompt=initial_prompt,
                word_timestamps=word_timestamps,
                condition_on_previous_text=condition_on_previous_text,
                # 温度回退最多到 0.2：默认 0.0→1.0 每档都把整段重解一遍，命中时单轮
                # 4~6 倍耗时（M4 实测 11.7s，正是"偶发卡顿"）。流式每轮本来就重识别
                # 整个缓冲、只提交两轮一致的前缀，不靠回退纠错。M4 上 187 轮对照：
                # WER 三者相同；上限 0.2 最慢 6.6s、p99 不变；完全不回退 p99 反而
                # 变差（重复解码一路生成到上限）
                temperature=(0.0, 0.2),
                verbose=None,
            )
        segments = [_segment_from_dict(s, ts_map) for s in result.get("segments", [])]
        if collect_timing:
            self.last_timing = _timing_record(time.perf_counter() - started, vad_s, active_profiler)
        info = SimpleNamespace(
            language=(result.get("language") or language or ""),
            duration=float(result.get("duration") or _duration_seconds(audio)),
        )
        return iter(segments), info

    @_on_mlx_thread
    def detect_language(self, audio, vad_filter=True):
        mlx_audio, _ts_map = _apply_vad(audio, vad_filter=vad_filter)
        if vad_filter and len(mlx_audio) == 0:
            return "", 0.0, {}

        import mlx.core as mx
        from mlx_whisper.audio import N_FRAMES, N_SAMPLES, log_mel_spectrogram, pad_or_trim
        from mlx_whisper.transcribe import ModelHolder

        # ☠️ 必须和 transcribe 共用 ModelHolder 里那一份：另 load_model 一份
        # 等于 turbo 再占 ~2GB 统一内存，正好和"按内存压力降档"对着干
        model = ModelHolder.get_model(self.repo, mx.float16)
        mel = log_mel_spectrogram(mlx_audio, n_mels=model.dims.n_mels, padding=N_SAMPLES)
        mel = pad_or_trim(mel, N_FRAMES, axis=-2).astype(mx.float16)
        # 返回 (语言 token 的 mx.array, {语言码: 概率})——第一个不是字符串，只用概率表
        _tokens, probs = model.detect_language(mel)
        all_probs = {str(k): float(v) for k, v in probs.items()}
        lang = max(all_probs, key=all_probs.get) if all_probs else ""
        prob = all_probs.get(lang, 0.0)
        return lang, prob, all_probs


def _apply_vad(audio, vad_filter: bool):
    if isinstance(audio, str):
        # 离线字幕传的是音频文件路径（faster-whisper 也接受路径）
        from faster_whisper.audio import decode_audio

        audio = decode_audio(audio, sampling_rate=SAMPLING_RATE)
    audio = np.asarray(audio, dtype=np.float32)
    if not vad_filter:
        return audio, None

    from faster_whisper.vad import (
        SpeechTimestampsMap,
        collect_chunks,
        get_speech_timestamps,
    )

    chunks = get_speech_timestamps(audio, sampling_rate=SAMPLING_RATE)
    if not chunks:
        return np.array([], dtype=np.float32), SpeechTimestampsMap([], SAMPLING_RATE)
    audio_chunks, _metadata = collect_chunks(audio, chunks, sampling_rate=SAMPLING_RATE)
    speech_audio = np.concatenate(audio_chunks).astype(np.float32, copy=False)
    return speech_audio, SpeechTimestampsMap(chunks, SAMPLING_RATE)


def _segment_from_dict(raw: dict, ts_map) -> MlxSegment:
    start = _map_time(float(raw.get("start", 0.0) or 0.0), ts_map)
    end = _map_time(float(raw.get("end", start) or start), ts_map, is_end=True)
    words = [_word_from_dict(w, ts_map) for w in raw.get("words", [])]
    return MlxSegment(
        start=start,
        end=end,
        text=str(raw.get("text", "") or ""),
        words=words,
        no_speech_prob=float(raw.get("no_speech_prob", 0.0) or 0.0),
        avg_logprob=float(raw.get("avg_logprob", 0.0) or 0.0),
    )


def _word_from_dict(raw: dict, ts_map) -> MlxWord:
    # 前导空格原样保留（同 faster-whisper）：中文等无空格语言的词本来就不带
    word = str(raw.get("word", "") or "")
    probability = raw.get("probability")
    return MlxWord(
        start=_map_time(float(raw.get("start", 0.0) or 0.0), ts_map),
        end=_map_time(float(raw.get("end", 0.0) or 0.0), ts_map, is_end=True),
        word=word,
        probability=None if probability is None else float(probability),
    )


def _map_time(value: float, ts_map, is_end: bool = False) -> float:
    if ts_map is None:
        return value
    return float(ts_map.get_original_time(value, is_end=is_end))


def _duration_seconds(audio) -> float:
    try:
        return len(audio) / SAMPLING_RATE
    except TypeError:
        return 0.0


def _clear_mlx_cache(mx) -> None:
    clear = getattr(mx, "clear_cache", None)
    if clear is None:
        clear = getattr(getattr(mx, "metal", None), "clear_cache", None)
    if clear is not None:
        clear()


def _set_mlx_cache_limit(mx, limit) -> None:
    setter = getattr(mx, "set_cache_limit", None) or mx.metal.set_cache_limit
    setter(limit)
