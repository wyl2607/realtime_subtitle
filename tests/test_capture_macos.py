import pytest

from realtime_subtitle import config
from realtime_subtitle.capture import audio_capture
from realtime_subtitle.capture.audio_capture import AudioCapture


class _FakeMacPyAudio:
    def __init__(self, devices, default_index=0):
        self.devices = devices
        self.default_index = default_index

    def get_default_input_device_info(self):
        return self.devices[self.default_index]

    def get_input_device_info_generator(self):
        for dev in self.devices:
            if dev.get("maxInputChannels", 0) > 0:
                yield dev


@pytest.fixture(autouse=True)
def _mac_platform(monkeypatch):
    monkeypatch.setattr(audio_capture.sys, "platform", "darwin")
    monkeypatch.setattr(AudioCapture, "_missing_warned", None, raising=False)
    monkeypatch.setattr(AudioCapture, "_mac_default_warned", None, raising=False)
    monkeypatch.setattr(config, "LOOPBACK_DEVICE_NAME", "", raising=False)


def _dev(index, name, channels=2, rate=48000):
    return {
        "name": name,
        "index": index,
        "defaultSampleRate": rate,
        "maxInputChannels": channels,
    }


def _warnings(capsys):
    return [ln for ln in capsys.readouterr().out.splitlines() if "macOS 不能直接抓系统声音" in ln]


def test_macos_prefers_blackhole_when_no_device_name(capsys):
    p = _FakeMacPyAudio([
        _dev(0, "MacBook Pro Microphone", 1),
        _dev(1, "BlackHole 2ch", 2),
    ])

    assert AudioCapture._resolve_loopback(p)["name"] == "BlackHole 2ch"
    assert _warnings(capsys) == []


def test_macos_uses_loopback_device_name_as_input_substring(capsys, monkeypatch):
    monkeypatch.setattr(config, "LOOPBACK_DEVICE_NAME", "scarlett", raising=False)
    p = _FakeMacPyAudio([
        _dev(0, "MacBook Pro Microphone", 1),
        _dev(1, "Focusrite Scarlett 2i2", 2),
        _dev(2, "BlackHole 2ch", 2),
    ])

    assert AudioCapture._resolve_loopback(p)["name"] == "Focusrite Scarlett 2i2"
    assert _warnings(capsys) == []


def test_macos_falls_back_to_default_input_and_warns_once(capsys):
    p = _FakeMacPyAudio([
        _dev(0, "MacBook Pro Microphone", 1),
        _dev(1, "USB Webcam Output Only", 0),
    ])

    for _ in range(5):
        assert AudioCapture._resolve_loopback(p)["name"] == "MacBook Pro Microphone"

    warnings = _warnings(capsys)
    assert len(warnings) == 1
    assert "BlackHole" in warnings[0]
    assert "多输出设备" in warnings[0]


def test_macos_missing_named_input_falls_back_to_default_and_warns_once(capsys, monkeypatch):
    monkeypatch.setattr(config, "LOOPBACK_DEVICE_NAME", "does-not-exist", raising=False)
    p = _FakeMacPyAudio([
        _dev(0, "MacBook Pro Microphone", 1),
        _dev(1, "BlackHole 2ch", 2),
    ])

    for _ in range(5):
        assert AudioCapture._resolve_loopback(p)["name"] == "MacBook Pro Microphone"

    warnings = [ln for ln in capsys.readouterr().out.splitlines() if "未找到名称包含" in ln]
    assert len(warnings) == 1
    assert "输入设备" in warnings[0]


def test_sounddevice_adapter_exposes_pyaudio_shaped_input_devices(monkeypatch):
    # Windows CI 没装 sounddevice：只有这条用例需要它
    pytest.importorskip("sounddevice")
    from realtime_subtitle.capture import _sounddevice_pyaudio

    devices = [
        {"name": "Output Only", "max_input_channels": 0, "default_samplerate": 44100},
        {"name": "BlackHole 2ch", "max_input_channels": 2, "default_samplerate": 48000},
    ]

    class _FakeSoundDevice:
        @staticmethod
        def query_devices(kind=None):
            if kind == "input":
                return dict(devices[1], index=1)
            return devices

    monkeypatch.setattr(_sounddevice_pyaudio, "sd", _FakeSoundDevice)

    p = _sounddevice_pyaudio.PyAudio()

    assert p.get_device_count() == 2
    assert p.get_default_input_device_info() == {
        "name": "BlackHole 2ch",
        "index": 1,
        "defaultSampleRate": 48000,
        "maxInputChannels": 2,
    }
    assert list(p.get_input_device_info_generator()) == [p.get_default_input_device_info()]


def test_windows_never_takes_macos_branch(monkeypatch):
    """pyaudiowpatch 也有 get_default_input_device_info，路由只能按平台判。"""
    monkeypatch.setattr(audio_capture.sys, "platform", "win32")
    loopback = _dev(7, "Speakers [Loopback]")

    class _FakeWasapi(_FakeMacPyAudio):
        def get_default_wasapi_loopback(self):
            return loopback

    p = _FakeWasapi([_dev(0, "BlackHole 2ch")])
    assert AudioCapture._resolve_loopback(p) is loopback
