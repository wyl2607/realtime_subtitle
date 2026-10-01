"""只生成合成音频，不需要 ScreenCaptureKit 权限或任何真实系统声音。"""
import json
import os
from pathlib import Path
import sys
import time

import numpy as np
import pytest

from realtime_subtitle import config
from realtime_subtitle import capture
from realtime_subtitle.capture import macos_capture as mac


_FAKE_HELPER = r'''
import json, math, os, pathlib, struct, sys, time
mode, marker = sys.argv[1:]
marker = pathlib.Path(marker)
with marker.open('a') as f:
    f.write(str(os.getpid()) + '\n')
if mode == 'permission':
    print('permission denied', file=sys.stderr, flush=True)
    sys.exit(2)
if mode == 'crash' and len(marker.read_text().splitlines()) <= 2:
    sys.exit(1)
if mode == 'hang':
    time.sleep(60)
    sys.exit(0)
header = json.dumps({'sample_rate': 48000, 'channels': 2, 'format': 'f32le'}).encode() + b'\n'
# 故意拆开协议头，让读取端不能假设一次 read 就得到一整行。
for b in header:
    os.write(1, bytes([b]))
if mode == 'stderr':
    os.write(2, b'e' * 100000)
frames = 4800
for block in range(600):
    quiet = mode == 'silence' or (mode == 'tail' and block >= 2)
    samples = []
    for i in range(frames):
        value = 0 if quiet else 0.4 * math.sin(2 * math.pi * 440 * (block * frames + i) / 48000)
        samples.extend((value, value * 0.5))
    data = struct.pack('<' + 'f' * len(samples), *samples)
    # 单次 pipe write 可以只写一部分；同时用非帧对齐的切点验证接收端拼接。
    for part in (data[:123], data[123:]):
        while part:
            n = os.write(1, part)
            part = part[n:]
    time.sleep(0.02)
'''


def _wait_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    assert predicate(), "等待采集条件超时"


@pytest.fixture
def settings(monkeypatch, tmp_path):
    monkeypatch.setattr(config, "SAMPLE_RATE", 16000)
    monkeypatch.setattr(config, "CHUNK_SIZE", 4800)
    monkeypatch.setattr(config, "CHUNK_SUBMIT_SECONDS", 0.1)
    monkeypatch.setattr(config, "ENERGY_THRESHOLD_SPEECH", 0.01)
    monkeypatch.setattr(config, "LOOPBACK_DEVICE_NAME", "")
    monkeypatch.setattr(mac, "PAUSE_FLAG_FILE", str(tmp_path / ".paused"))
    monkeypatch.setattr(mac, "STOP_FLAG_FILE", str(tmp_path / ".stop"))


@pytest.fixture
def fake_helper(settings, monkeypatch, tmp_path):
    if sys.platform == "win32":
        pytest.skip("Windows 的匿名管道不支持 select；macOS 子进程行为在 POSIX 测试")
    script = tmp_path / "fake_helper.py"
    script.write_text(_FAKE_HELPER, encoding="utf-8")
    marker = tmp_path / "children.txt"
    instances = []

    def make(mode="sine", callback=None):
        output, statuses = [], []
        cap = mac.MacOSAudioCapture(callback or (lambda audio, timestamp: output.append((audio, timestamp))), statuses.append)
        monkeypatch.setattr(cap, "_helper_command", lambda: [sys.executable, str(script), mode, str(marker)])
        cap.RESTART_INITIAL = 0.05
        cap.RESTART_MAX = 0.2
        cap.PAUSE_CHECK_INTERVAL = 0.01
        instances.append(cap)
        return cap, output, statuses, marker

    yield make
    for cap in instances:
        cap.stop()


@pytest.mark.parametrize("rate,channels", [(48000, 2), (16000, 1), (96000, 6)])
def test_header_parsing(rate, channels):
    line = json.dumps(dict(sample_rate=rate, channels=channels, format="f32le")).encode()
    assert mac.MacOSAudioCapture._parse_header(line) == (rate, channels)


@pytest.mark.parametrize("header", [b"bad", b"[]", b"null", b"\xff", b"{}",
    b'{"sample_rate":48000,"channels":0,"format":"f32le"}',
    b'{"sample_rate":48000,"channels":true,"format":"f32le"}',
    b'{"sample_rate":1,"channels":2,"format":"f32le"}',
    b'{"sample_rate":48000,"channels":2,"format":"s16le"}'])
def test_rejects_invalid_header(header):
    with pytest.raises(ValueError, match="协议头无效"):
        mac.MacOSAudioCapture._parse_header(header)


def test_downmix_averages_every_channel():
    data = np.array([[0.3, -0.1, 0.4], [-0.6, 0.2, 0.1]], dtype="<f4")
    output = mac.MacOSAudioCapture._downmix(data.tobytes(), 3)
    np.testing.assert_allclose(output, data.mean(axis=1))
    assert output.dtype == np.float32


def test_downmix_mono_passthrough():
    data = np.array([0.1, -0.2], dtype="<f4")
    np.testing.assert_array_equal(mac.MacOSAudioCapture._downmix(data.tobytes(), 1), data)


def test_helper_path_and_application_filter(settings, monkeypatch, tmp_path):
    helper = tmp_path / "sc-audio-tap"
    helper.touch()
    monkeypatch.setattr(config, "MAC_AUDIO_HELPER", str(helper), raising=False)
    monkeypatch.setattr(config, "MAC_AUDIO_BUNDLE_IDS", ["org.example.a", "org.example.b"], raising=False)
    cap = mac.MacOSAudioCapture(None)
    assert cap._helper_command() == [str(helper), "--bundle-ids", "org.example.a,org.example.b"]


def test_missing_helper_has_build_instruction(settings, monkeypatch, tmp_path):
    monkeypatch.setattr(config, "MAC_AUDIO_HELPER", str(tmp_path / "missing"), raising=False)
    messages = []
    cap = mac.MacOSAudioCapture(None, messages.append)
    with pytest.raises(FileNotFoundError, match="swift build -c release"):
        cap.start()
    assert "swift build -c release" in messages[0]
    assert not cap.running


@pytest.mark.parametrize("platform", ["darwin", "win32"])
def test_factory_selects_backend(settings, monkeypatch, platform):
    monkeypatch.setattr(sys, "platform", platform)
    callback, status = lambda *a: None, lambda *a: None
    cap = capture.make_audio_capture(callback, status)
    expected = mac.MacOSAudioCapture if platform == "darwin" else capture.AudioCapture
    assert isinstance(cap, expected)
    assert cap.callback is callback and cap.on_status is status


def test_factory_rejects_unsupported_platform(settings, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="不支持"):
        capture.make_audio_capture(None)


def test_fake_helper_resamples_and_preserves_sine_and_end_timestamp(fake_helper):
    cap, output, _, _ = fake_helper()
    before = time.time()
    cap.start()
    _wait_until(lambda: len(output) >= 4)
    cap.stop()
    audio, timestamp = output[2]
    assert 1600 <= len(audio) < 3200
    spectrum = abs(np.fft.rfft(audio))
    frequency = np.fft.rfftfreq(len(audio), 1 / 16000)[spectrum.argmax()]
    assert abs(frequency - 440) < 10
    assert np.sqrt(np.mean(audio ** 2)) == pytest.approx(0.3 / np.sqrt(2), abs=0.005)
    assert before <= timestamp <= time.time()
    assert audio.dtype == np.float32 and audio.ndim == 1


def test_silence_gate_drops_silent_periods(fake_helper):
    cap, output, statuses, _ = fake_helper("silence")
    cap.start()
    _wait_until(lambda: any("正在捕获" in s for s in statuses))
    _wait_until(lambda: cap._silent_periods >= 3)
    cap.stop()
    assert output == []


def test_silence_gate_keeps_one_tail_period(fake_helper):
    cap, output, _, _ = fake_helper("tail")
    cap.start()
    _wait_until(lambda: cap._silent_periods >= 3)
    cap.stop()
    assert len(output) >= 2
    assert np.sqrt(np.mean(output[-1][0] ** 2)) < config.ENERGY_THRESHOLD_SPEECH
    assert any(np.sqrt(np.mean(audio ** 2)) > config.ENERGY_THRESHOLD_SPEECH for audio, _ in output[:-1])


def test_pause_flag_discards_audio_and_resumes_with_fresh_resampler(fake_helper):
    Path(mac.PAUSE_FLAG_FILE).touch()
    cap, output, _, _ = fake_helper()
    cap.start()
    _wait_until(lambda: cap._paused)
    first = cap._resampler
    _wait_until(lambda: cap._resampler is not first)
    assert output == []
    first = None  # ☠️ 测试本身也不要把 nanobind 对象留到解释器卸载。
    Path(mac.PAUSE_FLAG_FILE).unlink()
    _wait_until(lambda: len(output) >= 2)
    cap.stop()


def test_crash_restarts_with_backoff_and_status(fake_helper):
    cap, output, statuses, marker = fake_helper("crash")
    cap.start()
    _wait_until(lambda: len(output) >= 1)
    cap.stop()
    assert len(marker.read_text().splitlines()) == 3
    failures = [s for s in statuses if "重新连接" in s]
    assert len(failures) == 2
    assert "0.05 秒" in failures[0] and "0.1 秒" in failures[1]


def test_permission_denied_is_terminal_and_reports_exact_hint(fake_helper):
    cap, _, statuses, marker = fake_helper("permission")
    cap.start()
    _wait_until(lambda: not cap.running)
    cap.stop()
    assert statuses == [mac.PERMISSION_HINT]
    assert len(marker.read_text().splitlines()) == 1
    assert cap._helper is None and not cap.capture_thread.is_alive()


def test_stderr_is_drained_while_pcm_is_read(fake_helper, capsys):
    cap, output, _, _ = fake_helper("stderr")
    cap.start()
    _wait_until(lambda: len(output) >= 2)
    cap.stop()
    assert "[sc-audio-tap]" in capsys.readouterr().out


@pytest.mark.parametrize("mode", ["sine", "hang"])
def test_stop_reaps_child_even_without_pcm(fake_helper, mode):
    cap, _, _, marker = fake_helper(mode)
    cap.start()
    _wait_until(lambda: marker.exists())
    proc = cap._helper
    assert proc is not None and proc.poll() is None
    cap.stop()
    assert proc.poll() is not None
    assert cap._helper is None and cap._resampler is None
    assert not cap.capture_thread.is_alive() and not cap.process_thread.is_alive()
    with pytest.raises(ProcessLookupError):
        os.kill(proc.pid, 0)
    cap.stop()  # 幂等，再停不会碰到已回收的 PID。


def test_stop_flag_closes_helper_while_startup_is_stalled(fake_helper):
    cap, _, _, marker = fake_helper("hang")
    cap.start()
    _wait_until(lambda: marker.exists())
    proc = cap._helper
    Path(mac.STOP_FLAG_FILE).touch()
    _wait_until(lambda: not cap.running and cap._helper is None)
    cap.stop()
    assert proc.poll() is not None
    assert Path(mac.STOP_FLAG_FILE).exists()  # 由 app 消费和移除。


def test_header_timeout_restarts_and_can_be_stopped(fake_helper):
    cap, _, statuses, marker = fake_helper("hang")
    cap.HEADER_TIMEOUT = 0.1
    cap.start()
    _wait_until(lambda: any("启动超时" in s for s in statuses))
    cap.stop()
    assert len(marker.read_text().splitlines()) >= 1
    assert cap._helper is None
