"""节点 VAD 分段器：用合成音频验证切段规则与 a0/a1 精度（不加载任何模型）。

假 VAD 按窗口 RMS 判语音，和 Silero 一样是 512 样本一窗，所以时间精度的
结论（误差 ≤ 一个窗口、不累积漂移）对真 VAD 同样成立。
"""
import numpy as np

from realtime_subtitle.node.segmenter import (
    MAX_SEGMENT_S,
    SAMPLE_RATE,
    WINDOW,
    Segmenter,
)

SR = SAMPLE_RATE


def energy_vad(window):
    return 1.0 if float(np.sqrt((window ** 2).mean())) > 0.02 else 0.0


def tone(seconds, amp=0.3):
    n = int(seconds * SR)
    t = np.arange(n) / SR
    return (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.int16)


def win(n):
    """n 个 VAD 窗口对应的秒数——让起止点落在窗口边界上，断言可以取等。"""
    return n * WINDOW / SR


def run(pcm, chunk=1600, vad=energy_vad):
    seg = Segmenter(vad)
    out = []
    for i in range(0, len(pcm), chunk):
        out.extend(seg.feed(pcm[i:i + chunk]))
    return seg, out


def test_single_burst_gets_exact_bounds():
    pcm = np.concatenate([silence(win(32)), tone(win(64)), silence(1.0)])
    _, out = run(pcm)
    assert len(out) == 1
    assert abs(out[0].a0 - win(32)) <= 0.01
    assert abs(out[0].a1 - win(96)) <= 0.01


def test_gap_shorter_than_600ms_does_not_split():
    # 0.5s 静音 < 0.6s：两段话连成一段
    pcm = np.concatenate([tone(1.0), silence(0.5), tone(1.0), silence(1.0)])
    _, out = run(pcm)
    assert len(out) == 1
    assert out[0].a1 - out[0].a0 > 2.4


def test_gap_longer_than_600ms_splits():
    pcm = np.concatenate([tone(1.0), silence(0.7), tone(1.0), silence(1.0)])
    _, out = run(pcm)
    assert len(out) == 2
    assert out[0].a1 <= out[1].a0
    assert 0.65 <= out[1].a0 - out[0].a1 <= 0.75


def test_segment_closes_only_after_silence_threshold():
    seg = Segmenter(energy_vad)
    assert seg.feed(tone(1.0)) == []
    assert seg.feed(silence(0.5)) == []
    assert len(seg.feed(silence(0.2))) == 1


def test_flush_closes_open_segment_with_speech_end_as_a1():
    seg = Segmenter(energy_vad)
    seg.feed(tone(win(64)))
    seg.feed(silence(0.2))
    out = seg.flush()
    assert len(out) == 1
    assert abs(out[0].a0 - 0.0) <= 0.01
    assert abs(out[0].a1 - win(64)) <= 0.01
    assert seg.flush() == []  # 已收尾，不重复吐


def test_flush_without_speech_is_empty():
    seg = Segmenter(energy_vad)
    seg.feed(silence(2.0))
    assert seg.flush() == []


def test_too_short_blip_is_dropped():
    pcm = np.concatenate([silence(1.0), tone(0.1), silence(1.0)])
    _, out = run(pcm)
    assert out == []


def test_audio_carries_padding_and_audio_t0():
    pcm = np.concatenate([silence(win(64)), tone(win(64)), silence(1.0)])
    _, out = run(pcm)
    s = out[0]
    # 前垫 0.1s、后垫 0.2s：喂给 Whisper 的音频比 a0/a1 略宽
    assert abs((s.a0 - s.audio_t0) - 0.1) <= 0.01
    assert abs(len(s.audio) / SR - ((s.a1 - s.a0) + 0.1 + 0.2)) <= 0.02
    assert s.audio.dtype == np.float32
    assert float(np.abs(s.audio).max()) <= 1.0


def test_force_cut_boundary_has_no_padding_on_either_side():
    dip_at = win(406)
    parts = [tone(dip_at), silence(0.1), tone(20.0 - dip_at - 0.1), silence(1.0)]
    _, out = run(np.concatenate(parts))
    first, second = out
    # 强切边界：前段不带后垫、后段不带前垫，音频首尾相接
    assert abs(first.audio_t0 + len(first.audio) / SR - first.a1) <= 1e-6
    assert abs(second.audio_t0 - second.a0) <= 1e-6
    # 自然静音边界仍然带垫：后段尾部有后垫
    assert abs(second.audio_t0 + len(second.audio) / SR - (second.a1 + 0.2)) <= 0.02


def test_force_cut_at_15s_picks_lowest_energy_point():
    # 20s 连续说话，13.0s 处有 0.1s 的低能量凹口（短于 0.6s，不会触发静音收尾）
    dip_at = win(406)  # ≈13.0s，落在 12–15s 的搜索区内
    parts = [tone(dip_at), silence(0.1), tone(20.0 - dip_at - 0.1), silence(1.0)]
    _, out = run(np.concatenate(parts))
    assert len(out) == 2
    first, second = out
    assert abs(first.a1 - (dip_at + 0.05)) <= 0.06
    assert first.a1 - first.a0 <= MAX_SEGMENT_S
    # 切点两侧首尾相接：不丢不重
    assert abs(second.a0 - first.a1) <= 0.01
    assert abs(second.a1 - 20.0) <= 0.05


def test_force_cut_never_exceeds_max_for_continuous_speech():
    _, out = run(np.concatenate([tone(50.0), silence(1.0)]))
    assert len(out) >= 4
    for s in out:
        assert s.a1 - s.a0 <= MAX_SEGMENT_S + 0.04
    for a, b in zip(out, out[1:]):
        assert abs(b.a0 - a.a1) <= 0.01
    assert abs(out[-1].a1 - 50.0) <= 0.05


def test_results_do_not_depend_on_chunk_size():
    pcm = np.concatenate([silence(0.7), tone(2.0), silence(0.8), tone(3.0), silence(1.0)])
    _, ref = run(pcm, chunk=1600)
    for chunk in (1, 333, 4097, 70000):
        _, got = run(pcm, chunk=chunk)
        assert [(s.a0, s.a1) for s in got] == [(s.a0, s.a1) for s in ref]


def test_no_clock_drift_over_many_segments():
    # 60 个「3s 说话 + 1s 静音」周期：第 60 段的 a0 仍然精确到窗口
    cycle = np.concatenate([tone(win(94)), silence(win(63))])
    n = 60
    _, out = run(np.concatenate([cycle] * n + [silence(1.0)]))
    assert len(out) == n
    period = win(94 + 63)
    assert abs(out[-1].a0 - (n - 1) * period) <= 0.01
    assert abs(out[-1].a1 - ((n - 1) * period + win(94))) <= 0.01


def test_buffer_does_not_grow_without_bound_when_idle():
    seg = Segmenter(energy_vad)
    for _ in range(600):  # 60s 静音
        seg.feed(silence(0.1))
    assert len(seg._audio) < SR  # 只留前垫
    assert seg.samples_received == 600 * 1600
