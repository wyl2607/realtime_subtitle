"""scripts/node/cluster.py：集群状态 / 批量更新（不连真实节点，全部打桩）。"""
import importlib.util
import json
import urllib.error
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "a" * 64


def _load():
    spec = importlib.util.spec_from_file_location("cluster", ROOT / "scripts" / "node" / "cluster.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def cl(tmp_path, monkeypatch):
    mod = _load()
    (tmp_path / "tokens").mkdir()
    (tmp_path / "tokens" / "m1.token").write_text(TOKEN)
    (tmp_path / "tokens" / "w1.token").write_text("b" * 64)
    nodes = [
        {"id": "m1", "node_id": "nodeidm1aaaa", "url": "ws://m1.tail.ts.net:8791", "token_file": "tokens/m1.token"},
        {"id": "w1", "node_id": "nodeidw1aaaa", "url": "ws://w1.tail.ts.net:8791", "token_file": "tokens/w1.token"},
    ]
    (tmp_path / "nodes.json").write_text(json.dumps(nodes))
    monkeypatch.setenv("RSLITE_CONFIG_DIR", str(tmp_path))
    return mod


def _info(node_id, sessions=0, **extra):
    return {"v": 2, "node_id": node_id, "asr": {"model": "mlx/whisper-large-v3-turbo", "rtf": 0.23},
            "translator": "apple", "translator_ok": True, "sessions": sessions, "capacity": 2, **extra}


def _patch_net(monkeypatch, cl, infos, ips=None, seen=None):
    ips = ips or {"m1.tail.ts.net": "100.64.0.7", "w1.tail.ts.net": "100.64.0.8"}

    def resolve(host):
        ip = ips.get(host, "")
        if not ip.startswith("100."):
            raise ValueError("not_tailnet_ip")
        return ip

    def fetch(ip, port, token):
        if seen is not None:
            seen.append((ip, port, token))
        r = infos[ip]
        if isinstance(r, Exception):
            raise r
        return r, 12.0

    monkeypatch.setattr(cl, "resolve_tailnet_ip", resolve)
    monkeypatch.setattr(cl, "fetch_info", fetch)


def test_status_one_online_one_offline(cl, monkeypatch, capsys):
    _patch_net(monkeypatch, cl, {"100.64.0.7": _info("nodeidm1aaaa"), "100.64.0.8": TimeoutError()})
    assert cl.main(["status"]) == 0
    out = capsys.readouterr().out
    lines = out.strip().splitlines()
    assert len(lines) == 3
    assert "m1" in lines[1] and "在线" in lines[1] and "whisper-large-v3-turbo" in lines[1]
    assert "w1" in lines[2] and "离线(TimeoutError)" in lines[2]
    assert TOKEN not in out


def test_status_json_and_token_only_in_header(cl, monkeypatch, capsys):
    seen = []
    _patch_net(monkeypatch, cl, {"100.64.0.7": _info("nodeidm1aaaa", mem_pressure="warn", swap_used_gb=12.3),
                                 "100.64.0.8": _info("nodeidw1aaaa", sessions=1)}, seen=seen)
    cl.main(["status", "--json"])
    out = capsys.readouterr().out
    rows = {r["id"]: r for r in json.loads(out)}
    assert rows["m1"]["mem_pressure"] == "warn" and rows["m1"]["swap_used_gb"] == 12.3
    assert rows["w1"]["sessions"] == 1 and rows["w1"]["capacity"] == 2
    assert TOKEN not in out
    assert ("100.64.0.7", 8791, TOKEN) in seen  # 钉 IP 直连，token 走请求头


def test_status_rejects_non_tailnet_ip(cl, monkeypatch):
    _patch_net(monkeypatch, cl, {}, ips={"m1.tail.ts.net": "8.8.8.8", "w1.tail.ts.net": ""})
    rows = cl.all_status(cl.load_nodes())
    assert all(not r["online"] and r["reason"] == "not_tailnet_ip" for r in rows)


def test_status_flags_node_id_mismatch_and_http_errors(cl, monkeypatch):
    err = urllib.error.HTTPError("u", 401, "x", None, None)
    _patch_net(monkeypatch, cl, {"100.64.0.7": _info("someoneelse1"), "100.64.0.8": err})
    rows = {r["id"]: r for r in cl.all_status(cl.load_nodes())}
    assert rows["m1"]["reason"] == "node_id_mismatch"
    assert rows["w1"]["reason"] == "http_401"


def test_resolve_tailnet_ip_validates_range(cl, monkeypatch):
    class R:
        def __init__(self, out):
            self.stdout = out
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: R("100.105.163.59\n"))
    assert cl.resolve_tailnet_ip("mini2") == "100.105.163.59"
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: R("192.168.1.5\n"))
    with pytest.raises(ValueError):
        cl.resolve_tailnet_ip("mini2")


def _patch_update(monkeypatch, cl, os_by_id, rc=0):
    calls = []

    class P:
        def __init__(self, code):
            self.returncode = code

    monkeypatch.setattr(cl, "detect_os", lambda host: os_by_id[host])

    def run(argv, *a, **k):
        calls.append(argv)
        return P(rc if isinstance(rc, int) else rc[argv[-1]])
    monkeypatch.setattr(cl.subprocess, "run", run)
    return calls


def test_update_skips_busy_node_unless_forced(cl, monkeypatch, capsys):
    _patch_net(monkeypatch, cl, {"100.64.0.7": _info("nodeidm1aaaa", sessions=1),
                                 "100.64.0.8": _info("nodeidw1aaaa")})
    calls = _patch_update(monkeypatch, cl, {"m1": "mac", "w1": "windows"})
    assert cl.main(["update"]) == 0
    out = capsys.readouterr().out
    assert "m1: 跳过" in out and "w1: 成功" in out
    assert [Path(c[1]).name for c in calls] == ["install_node_windows.sh"]
    assert all(TOKEN not in " ".join(c) for c in calls)

    calls.clear()
    assert cl.main(["update", "--force", "m1"]) == 0
    assert [Path(c[1]).name for c in calls] == ["install_node.sh"] and calls[0][-1] == "m1"


def test_update_failure_does_not_stop_others(cl, monkeypatch, capsys):
    _patch_net(monkeypatch, cl, {"100.64.0.7": _info("nodeidm1aaaa"), "100.64.0.8": _info("nodeidw1aaaa")})
    _patch_update(monkeypatch, cl, {"m1": "mac", "w1": "windows"}, rc={"m1": 1, "w1": 0})
    assert cl.main(["update"]) == 1
    out = capsys.readouterr().out
    assert "m1: 失败（install_node.sh 退出码 1）" in out and "w1: 成功" in out


def test_update_unknown_id(cl, monkeypatch, capsys):
    assert cl.main(["update", "nope"]) == 2
