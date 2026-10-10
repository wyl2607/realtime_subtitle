import json
import struct
import sys
import wave
from pathlib import Path

import pytest

# 探针 import 了真 gateway；缺 websockets 的环境直接跳过，别往 sys.modules 塞假模块——
# 那会污染同一进程里后收集的 test_node_gateway 等用例（10-10 全量 pytest 收集即报错）
pytest.importorskip("websockets.asyncio.server")

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.bench import multi_node_probe, multi_node_verify


def _write_multi_silence_wav(path: Path) -> Path:
    sr = 100
    duration = 92.0
    silence_ranges = [(10.0, 10.8), (57.2, 58.35), (59.95, 61.1)]
    samples = []
    for i in range(int(sr * duration)):
        t = i / sr
        silent = any(start <= t < end for start, end in silence_ranges)
        samples.append(0 if silent else (10000 if i % 2 else -10000))
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sr)
        wf.writeframes(struct.pack(f"<{len(samples)}h", *samples))
    return path


def _route_logs():
    return "\n".join([
        "rslite.route select node=node-a score=50.0 quality=60.0 speed=-10.0 penalties=0.0 bonus=0.0 rtt_ms=9",
        "rslite.route select node=node-b score=59.6 quality=60.0 speed=-0.4 penalties=0.0 bonus=0.0 rtt_ms=2",
        "rslite.route select node=node-b score=59.6 quality=60.0 speed=-0.4 penalties=0.0 bonus=0.0 rtt_ms=1",
        "rslite.route fallback failed_node=node-b offline_for_s=60 err=NSURLError",
        "rslite.route select node=node-a score=50.0 quality=60.0 speed=-10.0 penalties=0.0 bonus=0.0 rtt_ms=4",
    ])


def _stdout(first_b_t0=57.8, local_overlap=False):
    prev_a_t1 = first_b_t0
    events = [
        {"ev": "status", "t": 0.2, "text": "模式：混合·node-a"},
        {"ev": "final", "t": 52.1, "t0": 0.0, "t1": 52.0, "text": "[node-a] A-seg1"},
        {"ev": "final", "t": 62.2, "t0": 52.0, "t1": prev_a_t1, "text": "[node-a] A-seg2"},
        {"ev": "status", "t": 67.3, "text": "模式：精修中"},
        {"ev": "replace", "t": 67.3, "t0": first_b_t0, "t1": first_b_t0 + 5.0, "text": "P5 node=node-b replaced=0 ids=[]"},
        {"ev": "final", "t": 67.31, "t0": first_b_t0, "t1": first_b_t0 + 5.0, "text": "[node-b] B-seg1"},
        {"ev": "translation", "t": 67.31, "text": "[node-b] B-zh1"},
        {"ev": "status", "t": 67.31, "text": "模式：混合·node-b"},
        {"ev": "final", "t": 72.6, "t0": first_b_t0 + 5.0, "t1": first_b_t0 + 10.0, "text": "[node-b] B-seg2"},
        {"ev": "status", "t": 72.6, "text": "模式：混合·node-b"},
        {"ev": "final", "t": 77.9, "t0": first_b_t0 + 10.0, "t1": first_b_t0 + 15.0, "text": "[node-b] B-seg3"},
        {"ev": "status", "t": 77.9, "text": "模式：混合·node-b"},
        {"ev": "final", "t": 83.1, "t0": first_b_t0 + 15.0, "t1": first_b_t0 + 20.0, "text": "[node-b] B-seg4"},
        {"ev": "status", "t": 83.1, "text": "模式：混合·node-b"},
        {"ev": "final", "id": 23, "t": 85.264, "t0": 77.58, "t1": 78.54, "text": "local fallback clock anchor"},
        {"ev": "status", "t": 86.724, "text": "模式：本机"},
        {"ev": "status", "t": 86.9, "text": "模式：混合·node-a"},
        {"ev": "final", "t": 92.0, "t0": 80.0, "t1": 92.0, "text": "[node-a] A-seg3"},
        {"ev": "final", "id": 99, "t": 93.0, "text": "local id sentinel"},
    ]
    if local_overlap:
        events[-2] = {"ev": "final", "t": 88.0, "t0": 80.0, "t1": 88.0, "text": "[node-a] A-seg3"}
        events.extend([
            {"ev": "final", "id": 100, "t": 88.2, "t0": 88.1, "t1": 90.0, "text": "local overlap one"},
            {"ev": "final", "id": 101, "t": 88.3, "t0": 88.5, "t1": 91.0, "text": "local overlap two"},
        ])
    return "\n".join(json.dumps(e) for e in events)


def _verify(tmp_path, *, stdout_text=None, stderr_text=None, gateway_log="", drained_events=None, rc=0):
    wav_file = _write_multi_silence_wav(tmp_path / "multi.wav")
    return multi_node_verify.verify_probe(
        stdout_text=stdout_text if stdout_text is not None else _stdout(),
        stderr_text=stderr_text if stderr_text is not None else _route_logs(),
        gateway_log=gateway_log,
        drained_events=drained_events if drained_events is not None else [{"ev": "drained", "audio_s": 57.6}],
        wav_file=str(wav_file),
        rc=rc,
        token_a="token_a",
        token_b="token_b",
    )


def test_fake_worker_hello_resets_session_clock_and_ids():
    script = multi_node_probe.make_fake_worker("A")
    hello_block = script.split('if t == "hello":', 1)[1].split('elif t == "flush":', 1)[0]
    assert "total = 0" in hello_block
    assert "seg_start = 0" in hello_block
    assert "seg_id = 0" in hello_block
    # 文本计数器跨会话保留：同一 worker 进程被复用时，清零会让 A→B→A 后节点句文字重复
    assert "global_seg = 0" not in hello_block


def test_silence_fixture_has_distinct_windows_and_rejects_bad_mapping(tmp_path):
    wav_file = _write_multi_silence_wav(tmp_path / "multi.wav")
    windows, _duration = multi_node_verify.find_silence_windows(wav_file)
    assert len(windows) >= 3
    assert multi_node_verify.silence_window_for(57.8, windows) == pytest.approx((57.2, 58.35))
    assert multi_node_verify.silence_window_for(56.3, windows) is None


def test_verify_probe_success(tmp_path):
    assert _verify(tmp_path) == 0


def test_verify_probe_rejects_wall_clock_style_migration_mapping(tmp_path, capsys):
    assert _verify(tmp_path, stdout_text=_stdout(first_b_t0=56.3)) == 1
    assert "Migration audio switch" in capsys.readouterr().out


def test_verify_probe_rejects_token_leak(tmp_path, capsys):
    assert _verify(tmp_path, gateway_log="leaked token_b") == 1
    assert "Tokens are absent from gateway log" in capsys.readouterr().out


def test_verify_probe_rejects_visible_local_overlap(tmp_path, capsys):
    assert _verify(tmp_path, stdout_text=_stdout(local_overlap=True)) == 1
    assert "Local sentences do not overlap" in capsys.readouterr().out
