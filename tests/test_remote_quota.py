"""额度账本：本地滚动窗口 + 每日请求头校正。不连网。"""
import json
from pathlib import Path

import pytest

from realtime_subtitle.remote.quota import QuotaLedger
from realtime_subtitle.remote.routing_rules import RoutingState


def _ledger(tmp_path, now=0.0, state=None, name="quota.json"):
    routing = state if state is not None else RoutingState(has_key=True, cloud_enabled=True)
    clock = now if callable(now) else (lambda now=now: now)
    return QuotaLedger(tmp_path / name, routing=routing, now=clock), routing


def test_billed_seconds_are_at_least_10(tmp_path):
    ledger, _state = _ledger(tmp_path)
    first = ledger.note(3, now=10)
    assert first.hour_audio_s == 10
    second = ledger.note(12.5, now=11)
    assert second.hour_audio_s == 22.5
    assert second.minute_requests == 2


def test_minute_window_enters_at_90_and_recovers_below_80(tmp_path):
    ledger, state = _ledger(tmp_path)
    for i in range(17):
        ledger.note(1, now=i)
    assert state.cloud_state == "available"
    assert ledger.usage(now=16).minute_requests == 17
    assert ledger.usage(now=16).usage_percent == 85

    crossed = ledger.note(1, now=17)
    assert crossed.minute_requests == 18
    assert crossed.usage_percent == 90
    assert state.cloud_state == "cooldown_quota"
    assert state.action == "quota_cooldown"
    assert state.tier() == "local"

    held = ledger.usage(now=62)
    assert held.minute_requests == 16
    assert held.usage_percent == 80
    assert state.cloud_state == "cooldown_quota"

    recovered = ledger.usage(now=63)
    assert recovered.minute_requests == 15
    assert recovered.usage_percent == 75
    assert state.cloud_state == "available"
    assert state.action == "resume_cloud"
    assert state.tier() == "cloud"


def test_hour_audio_window_is_independent(tmp_path):
    ledger, state = _ledger(tmp_path, name="under.json")
    under = ledger.note(6479.9, now=0)
    assert under.hour_audio_s == 6479.9
    assert under.usage_percent < 90
    assert state.cloud_state == "available"

    ledger, state = _ledger(tmp_path, name="hit.json")
    hit = ledger.note(6480, now=0)
    assert hit.minute_requests == 1
    assert hit.hour_audio_s == 6480
    assert hit.usage_percent == 90
    assert state.cloud_state == "cooldown_quota"


def test_day_audio_window_still_counts_after_the_hour_rolls_off(tmp_path):
    ledger, state = _ledger(tmp_path)
    ledger.note(25920, now=0)
    assert state.cloud_state == "cooldown_quota"
    rolled = ledger.usage(now=3600.001)
    assert rolled.hour_audio_s == 0
    assert rolled.day_audio_s == 25920
    assert rolled.usage_percent == 90
    assert state.cloud_state == "cooldown_quota"
    kept = ledger.usage(now=86400)
    assert kept.day_audio_s == 25920
    assert state.cloud_state == "cooldown_quota"
    dropped = ledger.usage(now=86400.001)
    assert dropped.day_audio_s == 0
    assert dropped.usage_percent == 0
    assert state.cloud_state == "available"


def test_remaining_requests_corrects_only_the_day_request_window(tmp_path):
    ledger, state = _ledger(tmp_path, name="header.json")
    usage = ledger.note(100, now=50, remaining_requests=200)
    assert usage.minute_requests == 1
    assert usage.hour_audio_s == 100
    assert usage.day_requests == 1800
    assert usage.usage_percent == 90
    assert state.cloud_state == "cooldown_quota"

    # 头里的剩余把每日请求改成 1，但不能把这一分钟已经发出的 18 次抹掉。
    ledger, state = _ledger(tmp_path, name="minute.json")
    for i in range(18):
        usage = ledger.note(1, now=100 + i, remaining_requests=1999)
    assert usage.minute_requests == 18
    assert usage.day_requests == 1
    assert usage.usage_percent == 90
    assert state.cloud_state == "cooldown_quota"

    ledger, state = _ledger(tmp_path, name="full.json")
    for i in range(3):
        usage = ledger.note(1, now=i, remaining_requests=0)
    assert usage.minute_requests == 3
    assert usage.day_requests == 2000
    assert usage.hour_audio_s == 30


def test_day_request_header_hysteresis_and_later_local_requests(tmp_path):
    ledger, state = _ledger(tmp_path)
    ledger.note(1, now=0, remaining_requests=100)
    assert state.cloud_state == "cooldown_quota"
    assert ledger.usage(now=0).day_requests == 1900

    # 窗口内更宽裕的 remaining 只能被忽略，不能把日请求水位从 1900 洗到 1700。
    stayed = ledger.note(1, now=10, remaining_requests=300)
    assert stayed.day_requests == 1900
    assert stayed.usage_percent == 95
    assert state.cloud_state == "cooldown_quota"

    raised = ledger.note(1, now=20, remaining_requests=50)
    assert raised.day_requests == 1950
    assert raised.usage_percent == 97.5
    assert state.cloud_state == "cooldown_quota"

    extra = ledger.note(1, now=21)
    assert extra.minute_requests == 4
    assert extra.day_requests == 1951
    assert state.cloud_state == "cooldown_quota"

    # 高水位跟着自己的时间戳过 24h，不是永久饱和。
    expired = ledger.usage(now=21 + 86401)
    assert expired.day_requests == 0
    assert expired.usage_percent == 0
    assert expired.saturated is False
    assert state.cloud_state == "available"
    assert state.action == "resume_cloud"


def test_low_usage_does_not_clear_a_rate_limit_cooldown(tmp_path):
    state = RoutingState(has_key=True, cloud_enabled=True)
    state.cloud_state = "cooldown_rate_limited"
    state.cooldown_seconds = 60
    ledger, state = _ledger(tmp_path, state=state)
    ledger.note(1, now=5)
    ledger.usage(now=6)
    assert state.cloud_state == "cooldown_rate_limited"
    assert state.cooldown_seconds == 60


def test_persistence_survives_reload_and_drops_events_older_than_24h(tmp_path):
    ledger, _state = _ledger(tmp_path, now=lambda: 1000)
    ledger.note(12.5, now=1000, remaining_requests=1500)
    path = tmp_path / "quota.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["server_day_used"] == 500
    assert saved["events"][0]["billed_s"] == 12.5
    assert saved["events"][0]["in_server_day"] is True

    reloaded, state = _ledger(tmp_path, now=lambda: 1000)
    usage = reloaded.usage(now=1000)
    assert usage.hour_audio_s == 12.5
    assert usage.day_requests == 500
    assert state.cloud_state == "available"

    aged, aged_state = _ledger(tmp_path, now=lambda: 1000 + 86400.001)
    assert aged.usage(now=1000 + 86400.001).day_requests == 0
    assert aged.usage(now=1000 + 86400.001).day_audio_s == 0
    assert aged_state.cloud_state == "available"

    still, _state = _ledger(tmp_path, now=lambda: 1000 + 86400)
    # 上一次加载已经把超龄事件剪掉并写回。用原文件再验证「刚好 24h 仍保留」。
    original = {
        "version": 1,
        "saturated": False,
        "events": [{"ts": 1000, "billed_s": 10, "in_server_day": False}],
        "server_day_used": None,
        "server_day_used_at": None,
    }
    path.write_text(json.dumps(original), encoding="utf-8")
    kept, _state = _ledger(tmp_path, now=lambda: 1000 + 86400)
    assert kept.usage(now=1000 + 86400).day_audio_s == 10
    _ = still


def test_missing_file_is_empty_not_full(tmp_path):
    path = tmp_path / "quota.json"
    state = RoutingState(has_key=True, cloud_enabled=True)
    ledger = QuotaLedger(path, routing=state, now=lambda: 0)
    assert not path.exists()
    assert ledger.usage(now=0).usage_percent == 0
    assert ledger.usage(now=0).saturated is False
    assert state.cloud_state == "available"


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"{",
        b'{"version": 1}',
        b'{"version": 2, "events": []}',
        b'{"version": 1, "events": [{"ts": 0, "billed_s": -1, "in_server_day": false}]}',
        b'{"version": 1, "events": "nope"}',
    ],
)
def test_corrupt_ledger_is_treated_as_full(tmp_path, payload):
    path = tmp_path / "quota.json"
    path.write_bytes(payload)
    state = RoutingState(has_key=True, cloud_enabled=True)
    ledger = QuotaLedger(path, routing=state, now=lambda: 50)
    usage = ledger.usage(now=50)
    assert usage.saturated is True
    assert usage.usage_percent == 100
    assert state.cloud_state == "cooldown_quota"
    assert state.tier() == "local"

    reloaded, reloaded_state = _ledger(tmp_path, now=lambda: 50)
    assert reloaded.usage(now=50).usage_percent == 100
    assert reloaded_state.cloud_state == "cooldown_quota"
    # 每日请求头不能把坏掉的音频账本洗成 0。
    still = reloaded.note(1, now=51, remaining_requests=1999)
    assert still.usage_percent == 100
    assert still.day_requests == 1 or still.saturated
    assert reloaded.routing.cloud_state == "cooldown_quota"


def test_replace_failure_keeps_the_previous_file_and_no_temp_leftover(tmp_path, monkeypatch):
    import realtime_subtitle.remote.quota as quota

    ledger, _state = _ledger(tmp_path)
    ledger.note(12, now=1)
    path = tmp_path / "quota.json"
    original = path.read_bytes()
    calls = []
    real_replace = quota.os.replace

    def spy_fsync(fd):
        calls.append(("fsync", fd))

    def boom(src, dst):
        calls.append(("replace", src, dst))
        raise OSError("disk full")

    monkeypatch.setattr(quota.os, "fsync", spy_fsync)
    monkeypatch.setattr(quota.os, "replace", boom)
    with pytest.raises(OSError):
        ledger.note(12, now=2)
    assert path.read_bytes() == original
    assert list(path.parent.glob(".quota-*")) == []
    assert calls[0][0] == "fsync"
    assert calls[1][0] == "replace"
    _ = real_replace


def test_save_fsyncs_then_replaces_in_the_same_directory(tmp_path, monkeypatch):
    import realtime_subtitle.remote.quota as quota

    order = []
    real_replace = quota.os.replace
    real_fsync = quota.os.fsync

    def spy_fsync(fd):
        order.append("fsync")
        return real_fsync(fd)

    def spy_replace(src, dst):
        order.append(("replace", Path(src).parent, Path(dst)))
        return real_replace(src, dst)

    monkeypatch.setattr(quota.os, "fsync", spy_fsync)
    monkeypatch.setattr(quota.os, "replace", spy_replace)
    ledger, _state = _ledger(tmp_path)
    ledger.note(4, now=4)
    path = tmp_path / "quota.json"
    assert order[0] == "fsync"
    kind, parent, dst = order[1]
    assert kind == "replace"
    assert parent == path.parent
    assert dst == path
    assert list(path.parent.glob(".quota-*")) == []


def test_corrupt_ledger_is_capped_for_24h_then_expires(tmp_path):
    path = tmp_path / "quota.json"
    corrupt = tmp_path / "quota.json.corrupt"
    corrupt.write_bytes(b"old-corrupt")
    original = b"NOT-JSON{{"
    path.write_bytes(original)
    t0 = 10_000.0
    state = RoutingState(has_key=True, cloud_enabled=True)
    ledger = QuotaLedger(path, routing=state, now=lambda: t0)
    usage = ledger.usage(now=t0)
    assert usage.usage_percent == 100
    assert usage.saturated is True
    assert usage.day_requests == 2000
    assert state.cloud_state == "cooldown_quota"
    assert corrupt.read_bytes() == original
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["server_day_used"] == 2000
    assert saved["server_day_used_at"] == t0
    assert "saturated" not in saved

    assert ledger.usage(now=t0 + 86399).usage_percent == 100
    reloaded = QuotaLedger(path, routing=RoutingState(has_key=True, cloud_enabled=True), now=lambda: t0 + 86399)
    assert reloaded.usage(now=t0 + 86399).usage_percent == 100
    assert reloaded.usage(now=t0 + 86399).saturated is True
    # 封顶还在窗口里时，更宽裕的 remaining 不能把 2000 洗掉。用另一实例，避免污染上面的空事件列表。
    held = reloaded.note(1, now=t0 + 10, remaining_requests=1999)
    assert held.usage_percent == 100
    assert held.day_requests == 2000
    assert held.saturated is True

    fallen = ledger.usage(now=t0 + 86401)
    assert fallen.usage_percent == 0
    assert fallen.day_requests == 0
    assert fallen.saturated is False
    assert state.cloud_state == "available"
    resumed = ledger.note(12, now=t0 + 86401)
    assert resumed.hour_audio_s == 12
    assert resumed.day_requests == 1
    assert resumed.usage_percent < 90


def test_negative_nan_and_inf_events_are_dropped(tmp_path):
    path = tmp_path / "quota.json"
    path.write_bytes(
        b'{"version":1,"events":['
        b'{"ts":10000,"billed_s":-4,"in_server_day":false},'
        b'{"ts":10000,"billed_s":NaN,"in_server_day":false},'
        b'{"ts":10000,"billed_s":Infinity,"in_server_day":false},'
        b'{"ts":10000,"billed_s":-Infinity,"in_server_day":false},'
        b'{"ts":10000,"billed_s":+Infinity,"in_server_day":false},'
        b'{"ts":10000,"billed_s":10,"in_server_day":false},'
        b'{"ts":10000,"billed_s":12.5,"in_server_day":false}'
        b'],"server_day_used":null,"server_day_used_at":null}'
    )
    ledger, state = _ledger(tmp_path, now=lambda: 10000)
    usage = ledger.usage(now=10000)
    assert usage.day_requests == 2
    assert usage.day_audio_s == 22.5
    assert usage.usage_percent != 100
    assert state.cloud_state == "available"
    assert path.exists()
    assert not (tmp_path / "quota.json.corrupt").exists()


def test_note_still_rejects_negative_nan_and_inf(tmp_path):
    ledger, _state = _ledger(tmp_path)
    for bad in (-1, float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            ledger.note(bad, now=1)
    assert ledger.usage(now=1).minute_requests == 0
    assert ledger.usage(now=1).saturated is False


def test_quota_publishes_routing_events_and_does_not_own_cloud_state():
    source = Path(__file__).resolve().parents[1].joinpath("realtime_subtitle/remote/quota.py").read_text(encoding="utf-8")
    assert "quota_update" in source
    assert "cooldown_quota" not in source
    assert "RoutingState" in source
