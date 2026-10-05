"""Memory pressure probes and tier decisions for macOS.

This module is intentionally pure at the decision layer: callers provide
snapshots and timestamps, so later integration can test downgrade behavior
without touching clocks or app wiring.
"""

from __future__ import annotations

import ctypes
import dataclasses
import platform
import re
import subprocess
from typing import Iterable


@dataclasses.dataclass(frozen=True)
class MemorySnapshot:
    total_bytes: int | None = None
    available_bytes: int | None = None
    pressure_level: int | None = None
    swap_used_bytes: int | None = None


_VM_STAT_PAGE_RE = re.compile(r"page size of (\d+) bytes")
_VM_STAT_LINE_RE = re.compile(r"^Pages\s+(.+?):\s+([0-9.]+)\.?$")
_SWAP_USED_RE = re.compile(r"used\s*=\s*([0-9.]+)\s*([KMGTP])", re.IGNORECASE)


def _is_darwin() -> bool:
    return platform.system() == "Darwin"


def _run_text(args: list[str]) -> str | None:
    try:
        return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
    except (OSError, subprocess.SubprocessError):
        return None


def _sysctl_int_by_name(name: bytes) -> int | None:
    try:
        libc = ctypes.CDLL("libc.dylib", use_errno=True)
        value = ctypes.c_uint64()
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(name, ctypes.byref(value), ctypes.byref(size), None, 0)
        if rc == 0:
            return int(value.value)
    except (AttributeError, OSError):
        return None
    return None


def _sysctl_int(name: str) -> int | None:
    value = _sysctl_int_by_name(name.encode("utf-8"))
    if value is not None:
        return value
    text = _run_text(["sysctl", "-n", name])
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def read_pressure_level() -> int | None:
    """Return macOS memory pressure level: 1 normal, 2 warning, 4 critical."""
    if not _is_darwin():
        return None
    return _sysctl_int("kern.memorystatus_vm_pressure_level")


def parse_vm_stat(text: str) -> int | None:
    """Parse available memory bytes from macOS vm_stat output."""
    page_size = None
    pages: dict[str, int] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if page_size is None:
            match = _VM_STAT_PAGE_RE.search(line)
            if match:
                page_size = int(match.group(1))
                continue
        match = _VM_STAT_LINE_RE.match(line)
        if not match:
            continue
        key = match.group(1).lower()
        try:
            pages[key] = int(match.group(2).replace(".", ""))
        except ValueError:
            continue
    if page_size is None:
        return None
    available_pages = (
        pages.get("free", 0)
        + pages.get("inactive", 0)
        + pages.get("speculative", 0)
        + pages.get("purgeable", 0)
    )
    return available_pages * page_size


def parse_swapusage(text: str) -> int | None:
    """Parse used swap bytes from sysctl vm.swapusage output."""
    match = _SWAP_USED_RE.search(text)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    scale = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4, "P": 1024**5}[unit]
    return int(value * scale)


def _read_darwin_memory_snapshot() -> MemorySnapshot:
    vm_stat = _run_text(["vm_stat"])
    swapusage = _run_text(["sysctl", "vm.swapusage"])
    return MemorySnapshot(
        total_bytes=_sysctl_int("hw.memsize"),
        available_bytes=parse_vm_stat(vm_stat) if vm_stat else None,
        pressure_level=read_pressure_level(),
        swap_used_bytes=parse_swapusage(swapusage) if swapusage else None,
    )


def _read_psutil_memory_snapshot() -> MemorySnapshot:
    try:
        import psutil  # type: ignore[import-not-found]
    except ImportError:
        return MemorySnapshot()
    mem = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return MemorySnapshot(
        total_bytes=int(mem.total),
        available_bytes=int(mem.available),
        pressure_level=None,
        swap_used_bytes=int(swap.used),
    )


def read_memory_snapshot() -> MemorySnapshot:
    if _is_darwin():
        return _read_darwin_memory_snapshot()
    return _read_psutil_memory_snapshot()


class TierGovernor:
    """Hysteresis state machine for pressure-based tier changes."""

    def __init__(
        self,
        tiers: Iterable[str],
        current_tier: str,
        tier_cost_bytes: dict[str, int] | None = None,
        *,
        down_after_s: float = 5.0,
        up_after_s: float = 300.0,
        cooldown_s: float = 120.0,
        headroom_bytes: int = 0,
    ) -> None:
        self.tiers = list(tiers)
        if not self.tiers:
            raise ValueError("tiers must not be empty")
        if current_tier not in self.tiers:
            raise ValueError("current_tier must be present in tiers")
        self.current_tier = current_tier
        self.tier_cost_bytes = dict(tier_cost_bytes or {})
        self.down_after_s = float(down_after_s)
        self.up_after_s = float(up_after_s)
        self.cooldown_s = float(cooldown_s)
        self.headroom_bytes = int(headroom_bytes)
        self._warning_since: float | None = None
        self._normal_since: float | None = None
        self._last_switch_at: float | None = None
        self._last_down_at: float | None = None

    def observe(self, snapshot: MemorySnapshot, now: float) -> str | None:
        pressure = snapshot.pressure_level
        if pressure is None:
            self._warning_since = None
            self._normal_since = None
            return None

        if pressure >= 2:
            self._normal_since = None
            if self._warning_since is None:
                self._warning_since = now
            if pressure >= 4:
                return self._maybe_down(now)
            if now - self._warning_since >= self.down_after_s:
                return self._maybe_down(now)
            return None

        if pressure == 1:
            self._warning_since = None
            if self._normal_since is None:
                self._normal_since = now
            return self._maybe_up(snapshot, now)

        self._warning_since = None
        self._normal_since = None
        return None

    def _tier_index(self) -> int:
        return self.tiers.index(self.current_tier)

    def _maybe_down(self, now: float) -> str | None:
        index = self._tier_index()
        if index >= len(self.tiers) - 1:
            return None
        # 严重压力也不连跳：两次降档至少隔 down_after_s，给上一次卸载留时间生效
        if self._last_down_at is not None and now - self._last_down_at < self.down_after_s:
            return None
        return self._switch_to(self.tiers[index + 1], now, down=True)

    def _maybe_up(self, snapshot: MemorySnapshot, now: float) -> str | None:
        index = self._tier_index()
        if index <= 0:
            return None
        if self._last_switch_at is not None and now - self._last_switch_at < self.cooldown_s:
            return None
        if self._normal_since is None or now - self._normal_since < self.up_after_s:
            return None
        target = self.tiers[index - 1]
        if not self._has_memory_for(target, snapshot.available_bytes):
            return None
        return self._switch_to(target, now, down=False)

    def _has_memory_for(self, target: str, available_bytes: int | None) -> bool:
        if available_bytes is None:
            return False
        current_cost = self.tier_cost_bytes.get(self.current_tier, 0)
        target_cost = self.tier_cost_bytes.get(target, 0)
        extra = max(0, target_cost - current_cost)
        return available_bytes >= extra + self.headroom_bytes

    def _switch_to(self, tier: str, now: float, *, down: bool) -> str:
        self.current_tier = tier
        self._last_switch_at = now
        if down:
            self._last_down_at = now
            self._warning_since = now
        else:
            self._normal_since = now
        # 滞回是刻意设计：本项目避坑清单第 25 条记录过“超时判定来回震荡”的事故。
        # 升降阈值不对称，再加切换后冷却，能避免内存压力在边界附近时反复升降档。
        return tier
