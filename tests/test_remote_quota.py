"""额度账本：本地滚动窗口 + 每日请求头校正。不连网。"""
import json
from pathlib import Path

import pytest

from realtime_subtitle.remote.quota import QuotaLedger
from realtime_subtitle.remote.routing_rules import RoutingState


def _ledger(tmp_path, now=0.0, state=None, name="quota.json", monotonic=None):
    routing = state if state is not None else RoutingState(has_key=True, cloud_enabled=True)
    clock = now if callable(now) else (lambda now=now: now)
    return QuotaLedger(tmp_path / name, routing=routing, now=clock, monotonic=monotonic), routing


class _RecordingRouting(RoutingState):
    def __init__(self):
        super().__init__(has_key=True, cloud_enabled=True)
        self.events = []

    def apply(self, event):
        self.events.append(dict(event))
        super().apply(event)


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

    # 日请求下限可以降到 1，但本地已经发出的 18 次仍是独立下限。
    ledger, state = _ledger(tmp_path, name="minute.json")
    for i in range(18):
        usage = ledger.note(1, now=100 + i, remaining_requests=1999)
    assert usage.minute_requests == 18
    assert usage.day_requests == 18
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


def test_trusted_reset_record_requires_both_headers_and_key_fingerprint(tmp_path):
    ledger, _state = _ledger(tmp_path, now=1000, name="both.json", monotonic=lambda: 0)
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    assert json.loads((tmp_path / "both.json").read_text(encoding="utf-8"))["reset_record"] == {
        "consumed": False, "key_fp": "key-a", "observed_at": 1000.0, "reset_at": 1100.0,
    }

    reset_only, _state = _ledger(tmp_path, now=1000, name="reset-only.json", monotonic=lambda: 0)
    reset_only.note(10, now=1000, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    assert json.loads((tmp_path / "reset-only.json").read_text(encoding="utf-8"))["reset_record"] is None

    remaining_only, _state = _ledger(tmp_path, now=1000, name="remaining-only.json", monotonic=lambda: 0)
    remaining_only.note(10, now=1000, remaining_requests=5, key_fp="key-a", sent_at=1000)
    assert json.loads((tmp_path / "remaining-only.json").read_text(encoding="utf-8"))["reset_record"] is None

    too_far, _state = _ledger(tmp_path, now=1000, name="too-far.json", monotonic=lambda: 0)
    too_far.note(10, now=1000, remaining_requests=5, reset_requests_s=86460.1, key_fp="key-a", sent_at=1000)
    assert json.loads((tmp_path / "too-far.json").read_text(encoding="utf-8"))["reset_record"] is None


def test_trusted_reset_emits_once_after_both_clocks_and_preserves_audio_windows(tmp_path):
    mono = [0.0]
    state = _RecordingRouting()
    ledger, _state = _ledger(tmp_path, now=100, state=state, monotonic=lambda: mono[0])
    before = ledger.note(3600, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)
    assert before.hour_audio_s == before.day_audio_s == 3600
    assert state.cloud_state == "cooldown_quota"

    mono[0] = 9.0
    after = ledger.usage(now=110)
    assert after.day_requests == 0
    assert after.hour_audio_s == before.hour_audio_s
    assert after.day_audio_s == before.day_audio_s
    assert state.events[-1] == {"type": "quota_reset_confirmed", "usage_percent": 50.0}
    assert sum(event["type"] == "quota_reset_confirmed" for event in state.events) == 1

    ledger.usage(now=111)
    assert sum(event["type"] == "quota_reset_confirmed" for event in state.events) == 1


def test_untrusted_wall_clock_changes_keep_server_floor_and_do_not_emit_reset(tmp_path):
    mono = [0.0]
    state = _RecordingRouting()
    ledger, _state = _ledger(tmp_path, now=100, state=state, monotonic=lambda: mono[0])
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)

    rolled_back = ledger.usage(now=94)
    assert rolled_back.day_requests == 1995
    assert not any(event["type"] == "quota_reset_confirmed" for event in state.events)
    assert json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))["reset_record"] is None

    mono = [0.0]
    state = _RecordingRouting()
    ledger, _state = _ledger(tmp_path, now=100, state=state, name="jump.json", monotonic=lambda: mono[0])
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)
    jumped = ledger.usage(now=110)
    assert jumped.day_requests == 1995
    assert not any(event["type"] == "quota_reset_confirmed" for event in state.events)


def test_reset_record_binds_key_and_rejects_late_old_cycle_headers(tmp_path):
    mono = [0.0]
    ledger, _state = _ledger(tmp_path, now=100, monotonic=lambda: mono[0])
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)

    ledger.note(10, now=110, remaining_requests=1999, reset_requests_s=50, key_fp="key-a", sent_at=99)
    assert ledger.usage(now=110).day_requests == 1996
    ledger.note(10, now=111, remaining_requests=1999, reset_requests_s=50, key_fp="key-a", sent_at=None)
    assert ledger.usage(now=111).day_requests == 1997

    ledger.note(10, now=112, remaining_requests=1999, reset_requests_s=50, key_fp="key-b", sent_at=112)
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"]["key_fp"] == "key-b"
    assert ledger.usage(now=112).day_requests == 1995

    ledger.note(10, now=113, remaining_requests=1999, reset_requests_s=50, key_fp=None, sent_at=113)
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"] is None
    assert ledger.usage(now=113).day_requests == 1995


def test_reset_record_persists_consumption_and_bad_record_is_not_quarantined(tmp_path):
    mono = [0.0]
    state = _RecordingRouting()
    ledger, _state = _ledger(tmp_path, now=100, state=state, monotonic=lambda: mono[0])
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)
    mono[0] = 9
    ledger.usage(now=110)
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"]["consumed"] is True

    restarted = _RecordingRouting()
    reloaded, _state = _ledger(tmp_path, now=lambda: 110, state=restarted, monotonic=lambda: 0)
    assert reloaded.usage(now=111).day_requests == 0
    assert not any(event["type"] == "quota_reset_confirmed" for event in restarted.events)

    path = tmp_path / "bad-record.json"
    path.write_text(json.dumps({
        "version": 1, "events": [], "server_day_used": 3, "server_day_used_at": 100,
        "reset_record": {"reset_at": "bad", "observed_at": 100, "key_fp": "key-a", "consumed": False},
    }), encoding="utf-8")
    bad, _state = _ledger(tmp_path, now=100, name="bad-record.json")
    assert bad.usage(now=100).day_requests == 3
    assert not (tmp_path / "bad-record.json.corrupt").exists()


def test_restarted_ledger_trusts_elapsed_reset_without_a_monotonic_observation(tmp_path):
    mono = [0.0]
    ledger, _state = _ledger(tmp_path, now=100, monotonic=lambda: mono[0])
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)

    restarted = _RecordingRouting()
    reloaded, _state = _ledger(tmp_path, now=lambda: 110, state=restarted, monotonic=lambda: 0)
    assert reloaded.usage(now=110).day_requests == 0
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"]["consumed"] is True
    # 由于 server_day_used=1995 (99.75%) ≥90%，旧周期处于冷却，重启时应发 quota_reset_confirmed
    assert any(event["type"] == "quota_reset_confirmed" for event in restarted.events)
    # usage_percent 由 minute 窗口决定（只有 1 个请求在 100 秒），1/20=5%
    assert restarted.events[-1]["type"] == "quota_reset_confirmed"
    assert restarted.events[-1]["usage_percent"] == 5.0


def test_smaller_server_usage_waits_60_seconds_before_it_is_accepted(tmp_path):
    ledger, _state = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=5)
    assert ledger.usage(now=1000).day_requests == 1995

    ledger.note(10, now=1030, remaining_requests=1999)
    assert ledger.usage(now=1030).day_requests == 1995

    ledger.note(10, now=1061, remaining_requests=1999)
    assert ledger.usage(now=1061).day_requests == 3
    assert ledger._server_day_used == 1


def test_remaining_after_reset_is_a_new_server_day_baseline(tmp_path):
    mono = [0.0]
    ledger, _state = _ledger(tmp_path, now=1000, monotonic=lambda: mono[0])
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    mono[0] = 99
    ledger.note(10, now=1101, remaining_requests=1900, key_fp="key-a", sent_at=1101)
    assert ledger.usage(now=1101).day_requests == 100


def test_reset_timestamp_round_trips_and_old_ledger_still_loads(tmp_path):
    ledger, _state = _ledger(tmp_path, now=2000, monotonic=lambda: 0)
    ledger.note(10, now=2000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=2000)
    path = tmp_path / "quota.json"
    assert json.loads(path.read_text(encoding="utf-8"))["reset_record"]["reset_at"] == 2100.0

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


def test_old_or_nonfinite_reset_fields_do_not_quarantine_the_ledger(tmp_path):
    path = tmp_path / "quota.json"
    path.write_bytes(
        b'{"version":1,"events":[],"server_day_used":3,'
        b'"server_day_used_at":1000,"server_reset_at":Infinity}'
    )
    ledger, _state = _ledger(tmp_path, now=1000)
    assert ledger.usage(now=1000).day_requests == 3
    assert not (tmp_path / "quota.json.corrupt").exists()


def test_parse_reset_record_validates_time_bounds(tmp_path):
    """G1: reset_at - observed_at must be in [0, MAX_RESET_REQUESTS_S]; otherwise record is discarded."""
    path = tmp_path / "quota.json"
    # Case 1: reset_at < observed_at (negative diff) -> discard record, keep server_day_used
    path.write_text(json.dumps({
        "version": 1,
        "events": [],
        "server_day_used": 500,
        "server_day_used_at": 1000,
        "reset_record": {"reset_at": 900, "observed_at": 1000, "key_fp": "key-a", "consumed": False},
    }), encoding="utf-8")
    ledger, _state = _ledger(tmp_path, now=1000, name="quota.json")
    assert ledger.usage(now=1000).day_requests == 500  # server_day_used preserved
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["reset_record"] is None  # bad record discarded
    assert not (tmp_path / "quota.json.corrupt").exists()  # ledger not quarantined

    # Case 2: reset_at - observed_at > MAX_RESET_REQUESTS_S -> discard record
    path.write_text(json.dumps({
        "version": 1,
        "events": [],
        "server_day_used": 800,
        "server_day_used_at": 1000,
        "reset_record": {"reset_at": 100000, "observed_at": 1000, "key_fp": "key-a", "consumed": False},
    }), encoding="utf-8")
    ledger2, _state = _ledger(tmp_path, now=1000, name="quota.json")
    assert ledger2.usage(now=1000).day_requests == 800
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["reset_record"] is None
    assert not (tmp_path / "quota.json.corrupt").exists()

    # Case 3: valid bounds -> record kept (not consumed yet since now=1000 < reset_at=1100)
    path.write_text(json.dumps({
        "version": 1,
        "events": [],
        "server_day_used": 300,
        "server_day_used_at": 1000,
        "reset_record": {"reset_at": 1100, "observed_at": 1000, "key_fp": "key-a", "consumed": False},
    }), encoding="utf-8")
    ledger3, _state = _ledger(tmp_path, now=1000, name="quota.json")
    assert ledger3.usage(now=1000).day_requests == 300  # server_day_used=300, reset not yet consumed
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["reset_record"] is not None
    assert saved["reset_record"]["key_fp"] == "key-a"


def test_restart_after_reset_emits_quota_reset_confirmed_when_hot(tmp_path):
    """G3: 重启时在 reset 之后、账本原本在冷却（≥90%）、恢复记录未消费。
    加载时应先用旧周期用量判断热度，置 _quota_hot=True，再推进消费并发 quota_reset_confirmed。"""
    mono = [0.0]
    state = _RecordingRouting()
    ledger, _state = _ledger(tmp_path, now=100, state=state, monotonic=lambda: mono[0])
    # 制造 ≥90% 用量（18 请求 = 90%）
    for i in range(18):
        ledger.note(1, now=100 + i)
    # 建立恢复记录：reset_at = 110（10秒后）
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-a", sent_at=100)
    # 此时 day_requests = 1995，usage_percent ≥90，_quota_hot=True
    assert state.cloud_state == "cooldown_quota"
    assert ledger._quota_hot is True
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"]["consumed"] is False
    assert saved["reset_record"]["reset_at"] == 110.0

    # 进程在 reset_at 之后重启（now=110）
    # 修复前：_advance_reset 先消费记录清 floor，_quota_hot 仍为 False，
    # _publish 里 reset_confirmed=True 但 previously_hot=False，不发 quota_reset_confirmed
    # 修复后：_load_locked 先用旧周期 _measure 判热度，置 _quota_hot=True，
    # 再 _advance_reset，_publish 里 previously_hot=True，发 quota_reset_confirmed
    restarted = _RecordingRouting()
    reloaded, _state = _ledger(tmp_path, now=lambda: 110, state=restarted, monotonic=lambda: 0)
    # day_requests 应归零（新周期）
    assert reloaded.usage(now=110).day_requests == 0
    # 关键：必须发出 quota_reset_confirmed，路由才能从 cooldown_quota 恢复
    assert any(event["type"] == "quota_reset_confirmed" for event in restarted.events)
    # usage_percent 由 minute 窗口决定（12 个请求在 100-110 秒内），12/20=60%
    assert restarted.events[-1] == {"type": "quota_reset_confirmed", "usage_percent": 60.0}


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


def test_remaining_requests_out_of_bounds_does_not_correct_or_create_reset_record(tmp_path):
    """F1: remaining_requests must be 0..LIMIT_REQUESTS_PER_DAY; 2001 and -1 are rejected."""
    ledger, state = _ledger(tmp_path, now=100, name="f1.json")
    # 先建立一个基线
    ledger.note(10, now=100, remaining_requests=1000)
    assert ledger.usage(now=100).day_requests == 1000

    # 2001 (超过上限) — 不校正 server_day_used，不建立 reset_record
    # 新请求仍计入本地计数，但因 valid_remaining=False，in_server_day 不被置 True
    # day_requests = server_floor(1000) + 1 个新本地请求 = 1001
    usage = ledger.note(10, now=101, remaining_requests=2001, reset_requests_s=50, key_fp="key-a", sent_at=101)
    assert usage.day_requests == 1001  # server_floor 1000 + 1 个新本地请求
    saved = json.loads((tmp_path / "f1.json").read_text(encoding="utf-8"))
    assert saved["reset_record"] is None
    assert saved["server_day_used"] == 1000  # server floor 未变
    # usage_percent = 1001/2000*100 = 50% < 90，所以状态是 available
    assert state.cloud_state == "available"

    # -1 (负数) — 同上
    usage = ledger.note(10, now=102, remaining_requests=-1, reset_requests_s=50, key_fp="key-a", sent_at=102)
    assert usage.day_requests == 1002  # server_floor 1000 + 2 个新本地请求
    saved = json.loads((tmp_path / "f1.json").read_text(encoding="utf-8"))
    assert saved["reset_record"] is None
    assert saved["server_day_used"] == 1000

    # 合法边界 0 和 2000 仍能工作
    usage = ledger.note(10, now=103, remaining_requests=0, reset_requests_s=50, key_fp="key-a", sent_at=103)
    assert usage.day_requests == 2000
    # 60s 宽限期后，remaining=2000 才能把 floor 降到 0
    usage = ledger.note(10, now=164, remaining_requests=2000, reset_requests_s=50, key_fp="key-a", sent_at=164)
    assert usage.day_requests == 5  # 服务端下限降到 0，本地 5 次仍须保留
    assert ledger._server_day_used == 0


def test_key_change_before_late_old_cycle_allows_new_reset_record(tmp_path):
    """F2: key_fp check must run before late_old_cycle judgment.
    Scenario: old key record has passed reset_at; new key response has both headers
    and sent_at=None. Before fix, late_old_cycle would be True (using old record)
    and new reset_record would not be created. After fix, key change drops old
    record first, so late_old_cycle becomes False and new record is established.
    Note: server floor lowering is still guarded by 60s grace period (SERVER_DECREASE_GRACE_S),
    so day_requests stays at old floor until grace period passes or old floor expires.
    """
    mono = [0.0]
    ledger, _state = _ledger(tmp_path, now=100, monotonic=lambda: mono[0])
    # 建立旧 key 的恢复记录，reset_at = 110
    ledger.note(10, now=100, remaining_requests=5, reset_requests_s=10, key_fp="key-old", sent_at=100)
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"]["key_fp"] == "key-old"
    assert saved["reset_record"]["reset_at"] == 110.0
    # 此时 server_day_used = 1995 (2000-5)

    # 时间推到 reset_at 之后 (115)，旧 key 的记录已过期
    # 新 key 发来带两个头的响应，sent_at=None (不知道何时发出)
    # 修复前：late_old_cycle 用旧 record 判为 True，新记录不建立
    # 修复后：先按 key_fp 丢掉旧 record，late_old_cycle 变 False，新记录建立
    mono[0] = 15.0
    usage = ledger.note(
        10, now=115, remaining_requests=1999, reset_requests_s=100, key_fp="key-new", sent_at=None
    )
    # 新 reset_record 应建立（关键修复点）
    saved = json.loads((tmp_path / "quota.json").read_text(encoding="utf-8"))
    assert saved["reset_record"] is not None
    assert saved["reset_record"]["key_fp"] == "key-new"
    assert saved["reset_record"]["reset_at"] == 215.0  # 115 + 100
    assert saved["reset_record"]["consumed"] is False
    # 但 server floor 降低仍受 60s 宽限期保护 (115-100=15 < 60)，仍为 1995
    # 由于 valid_remaining=True，所有事件 in_server_day 被置 True，extra=0
    # day_requests = server_floor(1995) + 0 = 1995
    assert usage.day_requests == 1995
    # 宽限期过后 (>=160)，再次 note() 触发 floor 降到新 remaining 对应的 1
    usage2 = ledger.note(10, now=161, remaining_requests=1999, key_fp="key-new", sent_at=161)
    assert usage2.day_requests == 3  # 服务端下限为 1，本地 3 次仍须保留
    assert ledger._server_day_used == 1


@pytest.mark.parametrize("restart_consumes", [False, True])
@pytest.mark.parametrize("replace_record", [False, True])
def test_consumed_reset_rollback_restores_retained_server_floor(tmp_path, restart_consumes, replace_record):
    """F1：消费恢复点不能删历史；回拨和重启都必须重新计入旧周期下限。"""
    mono = [100.0]
    ledger, state = _ledger(tmp_path, now=1000, monotonic=lambda: mono[0])
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    mono[0] = 300
    if restart_consumes:
        ledger, state = _ledger(tmp_path, now=1200, monotonic=lambda: mono[0])
    assert ledger.usage(now=1200).day_requests == 0
    if replace_record:
        ledger.note(10, now=1200, remaining_requests=1999, reset_requests_s=100, key_fp="key-b", sent_at=1200)
    rolled = ledger.usage(now=1050)
    assert rolled.day_requests >= 1995
    assert rolled.usage_percent >= 99.75
    assert state.cloud_state == "cooldown_quota"
    restarted, restarted_state = _ledger(tmp_path, now=1050)
    assert restarted.usage(now=1050).day_requests >= 1995
    assert restarted_state.cloud_state == "cooldown_quota"


@pytest.mark.parametrize("new_headers", [False, True])
def test_confirmed_day_epoch_survives_key_record_discard_and_restart(tmp_path, new_headers):
    """F3：已由 A 确认的周期边界独立于恢复证明，换 key 不能让旧请求复活。"""
    mono = [100.0]
    ledger, _ = _ledger(tmp_path, now=1000, monotonic=lambda: mono[0])
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    mono[0] = 201
    assert ledger.usage(now=1101).day_requests == 0
    headers = dict(remaining_requests=1999, reset_requests_s=100) if new_headers else {}
    assert ledger.note(10, now=1102, key_fp="key-b", sent_at=1102, **headers).day_requests == 1
    restarted, _ = _ledger(tmp_path, now=1102)
    assert restarted.usage(now=1102).day_requests == 1
    assert json.loads((tmp_path / "quota.json").read_text())["day_epoch"] == 1100


def test_headerless_key_change_discards_unconfirmed_old_reset(tmp_path):
    """F5/R2：B 无配额头也必须清 A 证明，不能在 A 边界后自行解锁。"""
    mono = [100.0]
    state = _RecordingRouting()
    ledger, _ = _ledger(tmp_path, now=1000, state=state, monotonic=lambda: mono[0])
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    mono[0] = 201
    ledger.note(10, now=1101, key_fp="key-b", sent_at=1101)
    assert ledger.usage(now=1101).day_requests >= 1995
    assert state.cloud_state == "cooldown_quota"
    assert not any(event["type"] == "quota_reset_confirmed" for event in state.events)
    assert json.loads((tmp_path / "quota.json").read_text())["reset_record"] is None


def test_unconfirmed_old_key_boundary_cannot_erase_local_high_water(tmp_path):
    """F3/R2：第一条 B 响应不能确认 A，也不能把本地 1900 次降成一次。"""
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({
        "version": 1,
        "events": [{"ts": -10000 + i, "billed_s": 10} for i in range(1900)],
        "server_day_used": 1995, "server_day_used_at": 1000,
        "reset_record": {"reset_at": 1100, "observed_at": 1000, "key_fp": "key-a", "consumed": False},
    }))
    state = _RecordingRouting()
    ledger, _ = _ledger(tmp_path, now=1000, state=state, monotonic=lambda: 100)
    usage = ledger.note(10, now=1101, remaining_requests=1999, reset_requests_s=100, key_fp="key-b", sent_at=1101)
    assert usage.day_requests == 1901
    assert state.cloud_state == "cooldown_quota"
    assert not any(event["type"] == "quota_reset_confirmed" for event in state.events)
    assert json.loads(path.read_text())["day_epoch"] is None


def test_legacy_consumed_record_restores_conservative_floor_on_rollback(tmp_path):
    """F1：旧版本消费时已删下限，只能在回拨时保守封顶，正常周期仍归零。"""
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({
        "version": 1, "events": [{"ts": 1000, "billed_s": 10, "in_server_day": True}],
        "server_day_used": None, "server_day_used_at": None,
        "reset_record": {"reset_at": 1100, "observed_at": 1000, "key_fp": "key-a", "consumed": True},
    }))
    ledger, _ = _ledger(tmp_path, now=1200)
    assert ledger.usage(now=1200).day_requests == 0
    assert ledger.usage(now=1050).day_requests == 2000
    restarted, state = _ledger(tmp_path, now=1050)
    assert restarted.usage(now=1050).day_requests == 2000
    assert state.cloud_state == "cooldown_quota"


def test_headerless_key_change_before_boundary_retains_additive_local_floor(tmp_path):
    """F5：与 Swift 相同序列，B 无头请求在边界前到达，边界后仍为 1996。"""
    mono = [100.0]
    state = _RecordingRouting()
    ledger, _ = _ledger(tmp_path, now=1000, state=state, monotonic=lambda: mono[0])
    ledger.note(10, now=1000, remaining_requests=5, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    mono[0] = 150
    assert ledger.note(10, now=1050, key_fp="key-b", sent_at=1050).day_requests == 1996
    mono[0] = 201
    assert ledger.usage(now=1101).day_requests == 1996
    assert state.cloud_state == "cooldown_quota"
    assert not any(event["type"] == "quota_reset_confirmed" for event in state.events)


def test_rejected_lower_correction_acknowledges_local_requests(tmp_path):
    ledger, _ = _ledger(tmp_path, now=1000)
    ledger.note(10, now=1000, remaining_requests=5)
    assert ledger.note(10, now=1030, remaining_requests=1999).day_requests == 1995
    assert ledger.note(10, now=1031).day_requests == 1996


def test_startup_current_key_invalidates_old_reset_before_load_advance(tmp_path):
    """F5：重启直接使用 B 时，加载也不能先确认 A 的恢复点。"""
    path = tmp_path / "quota.json"
    path.write_text(json.dumps({
        "version": 1, "events": [{"ts": 1000, "billed_s": 10, "in_server_day": True}],
        "server_day_used": 1995, "server_day_used_at": 1000,
        "reset_record": {"reset_at": 1100, "observed_at": 1000, "key_fp": "key-a", "consumed": False},
    }))
    state = _RecordingRouting()
    ledger = QuotaLedger(path, routing=state, now=lambda: 1101, active_key_fp="key-b")
    assert ledger.usage().day_requests == 1995
    assert state.cloud_state == "cooldown_quota"
    assert not any(event["type"] == "quota_reset_confirmed" for event in state.events)
    saved = json.loads(path.read_text())
    assert saved["reset_record"] is None
    assert saved["day_epoch"] is None


def test_same_timestamp_headerless_key_request_crosses_quota_threshold(tmp_path):
    """F5：同秒的 B 无头请求也独立记账，1799 + 1 必须触发额度冷却。"""
    ledger, state = _ledger(tmp_path, now=1000, monotonic=lambda: 100)
    first = ledger.note(10, now=1000, remaining_requests=201, reset_requests_s=100, key_fp="key-a", sent_at=1000)
    assert first.day_requests == 1799
    assert state.cloud_state == "available"
    second = ledger.note(10, now=1000, key_fp="key-b", sent_at=1000)
    assert second.day_requests == 1800
    assert state.cloud_state == "cooldown_quota"
    restarted, restarted_state = _ledger(tmp_path, now=1000)
    assert restarted.usage(now=1000).day_requests == 1800
    assert restarted_state.cloud_state == "cooldown_quota"
