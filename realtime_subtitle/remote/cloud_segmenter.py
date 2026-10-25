"""云端分段：复用节点 Segmenter，只加「短于 1.5s 且间隔 <1.5s 就合并」。

产出的 pcm 从 a0 起、到 a1 止（int16，含中间那段真实间隔）。Groq 的
segments[].start/end 加上这个 a0 就是客户端样本时钟，不能再加前垫。
"""
from __future__ import annotations

from collections.abc import Callable

import numpy as np

from realtime_subtitle.node.segmenter import SAMPLE_RATE, Segmenter, SileroVad

# 等于 1.5s 不合并：短于、间隔小于都是严格小于。
MERGE_SHORT_S = 1.5
MERGE_GAP_S = 1.5
# 15s 强切 + 1.5s 间隔 + 收尾静音，再留窗口对齐的余量。已决定合并的前半段另外拷走。
_RETAIN_S = 22.0


class CloudSegmenter:
    """feed() 吃 int16 PCM，吐出已经可以上传的 (a0, a1, pcm)。"""

    def __init__(self, vad: Callable[[np.ndarray], float] | None = None):
        if vad is None:
            vad = SileroVad()
        self._segmenter = Segmenter(vad)
        self._pcm = np.zeros(0, dtype=np.int16)
        self._origin = 0
        self._held: tuple[float, float, np.ndarray] | None = None

    def feed(self, pcm: np.ndarray) -> list[tuple[float, float, np.ndarray]]:
        samples = _as_int16(pcm)
        self._append(samples)
        out: list[tuple[float, float, np.ndarray]] = []
        for seg in self._segmenter.feed(samples):
            out.extend(self._consider(seg.a0, seg.a1))
        out.extend(self._expire_held())
        self._trim()
        self._check_clock()
        return out

    def flush(self) -> list[tuple[float, float, np.ndarray]]:
        """流结束时短段不再等下一段，照样吐出。"""
        out: list[tuple[float, float, np.ndarray]] = []
        for seg in self._segmenter.flush():
            out.extend(self._consider(seg.a0, seg.a1))
        if self._held is not None:
            out.append(self._held)
            self._held = None
        self._trim()
        self._check_clock()
        return out

    def _consider(self, a0: float, a1: float) -> list[tuple[float, float, np.ndarray]]:
        pcm = self._slice(a0, a1)
        if self._held is None:
            if a1 - a0 < MERGE_SHORT_S:
                self._held = (a0, a1, pcm)
                return []
            return [(a0, a1, pcm)]
        h0, h1, hpcm = self._held
        if a0 - h1 < MERGE_GAP_S:
            merged = np.concatenate([hpcm, self._slice(h1, a1)])
            if a1 - h0 < MERGE_SHORT_S:
                self._held = (h0, a1, merged)
                return []
            self._held = None
            return [(h0, a1, merged)]
        self._held = None
        out = [(h0, h1, hpcm)]
        out.extend(self._consider(a0, a1))
        return out

    def _expire_held(self) -> list[tuple[float, float, np.ndarray]]:
        # 间隔已经走完 1.5s 还没有下一段，再等只会拖延迟。正在说话就先别放，
        # 这段话的起点若落在 1.5s 内，收尾时会并进来。
        if self._held is None or self._segmenter.in_speech:
            return []
        _h0, h1, _hpcm = self._held
        now = self._segmenter.samples_received / SAMPLE_RATE
        if now < h1 + MERGE_GAP_S:
            return []
        item = self._held
        self._held = None
        return [item]

    def _slice(self, a0: float, a1: float) -> np.ndarray:
        start = int(round(a0 * SAMPLE_RATE))
        end = int(round(a1 * SAMPLE_RATE))
        lo = start - self._origin
        hi = end - self._origin
        if lo < 0 or hi > len(self._pcm) or hi < lo:
            raise RuntimeError(f"segment audio missing a0={a0} a1={a1}")
        return self._pcm[lo:hi].copy()

    def _append(self, samples: np.ndarray) -> None:
        if len(samples) == 0:
            return
        if len(self._pcm) == 0:
            self._pcm = samples.copy()
        else:
            self._pcm = np.concatenate([self._pcm, samples])

    def _trim(self) -> None:
        keep = int(_RETAIN_S * SAMPLE_RATE)
        if len(self._pcm) <= keep:
            return
        drop = len(self._pcm) - keep
        self._pcm = self._pcm[drop:]
        self._origin += drop

    def _check_clock(self) -> None:
        # 缓冲下标必须和 Segmenter 的样本钟一致，否则 a0 切片会错位。
        if self._origin + len(self._pcm) != self._segmenter.samples_received:
            raise RuntimeError("sample clock diverged")


def _as_int16(pcm: np.ndarray) -> np.ndarray:
    arr = np.asarray(pcm)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if arr.dtype != np.int16:
        raise TypeError("pcm must be int16")
    return np.ascontiguousarray(arr)
