"""MLX Whisper adapter that presents the faster-whisper model surface."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
import gc
import time

import numpy as np


SAMPLING_RATE = 16000


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
    ):
        # mlx-whisper does not expose beam search; accept the faster-whisper
        # argument so callers can keep using the same configuration.
        del beam_size

        mlx_audio, ts_map = _apply_vad(audio, vad_filter=vad_filter)
        if vad_filter and len(mlx_audio) == 0:
            return iter(()), SimpleNamespace(language=language or "", duration=0.0)

        import mlx_whisper

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
        info = SimpleNamespace(
            language=(result.get("language") or language or ""),
            duration=float(result.get("duration") or _duration_seconds(audio)),
        )
        return iter(segments), info

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

