import sys

import pytest

from realtime_subtitle.resources.memory import (
    MemorySnapshot,
    TierGovernor,
    parse_swapusage,
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
    return MemorySnapshot(total_bytes=100_000, available_bytes=available, pressure_level=pressure, swap_used_bytes=0)


def test_parse_vm_stat_available_memory_uses_header_page_size():
    expected_pages = 100 + 300 + 40 + 60
    assert parse_vm_stat(VM_STAT_SAMPLE) == expected_pages * 16384


def test_parse_vm_stat_returns_none_without_page_size():
    assert parse_vm_stat("Pages free: 100.\nPages inactive: 50.\n") is None


def test_parse_swapusage_used_bytes():
    text = "vm.swapusage: total = 2048.00M  used = 1.50G  free = 512.00M  (encrypted)"
    assert parse_swapusage(text) == int(1.5 * 1024**3)


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
