"""自适应静音阈值：长段更快收尾，短段仍保持旧的稳健阈值。"""
import numpy as np

from realtime_subtitle.node.segmenter import (
    MAX_SEGMENT_S,
    SAMPLE_RATE,
    SILENCE_END_S,
    WINDOW,
    Segmenter,
)
from tests.test_node_segmenter import energy_vad, silence, tone, win

SR = SAMPLE_RATE


def run(pcm, *, adaptive=True, chunk=1600):
    seg = Segmenter(energy_vad, adaptive=adaptive)
    out = []
    for i in range(0, len(pcm), chunk):
        out.extend(seg.feed(pcm[i:i + chunk]))
    return seg, out


def test_adaptive_keeps_short_segments_on_full_silence_threshold():
    seg = Segmenter(energy_vad)
    assert seg.feed(tone(1.0)) == []
    assert seg.feed(silence(0.3)) == []
    assert seg.in_speech


def test_adaptive_splits_long_segments_after_300ms_pause():
    seg = Segmenter(energy_vad)
    assert seg.feed(tone(5.1)) == []
    out = seg.feed(silence(0.3))
    assert len(out) == 1
    assert abs(out[0].a1 - 5.1) <= 0.05


def test_adaptive_interpolates_mid_segment_threshold():
    seg = Segmenter(energy_vad)
    assert seg.feed(tone(win(109))) == []  # ~3.49s -> threshold ~0.426s.
    assert seg.feed(silence(0.4)) == []
    out = seg.feed(silence(0.05))
    assert len(out) == 1


def test_adaptive_false_keeps_fixed_silence_behavior_frame_by_frame():
    # 回退模式保持旧固定阈值：长段里的 0.3s 停顿不收尾，直到累计静音达到 0.6s。
    seg = Segmenter(energy_vad, adaptive=False)
    frames = np.concatenate([tone(6.0), silence(0.3), tone(1.0), silence(1.0)])
    emitted_at = []
    out = []
    for frame_idx, i in enumerate(range(0, len(frames), WINDOW)):
        batch = seg.feed(frames[i:i + WINDOW])
        if batch:
            emitted_at.append(frame_idx)
            out.extend(batch)

    assert len(out) == 1
    assert emitted_at[0] >= int((6.0 + 0.3 + 1.0 + SILENCE_END_S) * SR / WINDOW) - 1
    assert abs(out[0].a0 - 0.0) <= 0.01
    assert abs(out[0].a1 - 7.3) <= 0.05


def test_continuous_20s_speech_still_force_cuts_at_max_s():
    _, out = run(np.concatenate([tone(20.0), silence(1.0)]))
    assert len(out) == 2
    assert out[0].a1 - out[0].a0 <= MAX_SEGMENT_S + WINDOW / SR
    assert abs(out[1].a0 - out[0].a1) <= 0.01
    assert abs(out[-1].a1 - 20.0) <= 0.05
