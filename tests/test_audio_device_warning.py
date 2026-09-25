"""「找不到指定设备」只在状态变化时报一次，别每 5 秒刷一行进 subtitle.log。

☠️ 探测线程每 DEVICE_CHECK_INTERVAL 秒调一次 _resolve_loopback。设备名填错或
设备拔了，以前每次都打一行，一天约一万七千行——而 subtitle.log 正是出问题时
要发给 AI 看的那份，有用的行全被淹掉。
"""
import pytest

from realtime_subtitle import config
from realtime_subtitle.capture.audio_capture import AudioCapture


class _FakePyAudio:
    def __init__(self, names, default="Lautsprecher [Loopback]"):
        self.names = names
        self.default = default

    def get_default_wasapi_loopback(self):
        return {"name": self.default}

    def get_loopback_device_info_generator(self):
        for n in self.names:
            yield {"name": n}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    monkeypatch.setattr(AudioCapture, "_missing_warned", None, raising=False)
    monkeypatch.setattr(config, "LOOPBACK_DEVICE_NAME", "FiiO", raising=False)


def _warnings(capsys):
    return [ln for ln in capsys.readouterr().out.splitlines() if "未找到名称包含" in ln]


def test_missing_device_warns_once_not_every_probe(capsys):
    p = _FakePyAudio(["Speakers [Loopback]"])
    for _ in range(50):  # 50 次探测 ≈ 4 分钟
        assert AudioCapture._resolve_loopback(p)["name"] == p.default
    assert len(_warnings(capsys)) == 1


def test_warns_again_after_device_came_back_and_left(capsys):
    """插上 → 拔掉：这是一次新的状态变化，用户需要看到。"""
    gone = _FakePyAudio(["Speakers [Loopback]"])
    back = _FakePyAudio(["FiiO K3 [Loopback]"])
    AudioCapture._resolve_loopback(gone)
    assert AudioCapture._resolve_loopback(back)["name"] == "FiiO K3 [Loopback]"
    AudioCapture._resolve_loopback(gone)
    assert len(_warnings(capsys)) == 2


def test_warns_again_when_fallback_default_changes(capsys):
    """回退到的默认设备变了，提示里的名字就过时了，要重报。"""
    AudioCapture._resolve_loopback(_FakePyAudio([], default="A [Loopback]"))
    AudioCapture._resolve_loopback(_FakePyAudio([], default="A [Loopback]"))
    AudioCapture._resolve_loopback(_FakePyAudio([], default="B [Loopback]"))
    assert len(_warnings(capsys)) == 2
