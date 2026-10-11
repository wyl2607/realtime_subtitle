"""云端分段：节点 Segmenter 的切点 + 短于 1.5s 的合并。不加载 Silero。"""
from pathlib import Path

import numpy as np
import pytest

from realtime_subtitle.node.segmenter import SAMPLE_RATE, WINDOW, Segmenter
from realtime_subtitle.remote import cloud_segmenter as cs


def energy_vad(window):
    return 1.0 if float(np.sqrt((window ** 2).mean())) > 0.02 else 0.0


def tone(seconds, amp=0.3):
    n = int(round(seconds * SAMPLE_RATE))
    t = np.arange(n) / SAMPLE_RATE
    return (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)


def silence(seconds):
    return np.zeros(int(round(seconds * SAMPLE_RATE)), dtype=np.int16)


def win(n):
    return n * WINDOW / SAMPLE_RATE


def raw_segments(pcm, chunk=1600):
    seg = Segmenter(energy_vad)
    out = []
    for i in range(0, len(pcm), chunk):
        out.extend(seg.feed(pcm[i:i + chunk]))
    out.extend(seg.flush())
    return out


def run(pcm, chunk=1600):
    segmenter = cs.CloudSegmenter(energy_vad)
    out = []
    for i in range(0, len(pcm), chunk):
        out.extend(segmenter.feed(pcm[i:i + chunk]))
    out.extend(segmenter.flush())
    return out


def _slice(pcm, a0, a1):
    i0 = int(round(a0 * SAMPLE_RATE))
    i1 = int(round(a1 * SAMPLE_RATE))
    return pcm[i0:i1]


def test_reuses_node_segmenter_without_copying_it():
    from realtime_subtitle.node import segmenter as node_segmenter

    assert cs.Segmenter is node_segmenter.Segmenter
    assert cs.SileroVad is node_segmenter.SileroVad
    source = Path(cs.__file__).read_text(encoding="utf-8")
    assert "def _force_cut" not in source
    assert "def _close" not in source
    assert "onnx" not in source


def test_injected_vad_does_not_construct_silero(monkeypatch):
    def boom(*_args, **_kwargs):
        raise AssertionError("SileroVad should not be constructed")

    monkeypatch.setattr(cs, "SileroVad", boom)
    cs.CloudSegmenter(vad=energy_vad)


def test_default_vad_is_silero(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(cs, "SileroVad", lambda: sentinel)
    segmenter = cs.CloudSegmenter()
    assert segmenter._segmenter._vad is sentinel


def test_long_segment_matches_node_segmenter_and_pcm_starts_at_a0():
    pcm = np.concatenate([silence(win(32)), tone(win(80)), silence(1.0)])
    raw = raw_segments(pcm)
    out = run(pcm)
    assert len(raw) == 1
    assert len(out) == 1
    a0, a1, audio = out[0]
    assert a0 == raw[0].a0
    assert a1 == raw[0].a1
    assert audio.dtype == np.int16
    assert audio.tobytes() == _slice(pcm, a0, a1).tobytes()
    assert a1 - a0 >= 1.5


def test_short_segments_closer_than_1_5s_merge_including_the_gap():
    pcm = np.concatenate([
        tone(win(40)),
        silence(win(20)),
        tone(win(40)),
        silence(1.0),
    ])
    raw = raw_segments(pcm)
    assert len(raw) == 2
    assert raw[0].a1 - raw[0].a0 < 1.5
    assert raw[1].a0 - raw[0].a1 < 1.5
    out = run(pcm)
    assert len(out) == 1
    a0, a1, audio = out[0]
    assert a0 == raw[0].a0
    assert a1 == raw[1].a1
    assert audio.tobytes() == _slice(pcm, a0, a1).tobytes()


def test_three_short_segments_keep_merging_until_the_span_reaches_1_5s():
    pcm = np.concatenate([
        tone(win(10)),
        silence(win(19)),
        tone(win(10)),
        silence(win(19)),
        tone(win(10)),
        silence(1.0),
    ])
    raw = raw_segments(pcm)
    assert len(raw) == 3
    assert all(seg.a1 - seg.a0 < 1.5 for seg in raw)
    assert raw[1].a0 - raw[0].a1 < 1.5
    out = run(pcm)
    assert len(out) == 1
    a0, a1, audio = out[0]
    assert a0 == raw[0].a0
    assert a1 == raw[-1].a1
    assert a1 - a0 >= 1.5
    assert audio.tobytes() == _slice(pcm, a0, a1).tobytes()


def test_gap_of_1_5s_or_more_does_not_merge():
    pcm = np.concatenate([
        tone(win(40)),
        silence(win(47)),
        tone(win(40)),
        silence(2.0),
    ])
    raw = raw_segments(pcm)
    assert len(raw) == 2
    assert raw[1].a0 - raw[0].a1 >= 1.5
    out = run(pcm)
    assert [(a0, a1) for a0, a1, _audio in out] == [(seg.a0, seg.a1) for seg in raw]
    for (a0, a1, audio), seg in zip(out, raw, strict=True):
        assert audio.tobytes() == _slice(pcm, seg.a0, seg.a1).tobytes()


def test_short_segment_without_a_neighbour_is_still_emitted():
    pcm = np.concatenate([tone(win(40)), silence(0.7)])
    segmenter = cs.CloudSegmenter(energy_vad)
    assert segmenter.feed(pcm) == []
    out = segmenter.flush()
    raw = raw_segments(pcm)
    assert len(out) == 1
    assert (out[0][0], out[0][1]) == (raw[0].a0, raw[0].a1)
    assert out[0][2].tobytes() == _slice(pcm, raw[0].a0, raw[0].a1).tobytes()


def test_short_segment_expires_once_the_gap_reaches_1_5s_without_flush():
    segmenter = cs.CloudSegmenter(energy_vad)
    first = np.concatenate([tone(win(40)), silence(0.7)])
    assert segmenter.feed(first) == []
    out = segmenter.feed(silence(1.0))
    assert len(out) == 1
    assert out[0][1] - out[0][0] < 1.5


def test_short_segment_merges_into_the_following_long_one():
    pcm = np.concatenate([
        tone(win(40)),
        silence(win(20)),
        tone(win(430)),
        silence(1.0),
    ])
    raw = raw_segments(pcm)
    assert len(raw) == 2
    assert raw[0].a1 - raw[0].a0 < 1.5
    assert raw[1].a1 - raw[1].a0 > 1.5
    assert raw[1].a0 - raw[0].a1 < 1.5
    out = run(pcm)
    assert len(out) == 1
    a0, a1, audio = out[0]
    assert (a0, a1) == (raw[0].a0, raw[1].a1)
    assert audio.tobytes() == _slice(pcm, a0, a1).tobytes()


def test_force_cut_pieces_stay_separate():
    pcm = np.concatenate([tone(20.0), silence(1.0)])
    raw = raw_segments(pcm)
    out = run(pcm)
    assert len(raw) >= 2
    assert [(a0, a1) for a0, a1, _audio in out] == [(seg.a0, seg.a1) for seg in raw]
    for a0, a1, audio in out:
        assert a1 - a0 <= 15.04
        assert audio.tobytes() == _slice(pcm, a0, a1).tobytes()


def test_results_do_not_depend_on_chunk_size():
    pcm = np.concatenate([
        silence(0.7),
        tone(win(40)),
        silence(win(20)),
        tone(win(40)),
        silence(1.0),
    ])
    ref = [(a0, a1, audio.tobytes()) for a0, a1, audio in run(pcm, chunk=1600)]
    for chunk in (1, 333, 4097, 70000):
        got = [(a0, a1, audio.tobytes()) for a0, a1, audio in run(pcm, chunk=chunk)]
        assert got == ref


def test_blip_shorter_than_the_node_minimum_is_dropped():
    pcm = np.concatenate([silence(1.0), tone(0.1), silence(1.0)])
    assert run(pcm) == []
    assert raw_segments(pcm) == []


def test_idle_buffer_stays_bounded_while_holding_then_expiring():
    segmenter = cs.CloudSegmenter(energy_vad)
    assert segmenter.feed(np.concatenate([tone(win(40)), silence(0.7)])) == []
    emitted = []
    for _ in range(400):
        emitted.extend(segmenter.feed(silence(0.1)))
    assert len(emitted) == 1
    assert len(segmenter._pcm) <= int(cs._RETAIN_S * SAMPLE_RATE) + WINDOW


def test_feed_rejects_non_int16():
    segmenter = cs.CloudSegmenter(energy_vad)
    with pytest.raises(TypeError):
        segmenter.feed(np.zeros(1600, dtype=np.float32))
