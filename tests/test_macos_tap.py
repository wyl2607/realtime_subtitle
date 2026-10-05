import os
import sys

import pytest

from realtime_subtitle import config
from realtime_subtitle.capture import audio_capture
from realtime_subtitle.capture.audio_capture import AudioCapture
from realtime_subtitle.capture import macos_tap


class _FakeCoreAudio:
    def __init__(self, fail=None, stale=None):
        self.fail = fail
        self.stale = stale or []
        self.calls = []
        self.aggregate_desc = None

    def aggregate_devices(self):
        self.calls.append(("list",))
        return list(self.stale)

    def destroy_aggregate_device(self, aggregate_id):
        self.calls.append(("destroy_aggregate", aggregate_id))

    def destroy_process_tap(self, tap_id):
        self.calls.append(("destroy_tap", tap_id))

    def process_object_ids_for_pid(self, pid):
        self.calls.append(("pid", pid))
        return [777]

    def make_global_tap_description(self, excluded):
        self.calls.append(("desc", list(excluded)))
        return {"excluded": excluded}

    def create_process_tap(self, desc):
        self.calls.append(("create_tap", desc))
        if self.fail == "tap":
            raise macos_tap.CoreAudioError("tap failed")
        return 11

    def get_tap_uid(self, tap_id):
        self.calls.append(("uid", tap_id))
        if self.fail == "uid":
            raise macos_tap.CoreAudioError("uid failed")
        return "tap-uid"

    def create_aggregate_device(self, name, uid, tap_uid):
        self.calls.append(("create_aggregate", name, tap_uid))
        self.aggregate_desc = {"name": name, "uid": uid, "tap_uid": tap_uid}
        if self.fail == "aggregate":
            raise macos_tap.CoreAudioError("aggregate failed")
        return 22


@pytest.fixture(autouse=True)
def _mac_14_2(monkeypatch):
    monkeypatch.setattr(macos_tap.platform, "mac_ver", lambda: ("14.2.1", ("", "", ""), ""))
    monkeypatch.setattr(macos_tap.atexit, "register", lambda func: None)
    monkeypatch.setattr(audio_capture.sys, "platform", "darwin")
    monkeypatch.setattr(config, "LOOPBACK_DEVICE_NAME", "", raising=False)
    monkeypatch.setattr(config, "MACOS_CAPTURE_MODE", "auto", raising=False)
    monkeypatch.setattr(AudioCapture, "_macos_tap_device_name", None, raising=False)
    monkeypatch.setattr(AudioCapture, "_macos_tap_required", False, raising=False)
    monkeypatch.setattr(AudioCapture, "_missing_warned", None, raising=False)
    monkeypatch.setattr(AudioCapture, "_mac_default_warned", None, raising=False)


def _dev(index, name, channels=2, rate=48000):
    return {
        "name": name,
        "index": index,
        "defaultSampleRate": rate,
        "maxInputChannels": channels,
    }


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


def test_old_macos_returns_none(capsys, monkeypatch):
    monkeypatch.setattr(macos_tap.platform, "mac_ver", lambda: ("14.1.9", ("", "", ""), ""))

    assert macos_tap.create_system_audio_tap(_coreaudio=_FakeCoreAudio()) is None

    assert "需要 macOS 14.2" in capsys.readouterr().out


def test_success_creates_tap_then_public_aggregate():
    fake = _FakeCoreAudio()

    handle = macos_tap.create_system_audio_tap("Tap Device", _coreaudio=fake)

    assert handle is not None
    assert handle.tap_id == 11
    assert handle.aggregate_id == 22
    assert [c[0] for c in fake.calls[:5]] == ["list", "pid", "desc", "create_tap", "uid"]
    desc = fake.aggregate_desc
    assert desc["name"] == "Tap Device"
    assert desc["tap_uid"] == "tap-uid"


def test_destroy_destroys_aggregate_then_tap():
    fake = _FakeCoreAudio()
    handle = macos_tap.create_system_audio_tap("Tap Device", _coreaudio=fake)

    handle.destroy()
    handle.destroy()

    assert fake.calls[-2:] == [("destroy_aggregate", 22), ("destroy_tap", 11)]


@pytest.mark.parametrize("fail", ["tap", "uid", "aggregate"])
def test_failures_roll_back_created_objects(fail):
    fake = _FakeCoreAudio(fail=fail)

    assert macos_tap.create_system_audio_tap("Tap Device", _coreaudio=fake) is None

    if fail == "tap":
        assert ("destroy_tap", 11) not in fake.calls
    else:
        assert ("destroy_tap", 11) in fake.calls
    if fail == "aggregate":
        assert ("destroy_aggregate", 22) not in fake.calls


def test_stale_same_name_aggregate_is_destroyed_first():
    fake = _FakeCoreAudio(stale=[(99, "Tap Device"), (100, "Other")])

    assert macos_tap.create_system_audio_tap("Tap Device", _coreaudio=fake) is not None

    assert fake.calls[0] == ("list",)
    assert fake.calls[1] == ("destroy_aggregate", 99)


def test_resolve_macos_input_prefers_tap_device():
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(AudioCapture, "_macos_tap_device_name", "RealtimeSubtitle System Audio", raising=False)
    try:
        p = _FakeMacPyAudio([
            _dev(0, "MacBook Pro Microphone", 1),
            _dev(1, "BlackHole 2ch", 2),
            _dev(2, "RealtimeSubtitle System Audio", 2),
        ])
        assert AudioCapture._resolve_macos_input(p)["name"] == "RealtimeSubtitle System Audio"
    finally:
        monkeypatch.undo()


def test_resolve_macos_input_without_tap_keeps_blackhole_behavior():
    p = _FakeMacPyAudio([
        _dev(0, "MacBook Pro Microphone", 1),
        _dev(1, "BlackHole 2ch", 2),
    ])

    assert AudioCapture._resolve_macos_input(p)["name"] == "BlackHole 2ch"


def test_tap_mode_does_not_fall_back_when_tap_device_is_missing(monkeypatch):
    monkeypatch.setattr(AudioCapture, "_macos_tap_device_name", "RealtimeSubtitle System Audio", raising=False)
    monkeypatch.setattr(AudioCapture, "_macos_tap_required", True, raising=False)
    p = _FakeMacPyAudio([
        _dev(0, "MacBook Pro Microphone", 1),
        _dev(1, "BlackHole 2ch", 2),
    ])

    with pytest.raises(RuntimeError, match="Process Tap 聚合设备"):
        AudioCapture._resolve_macos_input(p)


def test_capture_mode_input_does_not_touch_tap(monkeypatch):
    monkeypatch.setattr(config, "MACOS_CAPTURE_MODE", "input", raising=False)

    def fail_if_called():
        raise AssertionError("tap should not be touched in input mode")

    monkeypatch.setattr(macos_tap, "create_system_audio_tap", fail_if_called)
    cap = AudioCapture(lambda *_: None)

    assert cap._prepare_macos_tap() is True
    assert cap._macos_tap_handle is None


@pytest.mark.skipif(sys.platform != "darwin", reason="真机 Process Tap smoke 只在 macOS 跑")
@pytest.mark.skipif(os.environ.get("RS_TAP_SMOKE") != "1", reason="设置 RS_TAP_SMOKE=1 才会真建系统音频 tap")
def test_process_tap_smoke_records_system_audio():
    """真机冒烟（桌面会话里跑；第一次会弹「系统音频录制」授权）。

    不只看设备在不在：没授权时 tap 照样建得出来、只是交出全零静音，所以
    一边 afplay 系统提示音一边录 2 秒，断言录到的不是静音。
    """
    import subprocess
    import numpy as np
    import sounddevice as sd

    handle = macos_tap.create_system_audio_tap()
    assert handle is not None
    try:
        sd._terminate()
        sd._initialize()
        dev = next(i for i, d in enumerate(sd.query_devices()) if d.get("name") == handle.device_name)
        player = subprocess.Popen(["afplay", "/System/Library/Sounds/Glass.aiff"])
        rec = sd.rec(int(48000 * 2), samplerate=48000, channels=2, device=dev, dtype="float32")
        sd.wait()
        player.wait()
        peak = float(np.abs(rec).max())
        assert peak > 1e-3, f"录到的是静音（峰值 {peak}）：多半没授权「系统音频录制」"
    finally:
        handle.destroy()
        sd._terminate()
        sd._initialize()
    assert handle.device_name not in [d.get("name") for d in sd.query_devices()]


def test_real_backend_builds_aggregate_dict_with_sdk_keys():
    """键名用 SDK 真常量：曾被猜成 'tap_list'/'tap_autostart'（真值 'taps'/'tapautostart'），
    猜错的后果是聚合设备里没挂 tap、静默退回 BlackHole。pyobjc 给的键是 bytes。"""
    import types
    seen = {}
    ca = types.SimpleNamespace(
        kAudioAggregateDeviceNameKey=b"name", kAudioAggregateDeviceUIDKey=b"uid",
        kAudioAggregateDeviceIsPrivateKey=b"private", kAudioAggregateDeviceTapListKey=b"taps",
        kAudioAggregateDeviceTapAutoStartKey=b"tapautostart", kAudioSubTapUIDKey=b"uid",
        AudioHardwareCreateAggregateDevice=lambda d, _: (seen.update(d) or (0, 42)),
    )
    backend = macos_tap._CoreAudio.__new__(macos_tap._CoreAudio)
    backend._ca = ca

    assert backend.create_aggregate_device("Tap", "u-1", "tap-uid") == 42
    assert seen == {"name": "Tap", "uid": "u-1", "private": False,
                    "taps": [{"uid": "tap-uid"}], "tapautostart": True}
