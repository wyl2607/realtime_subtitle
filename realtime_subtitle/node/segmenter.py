"""节点 worker 的 VAD 分段器：把连续 PCM 切成「一段一次识别」的语音段。

为什么单独成文件：分段规则（静音 ≥0.6s 收尾、单段 ≤15s 强制切）决定了字幕的
断点和延迟，要能用合成音频、不加载任何模型地测准。本模块只依赖 numpy；Silero
只在 `SileroVad` 里惰性 import，测试注入假 VAD 即可。

时间轴约定（P2）：a0/a1 是「从 hello 起算的音频秒数，按收到的样本数计算」，
所以这里全部用**绝对样本下标**记账，最后才除以采样率，不读任何墙钟。
a0 = 段内第一个判为语音的 VAD 窗口起点，a1 = 最后一个语音窗口终点（不含收尾
静音）；精度就是一个 VAD 窗口（512 样本 = 32ms 的整数倍），不会累积漂移。
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 16000
# Silero 在 16k 下只接受 512 样本的窗口
WINDOW = 512
SPEECH_THRESHOLD = 0.5
# RFC 架构选择第 2 条：静音 ≥0.6s 视为段落结束，单段最长 15s
SILENCE_END_S = 0.6
MAX_SEGMENT_S = 15.0
# 强制切只在段尾最后这几秒里找能量最低点：全段搜索可能把一句话切在第 1 秒，
# 剩下 14 秒又立刻超限；段尾搜索保证切点离 15s 上限不远，下一段有足够余量
CUT_SEARCH_S = 3.0
# 能量最低点用 20ms 帧的 RMS 找，再做 3 帧平滑，避免单个过零点被当成「静音」
CUT_FRAME = 320
# 太短的「语音」基本是点击声/爆音，送给 Whisper 只会吐幻觉
MIN_SPEECH_S = 0.15
# 送去识别的音频比 a0/a1 各多带一点：VAD 窗口起点偏晚会吃掉辅音头，
# 这里只影响喂给模型的音频，不影响汇报的 a0/a1
PRE_PAD_S = 0.1
POST_PAD_S = 0.2


@dataclass
class Segment:
    """一段待识别的语音。

    a0/a1：语音起止（hello 起算的秒）。
    audio_t0：`audio` 第 0 个样本对应的秒（= a0 - 前垫），Whisper 的词时间戳
    是相对 `audio` 的，换算回会话时间轴要加它。
    """
    a0: float
    a1: float
    audio: np.ndarray  # float32, [-1, 1]
    audio_t0: float


VadFn = Callable[[np.ndarray], float]


class SileroVad:
    """逐窗口流式 Silero VAD。

    不用 `SileroVADModel.__call__`：它每次调用都把 LSTM 状态清零，流式按 32ms
    一窗调用等于永远没有上下文。这里直接驱动它的 onnx session，自己持有
    h/c 和前一窗口的末尾 64 样本（模型要求的 context）。
    """

    def __init__(self):
        from faster_whisper.vad import get_vad_model

        self._session = get_vad_model().session
        self.reset()

    def reset(self) -> None:
        self._h = np.zeros((1, 1, 128), dtype=np.float32)
        self._c = np.zeros((1, 1, 128), dtype=np.float32)
        self._ctx = np.zeros(64, dtype=np.float32)

    def __call__(self, window: np.ndarray) -> float:
        x = np.concatenate([self._ctx, window])[None].astype(np.float32, copy=False)
        out, self._h, self._c = self._session.run(
            None, {"input": x, "h": self._h, "c": self._c})
        self._ctx = window[-64:].copy()
        return float(np.ravel(out)[0])


class Segmenter:
    """feed() 喂 s16le PCM，吐出已经收尾的 Segment。"""

    def __init__(self, vad: VadFn, sample_rate: int = SAMPLE_RATE,
                 silence_s: float = SILENCE_END_S, max_s: float = MAX_SEGMENT_S):
        self._vad = vad
        self._sr = sample_rate
        self._silence = int(silence_s * sample_rate)
        self._max = int(max_s * sample_rate)
        self._pre = int(PRE_PAD_S * sample_rate)
        self._post = int(POST_PAD_S * sample_rate)
        self._min_speech = int(MIN_SPEECH_S * sample_rate)
        self._audio = np.zeros(0, dtype=np.float32)
        self._off = 0       # _audio[0] 的绝对样本下标
        self._pos = 0       # 下一个待判窗口的绝对起点
        self._start: int | None = None      # 当前段第一个语音窗口的起点
        self._last_speech_end = 0           # 最后一个语音窗口的终点

    @property
    def samples_received(self) -> int:
        return self._off + len(self._audio)

    @property
    def in_speech(self) -> bool:
        return self._start is not None

    def feed(self, pcm: np.ndarray) -> list[Segment]:
        """pcm: int16 一维数组。返回这一批里收尾的段（通常 0 或 1 个）。"""
        self._audio = np.concatenate(
            [self._audio, pcm.astype(np.float32) / 32768.0])
        out: list[Segment] = []
        while self._pos + WINDOW <= self.samples_received:
            lo = self._pos - self._off
            speech = self._vad(self._audio[lo:lo + WINDOW]) >= SPEECH_THRESHOLD
            end = self._pos + WINDOW
            if speech:
                if self._start is None:
                    self._start = self._pos
                self._last_speech_end = end
            if self._start is not None:
                if not speech and end - self._last_speech_end >= self._silence:
                    out.extend(self._close())
                elif end - self._start >= self._max:
                    out.extend(self._force_cut(end))
            self._pos = end
            if self._start is None:
                self._trim(self._pos - self._pre)
        return out

    def flush(self) -> list[Segment]:
        """把正在进行的段立刻收尾（P2 flush/drain）。不足一个 VAD 窗口的尾巴不判。"""
        out = self._close() if self._start is not None else []
        self._trim(self._pos - self._pre)
        return out

    def _make(self, a0: int, a1: int) -> Segment | None:
        if a1 - a0 < self._min_speech:
            return None
        lo = max(self._off, a0 - self._pre)
        hi = min(self.samples_received, a1 + self._post)
        return Segment(
            a0=a0 / self._sr,
            a1=a1 / self._sr,
            audio=self._audio[lo - self._off:hi - self._off].copy(),
            audio_t0=lo / self._sr,
        )

    def _close(self) -> list[Segment]:
        seg = self._make(self._start, self._last_speech_end)
        self._start = None
        # 收尾后立刻丢掉这一段的音频，只留前垫：长会话不能无限涨内存
        self._trim(self._pos + WINDOW - self._pre)
        return [seg] if seg else []

    def _force_cut(self, now: int) -> list[Segment]:
        """段超长：在段尾 CUT_SEARCH_S 内能量最低处切开，剩余部分接着当新段。"""
        search = int(CUT_SEARCH_S * self._sr)
        lo = max(self._start + self._min_speech, now - search)
        region = self._audio[lo - self._off:now - self._off]
        n = len(region) // CUT_FRAME
        if n < 3:
            cut = now
        else:
            frames = region[:n * CUT_FRAME].reshape(n, CUT_FRAME)
            rms = np.sqrt((frames ** 2).mean(axis=1))
            smooth = np.convolve(rms, np.ones(3) / 3, mode="same")
            cut = lo + int(np.argmin(smooth)) * CUT_FRAME + CUT_FRAME // 2
        seg = self._make(self._start, cut)
        self._start = cut
        self._last_speech_end = max(self._last_speech_end, cut)
        self._trim(cut - self._pre)
        return [seg] if seg else []

    def _trim(self, keep_from: int) -> None:
        keep_from = max(keep_from, self._off)
        if keep_from > self._off:
            self._audio = self._audio[keep_from - self._off:]
            self._off = keep_from
