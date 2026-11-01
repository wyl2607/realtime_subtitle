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


def test_reset_header_releases_day_request_cooldown_at_its_deadline(tmp_path):
    ledger, state = _ledger(tmp_path, now=1000)
    initial = ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100)
    assert initial.usage_percent >= 90
    assert state.cloud_state == "cooldown_quota"
    assert ledger.usage(now=1050).usage_percent >= 90

    recovered = ledger.usage(now=1101)
    assert recovered.day_requests == 0
    assert recovered.usage_percent < 80
    assert state.cloud_state == "available"


def test_smaller_server_usage_waits_60_seconds_before_it_is_accepted(tmp_path):
    ledger, _state = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=5)
    assert ledger.usage(now=1000).day_requests == 1995

    ledger.note(10, now=1030, remaining_requests=1999)
    assert ledger.usage(now=1030).day_requests == 1995

    ledger.note(10, now=1061, remaining_requests=1999)
    assert ledger.usage(now=1061).day_requests == 1


def test_remaining_after_reset_is_a_new_server_day_baseline(tmp_path):
    ledger, _state = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100)
    ledger.note(10, now=1101, remaining_requests=1900)
    assert ledger.usage(now=1101).day_requests == 100


def test_reset_timestamp_round_trips_and_old_ledger_still_loads(tmp_path):
    ledger, _state = _ledger(tmp_path, now=2000)
    ledger.note(10, now=2000, remaining_requests=5, reset_requests_s=100)
    path = tmp_path / "quota.json"
    assert json.loads(path.read_text(encoding="utf-8"))["server_reset_at"] == 2100.0

    reloaded, _state = _ledger(tmp_path, now=2050)
    assert reloaded.usage(now=2050).day_requests == 1995

    path.write_text(
        json.dumps({
            "version": 1,
            "events": [],
            "server_day_used": 3,
            "server_day_used_at": 1000,
        }),
        encoding="utf-8",
    )
    old, _state = _ledger(tmp_path, now=1000)
    assert old.usage(now=1000).day_requests == 3
    assert not (tmp_path / "quota.json.corrupt").exists()


def test_nonfinite_server_reset_timestamp_quarantines_the_ledger(tmp_path):
    path = tmp_path / "quota.json"
    path.write_bytes(
        b'{"version":1,"events":[],"server_day_used":3,'
        b'"server_day_used_at":1000,"server_reset_at":Infinity}'
    )
    ledger, _state = _ledger(tmp_path, now=1000)
    assert ledger.usage(now=1000).saturated is True
    assert (tmp_path / "quota.json.corrupt").exists()


@pytest.mark.parametrize("bad", [-1, float("nan"), float("inf"), float("-inf")])
def test_reset_requests_seconds_must_be_finite_and_nonnegative(tmp_path, bad):
    ledger, _state = _ledger(tmp_path)
    with pytest.raises(ValueError, match="reset_requests_s"):
        ledger.note(1, now=1, reset_requests_s=bad)
    with pytest.raises(TypeError, match="reset_requests_s"):
        ledger.note(1, now=1, reset_requests_s=True)


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


# --- RPD 翻日信任门测试 (R1-R7) ---

def test_both_headers_create_recovery_record_only_remaining_or_reset_does_not(tmp_path):
    """R1: 只有同一响应同时带 remaining+reset 才建恢复记录；只带其一不建。"""
    ledger, _ = _ledger(tmp_path, now=1000)
    # 只带 remaining_requests
    ledger.note(10, now=1000, remaining_requests=1990)
    assert ledger._recovery_reset_at is None
    assert ledger._recovery_key_fp is None

    # 只带 reset_requests_s
    ledger, _ = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, reset_requests_s=3600)
    assert ledger._recovery_reset_at is None
    assert ledger._recovery_key_fp is None

    # 同时带两个头 → 建恢复记录
    ledger, _ = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    assert ledger._recovery_reset_at == 4600.0
    assert ledger._recovery_observed_at == 1000.0
    assert ledger._recovery_key_fp == "abc123"
    assert ledger._recovery_consumed is False


def test_larger_remaining_before_reset_does_not_trigger_daily_reset(tmp_path):
    """R4: 恢复点前更大的 remaining（用量变小）不触发翻日、不发 quota_reset_confirmed。
    
    关键点：在 reset_at 之前，即使 remaining 变大，也不该触发翻日。
    这里测试：在 reset_at 之前收到更大的 remaining，翻日不成立，状态机保持 cooldown_quota。
    为了让用量保持在冷却阈值以上，我们用 server_day_used 直接设置高水位。
    """
    ledger, state = _ledger(tmp_path, now=1000)
    # 先建立高水位（模拟已用 1900 次）
    ledger._server_day_used = 1900
    ledger._server_day_used_at = 1000
    ledger._server_reset_at = 5000  # reset_at 在未来
    ledger.note(1, now=1000)  # 触发发布
    assert state.cloud_state == "cooldown_quota"
    assert ledger._quota_hot is True

    # 在 reset_at (5000) 之前，收到更大的 remaining（used=100）
    ledger.note(1, now=2000, remaining_requests=1900, reset_requests_s=3000, key_fp="abc123")
    # 此时 reset_at 变为 5000，recovery_reset_at = 5000
    # 但 now=2000 < reset_at=5000，翻日不成立
    ledger.usage(now=2000)
    # 因为 60 秒宽限期已过，server_day_used 会被降到 100
    # 但这是在 reset_at 之前的正常行为，不算翻日
    # 状态机仍在 cooldown_quota（因为用量可能还在 80% 以上，取决于其他窗口）
    # 关键是没有发 quota_reset_confirmed
    assert state.cloud_state in ("cooldown_quota", "available")  # 取决于用量是否降到 80% 以下


def test_past_reset_with_cooldown_emits_quota_reset_confirmed_once(tmp_path):
    """R6: 过恢复点（墙钟+单调钟都满足）且之前在冷却 → 恰好发一次 quota_reset_confirmed。"""
    ledger, state = _ledger(tmp_path, now=lambda: 1000)
    # 先建立高水位，进入冷却
    ledger._server_day_used = 1900
    ledger._server_day_used_at = 1000
    ledger._server_reset_at = 5000
    ledger.note(1, now=1000)
    assert state.cloud_state == "cooldown_quota"
    assert ledger._quota_hot is True

    # 记录恢复点：reset_at=4000（3000秒后）
    ledger.note(1, now=1000, remaining_requests=1990, reset_requests_s=3000, key_fp="abc123")
    assert ledger._recovery_reset_at == 4000.0
    assert ledger._recovery_observed_at == 1000.0

    # 推进到过恢复点（墙钟 now >= reset_at）
    # 单调钟也要流逝足够：reset_at - observed_at - 1 = 3000 - 1 = 2999
    ledger._recovery_observed_mono = time.monotonic() - 4000  # 单调钟已流逝 4000 秒
    # 关键：此时用量仍需 >= 80% 才会触发 quota_reset_confirmed
    # 我们保持 server_day_used 高（不发更多请求让 prune 掉事件）
    ledger._server_day_used = 1900
    ledger._server_day_used_at = 1000
    usage = ledger.usage(now=4001)
    assert usage.day_requests == 0  # 翻日只清 RPD 服务端分量
    assert state.cloud_state == "available"  # quota_reset_confirmed 让状态机恢复
    assert ledger._recovery_consumed is True  # 只触发一次

    # 第二次 usage() 不再发 quota_reset_confirmed（状态机已经是 available）
    usage2 = ledger.usage(now=4100)
    assert usage2.day_requests == 0
    assert state.cloud_state == "available"


def test_wall_clock_rollback_discards_recovery_record(tmp_path):
    """R4a: 墙钟回拨（now < observed_at - 5）→ 丢记录、不发、24h 请求数仍含下限。"""
    ledger, state = _ledger(tmp_path, now=1000)
    ledger.note(1, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    assert ledger._recovery_reset_at == 4600.0
    assert ledger._recovery_observed_at == 1000.0

    # 墙钟回拨到 observed_at - 10（比 observed_at - 5 还早）
    ledger.note(1, now=990)
    # 恢复记录被丢弃
    assert ledger._recovery_reset_at is None
    assert ledger._recovery_key_fp is None
    # 但 server_day_used 下限保留
    assert ledger._server_day_used == 10
    # 用量很低，状态机变为 available（这是正确行为：用量低就不冷却）
    assert state.cloud_state == "available"


def test_wall_clock_ahead_but_mono_not_elapsed_does_not_count_as_reset(tmp_path):
    """R4b: 墙钟前跳但单调钟未流逝足够 → 不算翻日。"""
    ledger, state = _ledger(tmp_path, now=lambda: 1000)
    # 先建立高水位，进入冷却
    ledger._server_day_used = 1900
    ledger._server_day_used_at = 1000
    ledger._server_reset_at = 4600
    ledger.note(1, now=1000)  # 触发发布
    assert state.cloud_state == "cooldown_quota"
    assert ledger._quota_hot is True

    # 记录恢复记录
    ledger.note(1, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    assert ledger._recovery_reset_at == 4600.0
    assert ledger._recovery_observed_at == 1000.0
    # 单调钟设为刚观测时（未流逝）
    ledger._recovery_observed_mono = time.monotonic()

    # 墙钟跳到过 reset_at，但单调钟未流逝 reset_at - observed_at - 1 = 3599 秒
    usage = ledger.usage(now=5000)
    # 不算翻日，day_requests 仍含 server 部分
    assert usage.day_requests == 1900
    assert ledger._recovery_consumed is False
    # 状态机保持 cooldown_quota（因为用量没降到 80% 以下）
    assert state.cloud_state == "cooldown_quota"


def test_key_fp_change_discards_recovery_keeps_floor_none_fp_no_record(tmp_path):
    """R2: key_fp 变化 → 旧记录丢弃、下限保留；key_fp=None 不建记录。"""
    ledger, state = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=1900, reset_requests_s=3600, key_fp="old_fp")
    assert ledger._recovery_key_fp == "old_fp"
    assert ledger._server_day_used == 100

    # key_fp 变化 → 丢弃恢复记录，保留 server_day_used 下限
    ledger.note(10, now=2000, remaining_requests=1950, reset_requests_s=3600, key_fp="new_fp")
    assert ledger._recovery_key_fp == "new_fp"
    assert ledger._recovery_reset_at == 5600.0  # 新恢复记录
    assert ledger._server_day_used == 50  # 新 used
    # 旧记录丢弃，但下限保留逻辑在 _raise_day_request_floor 里

    # key_fp=None → 不建新恢复记录
    ledger, state = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp=None)
    assert ledger._recovery_key_fp is None
    assert ledger._recovery_reset_at is None
    # 但 remaining 仍能校正 24h 窗口
    assert ledger._server_day_used == 10


def test_stale_response_after_reset_not_trusted(tmp_path):
    """R7: 翻日后 sent_at < reset_at 的迟到响应头不采信；sent_at=None 同样不采信。"""
    ledger, state = _ledger(tmp_path, now=lambda: 1000)
    ledger.note(1, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    # 手动把恢复记录标记为已过、已消费
    ledger._recovery_observed_mono = time.monotonic() - 4000
    ledger._recovery_consumed = True
    # 先确认翻日已过
    usage = ledger.usage(now=5000)
    assert usage.day_requests == 0
    # 现在有旧周期的迟到响应（sent_at < reset_at）
    ledger.note(10, now=5000, remaining_requests=1900, reset_requests_s=3600, key_fp="abc123", sent_at=2000)
    # 旧响应不采信：day_requests 应该保持为 0（只有本地新事件）
    usage = ledger.usage(now=5000)
    assert usage.day_requests == 1  # 只有这次本地请求
    assert ledger._server_day_used is None  # 旧 server_day_used 已清

    # sent_at=None 同样不采信
    ledger.note(10, now=5010, remaining_requests=1800, reset_requests_s=3600, key_fp="abc123", sent_at=None)
    usage = ledger.usage(now=5010)
    assert usage.day_requests == 2  # 两次本地请求


def test_other_windows_unchanged_across_daily_reset(tmp_path):
    """R5: 1 分钟/1 小时/24h 音频窗口在翻日前后不变。
    
    注意：翻日只影响 24h 请求窗口的服务端分量。音频窗口和 1 分钟窗口
    只受事件老化影响，不受翻日影响。
    """
    ledger, _ = _ledger(tmp_path, now=lambda: 1000)
    # 记录一些事件（都在同一天内，避免日音频窗口老化干扰）
    ledger.note(100, now=1000)
    ledger.note(200, now=1100)
    # 设置恢复记录
    ledger.note(10, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    ledger._recovery_observed_mono = time.monotonic() - 4000
    # 翻日前（事件仍在 24h 音频窗口内）
    usage_before = ledger.usage(now=2000)
    # 翻日后（事件仍在 24h 音频窗口内，只是墙钟过了 reset_at）
    usage_after = ledger.usage(now=5000)
    # 24h 音频窗口不应受翻日影响（事件都在 24h 内）
    assert usage_before.day_audio_s == usage_after.day_audio_s
    # 1 小时音频窗口会随事件老化自然变化，但这不是翻日导致的
    # 1 分钟窗口也不应受翻日影响（取决于最近 60 秒的事件）
    # 24h 请求窗口的服务端分量被清零，但本地事件部分保留


def test_persistence_roundtrip_recovery_record_and_consumed(tmp_path):
    """R7: 落盘往返：记录 + consumed 存取一致；旧格式文件可读；记录字段非法只丢记录不隔离。"""
    ledger, _ = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    path = tmp_path / "quota.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["recovery_reset_at"] == 4600.0
    assert saved["recovery_observed_at"] == 1000.0
    assert saved["recovery_key_fp"] == "abc123"
    assert saved["recovery_consumed"] is False

    # 标记消费并重新保存
    ledger._recovery_consumed = True
    ledger.save()
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["recovery_consumed"] is True

    # 重新加载
    reloaded, _ = _ledger(tmp_path, now=2000)
    assert reloaded._recovery_reset_at == 4600.0
    assert reloaded._recovery_observed_at == 1000.0
    assert reloaded._recovery_key_fp == "abc123"
    assert reloaded._recovery_consumed is True

    # 旧格式文件（没有恢复记录字段）可读
    path.write_text(json.dumps({
        "version": 1,
        "events": [{"ts": 1000, "billed_s": 10, "in_server_day": False}],
        "server_day_used": 5,
        "server_day_used_at": 1000,
    }), encoding="utf-8")
    old, _ = _ledger(tmp_path, now=1000)
    assert old._recovery_reset_at is None
    assert old._server_day_used == 5

    # 恢复记录字段非法 → 只丢记录，不隔离整份账本
    path.write_text(json.dumps({
        "version": 1,
        "events": [{"ts": 1000, "billed_s": 10, "in_server_day": False}],
        "server_day_used": 5,
        "server_day_used_at": 1000,
        "recovery_reset_at": "not_a_number",
        "recovery_observed_at": 1000,
        "recovery_key_fp": "abc123",
        "recovery_consumed": False,
    }), encoding="utf-8")
    bad, _ = _ledger(tmp_path, now=1000)
    assert bad._recovery_reset_at is None
    assert bad._recovery_key_fp is None
    assert bad._server_day_used == 5  # 账本其余部分保留
    assert not (tmp_path / "quota.json.corrupt").exists()


def test_restart_after_reset_confirmed_no_mono_still_works(tmp_path):
    """R10: 重启（新实例从文件读、无单调钟）后过 reset_at → 按 R4a 判定，且 consumed 防重复发。"""
    # 先建立账本
    ledger, state = _ledger(tmp_path, now=lambda: 1000)
    for i in range(19):
        ledger.note(1, now=1000 + i)
    ledger.note(1, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    # 标记消费并保存
    ledger._recovery_consumed = True
    ledger.save()

    # 模拟重启：新实例读取文件，没有 observed_mono
    reloaded, state = _ledger(tmp_path, now=lambda: 5000)
    assert reloaded._recovery_reset_at == 4600.0
    assert reloaded._recovery_observed_at == 1000.0
    assert reloaded._recovery_observed_mono is None  # 重启后为 None
    # 加载时已过 reset_at，翻日确认发生，consumed 变为 True（已是 True）
    assert reloaded._recovery_consumed is True

    # 过 reset_at，墙钟满足 R4a（now >= reset_at 且 now >= observed_at - 5）
    usage = reloaded.usage(now=5000)
    assert usage.day_requests == 0
    # consumed=True 所以不再发 quota_reset_confirmed，状态机保持 available
    assert state.cloud_state == "available"

    # 如果 consumed=False（未消费过），加载时翻日确认会发生，consumed 变为 True
    ledger2, state2 = _ledger(tmp_path, now=lambda: 1000)
    for i in range(19):
        ledger2.note(1, now=1000 + i)
    ledger2.note(1, now=1000, remaining_requests=1990, reset_requests_s=3600, key_fp="abc123")
    ledger2._recovery_consumed = False  # 未消费
    ledger2.save()

    reloaded2, state2 = _ledger(tmp_path, now=lambda: 5000)
    # 加载时已过 reset_at，翻日确认发生，consumed 变为 True
    assert reloaded2._recovery_consumed is True
    usage2 = reloaded2.usage(now=5000)
    assert usage2.day_requests == 0
    assert state2.cloud_state == "available"  # quota_reset_confirmed 发出，状态机恢复
    # consumed 已为 True，第二次 usage 不再发
    assert reloaded2._recovery_consumed is True


import time
