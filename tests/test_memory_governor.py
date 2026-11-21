import sys

import pytest

from realtime_subtitle.resources.memory import (
    MemorySnapshot,
    TierGovernor,
    parse_vm_stat,
    read_memory_snapshot,
)


VM_STAT_SAMPLE = """Mach Virtual Memory Statistics: (page size of 16384 bytes)
Pages free:                               100.
Pages active:                             200.
Pages inactive:                           300.
Pages speculative:                         40.
Pages throttled:                             0.
Pages wired down:                         500.
Pages purgeable:                           60.
"""


def snap(pressure, available=10_000):
    return MemorySnapshot(total_bytes=100_000, available_bytes=available, pressure_level=pressure)


def test_parse_vm_stat_available_memory_uses_header_page_size():
    expected_pages = 100 + 300 + 40 + 60
    assert parse_vm_stat(VM_STAT_SAMPLE) == expected_pages * 16384


def test_parse_vm_stat_returns_none_without_page_size():
    assert parse_vm_stat("Pages free: 100.\nPages inactive: 50.\n") is None


def test_darwin_snapshot_skips_unused_swap_probe(monkeypatch):
    """硬约束 HC-M02：Darwin 快照每 2 秒跑一次，只许探参与决策的指标。

    违反的是什么：_read_darwin_memory_snapshot 每轮都派生一次 `sysctl vm.swapusage`
    子进程。为什么不行：全仓库没有任何决策/显示读 swap_used_bytes，这次子进程
    是纯浪费，且随 AUTO_TIER_POLL_SEC=2.0 持续发生。必须怎么改：删掉这次
    sysctl 调用（以及只为它存在的 parse_swapusage / swap_used_bytes），只保留
    vm_stat + hw.memsize + 压力等级。不要改降档阈值和采样节奏。
    """
    import realtime_subtitle.resources.memory as memory

    calls: list[list[str]] = []

    def _fake_run_text(args):
        calls.append(list(args))
        return ""

    monkeypatch.setattr(memory, "_run_text", _fake_run_text)
    monkeypatch.setattr(memory, "_sysctl_int", lambda name: 1024)
    monkeypatch.setattr(memory, "read_pressure_level", lambda: 1)

    snapshot = memory._read_darwin_memory_snapshot()

    assert snapshot.total_bytes == 1024
    assert snapshot.pressure_level == 1
    swap_calls = [args for args in calls if "swapusage" in " ".join(args)]
    assert not swap_calls, f"违反 HC-M02：仍在派生无用的 swapusage 子进程 {swap_calls}，删掉它"
    assert calls == [["vm_stat"]], f"违反 HC-M02：Darwin 快照多派生了子进程 {calls}"


def test_warning_pressure_must_persist_before_downshift():
    gov = TierGovernor(["9b", "4b", "2b"], "9b", down_after_s=5)
    assert gov.observe(snap(2), now=0) is None
    assert gov.observe(snap(2), now=4.9) is None
    assert gov.observe(snap(2), now=5) == "4b"
    assert gov.current_tier == "4b"


def test_critical_pressure_downshifts_immediately():
    gov = TierGovernor(["9b", "4b", "2b"], "9b", down_after_s=5)
    assert gov.observe(snap(4), now=10) == "4b"


def test_upgrade_blocked_during_cooldown():
    gov = TierGovernor(
        ["9b", "4b", "2b"],
        "9b",
        tier_cost_bytes={"9b": 900, "4b": 400, "2b": 200},
        down_after_s=5,
        up_after_s=10,
        cooldown_s=120,
    )
    assert gov.observe(snap(4), now=0) == "4b"
    assert gov.observe(snap(1, available=10_000), now=10) is None
    assert gov.observe(snap(1, available=10_000), now=119) is None
    assert gov.observe(snap(1, available=10_000), now=120) == "9b"


def test_upgrade_requires_available_memory_for_target_extra_and_headroom():
    gov = TierGovernor(
        ["9b", "4b", "2b"],
        "4b",
        tier_cost_bytes={"9b": 900, "4b": 400, "2b": 200},
        up_after_s=10,
        cooldown_s=0,
        headroom_bytes=100,
    )
    assert gov.observe(snap(1, available=599), now=0) is None
    assert gov.observe(snap(1, available=599), now=10) is None
    assert gov.observe(snap(1, available=600), now=11) == "9b"


def test_tier_bounds_do_not_overflow():
    high = TierGovernor(["9b", "4b", "2b"], "9b", up_after_s=0)
    low = TierGovernor(["9b", "4b", "2b"], "2b", down_after_s=0)
    assert high.observe(snap(1, available=10_000), now=100) is None
    assert low.observe(snap(4), now=100) is None
    assert high.current_tier == "9b"
    assert low.current_tier == "2b"


def test_alternating_pressure_does_not_bounce_up_and_down():
    gov = TierGovernor(
        ["9b", "4b", "2b"],
        "9b",
        tier_cost_bytes={"9b": 900, "4b": 400, "2b": 200},
        down_after_s=1,
        up_after_s=3,
        cooldown_s=120,
    )
    switches = []
    for t in range(600):
        pressure = 1 if t % 2 == 0 else 2
        target = gov.observe(snap(pressure, available=10_000), now=float(t))
        if target is not None:
            switches.append(target)
    assert switches == []
    assert gov.current_tier == "9b"


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS-only memory probe")
def test_read_memory_snapshot_smoke_on_darwin():
    snapshot = read_memory_snapshot()
    assert snapshot.total_bytes is not None
    assert snapshot.available_bytes is not None
    assert snapshot.pressure_level is not None
    assert snapshot.total_bytes > snapshot.available_bytes > 0
