"""MLX Whisper adapter that presents the faster-whisper model surface."""
from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace

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


class MlxWhisperModel:
    """Small compatibility wrapper for the faster-whisper calls this app uses."""

    def __init__(self, repo: str):
        self.repo = repo

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
