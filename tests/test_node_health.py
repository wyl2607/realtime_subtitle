"""节点自报机器状态（mem_pressure / swapout_rate）与路由罚分（2026-10-12）。

真机背景：lmm 的 M4 mini 上 Adobe Acrobat 泄漏到 64GB，交换 35GB，节点翻译慢一倍，
退出 Acrobat 后 swap_used_gb 仍高，但 30 秒内 Swapouts 不再增长；累计量不能当当前压力。
"""
from __future__ import annotations

import types

from realtime_subtitle.node import info as info_mod
from realtime_subtitle.remote.node_client import NodeASRInfo, NodeInfo
from realtime_subtitle.remote.router import RouteCandidate, RoutingDecider


def test_parse_pressure_level():
    assert info_mod.parse_pressure_level("1\n") == "normal"
    assert info_mod.parse_pressure_level("2") == "warn"
    assert info_mod.parse_pressure_level("4") == "critical"
    assert info_mod.parse_pressure_level("garbage") is None
    assert info_mod.parse_pressure_level("3") is None


def test_parse_swap_used_gb():
    assert info_mod.parse_swap_used_gb("total = 35840.00M  used = 34785.94M  free = 1054.06M  (encrypted)") == 34.0
    assert info_mod.parse_swap_used_gb("total = 0.00M  used = 0.00M  free = 0.00M") == 0.0
    assert info_mod.parse_swap_used_gb("vm.swapusage: total = 2.00G  used = 1.25G") == 1.2
    assert info_mod.parse_swap_used_gb("nonsense") is None


def test_parse_vm_stat_swapouts():
    assert info_mod.parse_vm_stat_swapouts("Swapouts:                              10772944.\n") == 10772944
    assert info_mod.parse_vm_stat_swapouts("Pages free: 1.\nSwapouts: 1,234.\n") == 1234
    assert info_mod.parse_vm_stat_swapouts("nonsense") is None


def test_memory_probe_is_cached_and_first_swapout_rate_is_null(tmp_path, monkeypatch):
    monkeypatch.setattr(info_mod.sys, "platform", "darwin")
    monkeypatch.setattr(info_mod.time, "monotonic", lambda: 100.0)
    calls = []

    def run(argv):
        calls.append(argv)
        if argv[-1] == "kern.memorystatus_vm_pressure_level":
            return "2\n"
        if argv[-1] == "vm.swapusage":
            return "total = 1024.00M  used = 512.00M"
        if argv[0] == "vm_stat":
            return "Swapouts:                              1000.\n"
        raise AssertionError(argv)

    ni = info_mod.NodeInfo(run=run, state_dir=tmp_path)
    assert ni.memory() == ("warn", 0.5, None)
    assert ni.memory() == ("warn", 0.5, None)
    assert len(calls) == 3  # 第二次走 10 秒缓存，不重复 sysctl/vm_stat

    def boom(argv):
        raise OSError("no sysctl")

    assert info_mod.NodeInfo(run=boom, state_dir=tmp_path).memory() == (None, None, None)


def test_memory_probe_computes_swapout_rate_after_second_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(info_mod.sys, "platform", "darwin")
    now = {"t": 100.0}
    swapouts = {"count": 1000}
    monkeypatch.setattr(info_mod.time, "monotonic", lambda: now["t"])

    def run(argv):
        if argv[-1] == "kern.memorystatus_vm_pressure_level":
            return "1\n"
        if argv[-1] == "vm.swapusage":
            return "total = 40960.00M  used = 35840.00M"
        if argv[0] == "vm_stat":
            return f"Swapouts:                              {swapouts['count']}.\n"
        raise AssertionError(argv)

    ni = info_mod.NodeInfo(run=run, state_dir=tmp_path)
    assert ni.memory() == ("normal", 35.0, None)

    swapouts["count"] = 1000
    now["t"] = 111.0
    assert ni.memory() == ("normal", 35.0, 0.0)

    swapouts["count"] = 2000
    now["t"] = 122.0
    assert ni.memory() == ("normal", 35.0, 5454.5)


def test_swapout_rate_short_interval_and_vm_stat_failure_are_null(tmp_path, monkeypatch):
    monkeypatch.setattr(info_mod.sys, "platform", "darwin")
    now = {"t": 100.0}
    swapouts = {"count": 1000}
    monkeypatch.setattr(info_mod.time, "monotonic", lambda: now["t"])

    def run(argv):
        if argv[-1] == "kern.memorystatus_vm_pressure_level":
            return "1\n"
        if argv[-1] == "vm.swapusage":
            return "total = 1024.00M  used = 0.00M"
        if argv[0] == "vm_stat":
            if swapouts["count"] < 0:
                raise OSError("vm_stat")
            return f"Swapouts:                              {swapouts['count']}.\n"
        raise AssertionError(argv)

    ni = info_mod.NodeInfo(run=run, state_dir=tmp_path)
    assert ni.memory()[2] is None
    ni._memory = None
    now["t"] = 104.0
    swapouts["count"] = 1400
    assert ni.memory()[2] is None

    ni._memory = None
    now["t"] = 115.0
    swapouts["count"] = -1
    assert ni.memory() == ("normal", 0.0, None)


def test_windows_memory_from_fake_ctypes():
    gb = 1024 ** 3

    class Struct:
        def __init__(self):
            self.dwLength = 0

    def fake_status(ptr):
        st = ptr.obj
        st.dwMemoryLoad = 95
        st.ullTotalPhys, st.ullAvailPhys = 16 * gb, 1 * gb
        st.ullTotalPageFile, st.ullAvailPageFile = 40 * gb, 15 * gb
        return 1

    fake = types.SimpleNamespace(
        Structure=Struct, c_ulong=int, c_ulonglong=int, sizeof=lambda _t: 64,
        byref=lambda o: types.SimpleNamespace(obj=o),
        windll=types.SimpleNamespace(kernel32=types.SimpleNamespace(GlobalMemoryStatusEx=fake_status)),
    )
    pressure, swap = info_mod.windows_memory(fake)
    assert pressure == "critical"
    assert swap == 10.0  # 已提交 25GB − 物理已用 15GB


def _info(node_id, rtf, **kw):
    return NodeInfo(v=2, node_id=node_id, hw_hash="h", asr=NodeASRInfo(model="whisper-large-v3-turbo", backend="mlx", rtf=rtf),
                    translator="apple", busy=False, on_ac=True, in_use=False, worker="warm", **kw)


def _cand(cid, info):
    return RouteCandidate(id=cid, expected_node_id=info.node_id, info=info, rtt_ms=20.0, is_local=False, offline_until=None)


def test_router_avoids_critical_or_currently_swapping_node():
    base = _cand("base", _info("n1", 0.08))
    for kw in ({"mem_pressure": "critical"}, {"swapout_rate": 6000.0}):
        sick = _cand("sick", _info("n2", 0.02, **kw))
        assert RoutingDecider.choose([base, sick], now=0, local_accurate=True).id == "base"
    healthy = _cand("fast", _info("n2", 0.02))
    assert RoutingDecider.choose([base, healthy], now=0, local_accurate=True).id == "fast"


def test_router_ignores_accumulated_swap_and_null_swapout_rate():
    plain = RoutingDecider.score(_cand("a", _info("n1", 0.08)), now=0, local_accurate=True)
    ok = RoutingDecider.score(_cand("a", _info("n1", 0.08, mem_pressure="normal", swap_used_gb=35.0, swapout_rate=0.0)),
                              now=0, local_accurate=True)
    assert plain.score == ok.score
    assert "mem=normal/0.0" in ok.reason

    missing = RoutingDecider.score(_cand("a", _info("n1", 0.08, swap_used_gb=35.0, swapout_rate=None)),
                                   now=0, local_accurate=True)
    assert plain.score == missing.score


def test_router_swapout_rate_penalties():
    base = _cand("base", _info("n1", 0.08))
    light = _cand("light", _info("n2", 0.02, swapout_rate=600.0))
    heavy = _cand("heavy", _info("n2", 0.02, swapout_rate=6000.0))
    fast = _cand("fast", _info("n2", 0.02))
    assert RoutingDecider.score(fast, now=0, local_accurate=True).score - RoutingDecider.score(
        light, now=0, local_accurate=True
    ).score == 6.0
    assert RoutingDecider.choose([base, light], now=0, local_accurate=True).id == "base"
    assert RoutingDecider.choose([base, heavy], now=0, local_accurate=True).id == "base"


def test_node_info_parses_new_fields_and_ignores_bad_types():
    d = {"v": 2, "node_id": "n", "asr": {}, "mem_pressure": "warn", "swap_used_gb": 12.5, "swapout_rate": 6000.0}
    ni = NodeInfo.from_dict(d)
    assert ni.mem_pressure == "warn" and ni.swap_used_gb == 12.5 and ni.swapout_rate == 6000.0
    ni = NodeInfo.from_dict({"v": 2, "node_id": "n", "asr": {}, "mem_pressure": 3, "swap_used_gb": True, "swapout_rate": True})
    assert ni.mem_pressure is None and ni.swap_used_gb is None and ni.swapout_rate is None
