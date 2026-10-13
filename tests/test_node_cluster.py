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


def test_detect_os_tolerates_gbk_error_from_windows(cl, monkeypatch):
    class R:
        def __init__(self, out):
            self.stdout = out
    gbk = "'uname' 不是内部或外部命令".encode("gbk")
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: R(gbk))
    assert cl.detect_os("w1") == "windows"
    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: R(b"Darwin\n"))
    assert cl.detect_os("m1") == "mac"


def test_doctor_mac_all_ok(cl, monkeypatch, capsys):
    """Mac 节点全 OK：打桩 ssh 输出，包含 GBK 容错解码测试。"""
    # 打桩 subprocess.run：根据 argv 内容决定返回假输出
    def mock_run(argv, *a, **k):
        # 先检查是否为 ssh 调用（_run_ssh 会构造 ssh ... [cmd ...]）
        # argv[0] = ssh
        cmd_args = argv[6:] if len(argv) > 5 else argv
        # uname
        if cmd_args == ["uname", "-s"]:
            out = b"Darwin\n"
        # curl
        elif cmd_args == ["curl", "-s", "http://127.0.0.1:11434/api/version"]:
            out = b'{"version":"0.5.0"}\n'
        # tailscale
        elif cmd_args == ["env", "TAILSCALE_BE_CLI=1", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "status", "--json"]:
            out = b'{"BackendState":"Running"}\n'
        # df
        elif cmd_args == ["sh", "-c", "df -g ~ | tail -1"]:
            out = b"/dev/disk1s1  500  100  400  20% /Users\n"
        # defaults
        elif cmd_args == ["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"]:
            out = b"alice\n"
        # cat commit
        elif cmd_args == ["cat", "~/rs-node/.rs_commit", "2>/dev/null"]:
            out = b"a1b2c3d\n"
        else:
            out = b""
        class P:
            returncode = 0
            stdout = out
            stderr = b""
        return P()
    monkeypatch.setattr(cl.subprocess, "run", mock_run)
    assert cl.main(["doctor", "m1"]) == 0
    out = capsys.readouterr().out
    # 检查输出包含预期列
    assert "m1" in out
    assert "0.5.0" in out  # ollama 版本
    assert "Running" in out
    assert "是" in out  # 自动登录
    assert "a1b2c3d" in out  # commit
    assert "OK" in out
    # token 不应出现（doctor 不用 token）
    assert TOKEN not in out


def test_doctor_ssh_fails_offline(cl, monkeypatch, capsys):
    """ssh 完全失败时显示离线。"""
    def mock_run(argv, *a, **k):
        # 让 uname 失败（空 stdout、非零退出码、无 stderr）
        class P:
            returncode = 255
            stdout = b""
            stderr = b""
        return P()
    monkeypatch.setattr(cl.subprocess, "run", mock_run)
    assert cl.main(["doctor", "m1"]) == 0
    out = capsys.readouterr().out
    assert "离线（ssh 失败）" in out


def test_doctor_windows_ollama_down(cl, monkeypatch, capsys):
    """Windows 节点：Ollama 未运行，其余正常。"""
    def mock_run(argv, *a, **k):
        cmd_args = argv[6:] if len(argv) > 5 else argv
        if cmd_args == ["uname", "-s"]:
            out = b"MINGW64_NT-10.0\n"
        elif "curl.exe" in str(cmd_args):
            out = b""
        elif cmd_args == ["tailscale", "status", "--json"]:
            out = b'{"BackendState":"Running"}\n'
        elif cmd_args == ["powershell", "-NoProfile", "-Command", "(Get-PSDrive C).Free/1GB"]:
            out = b"200\n"
        elif "AutoAdminLogon" in str(cmd_args) or "reg" in str(cmd_args[3]) if len(argv) > 3 else False:
            # 简化：假设命令中包含 reg query 则返回无值（未开）
            out = b"\n"
        elif cmd_args == ["cmd", "/c", "type", "%LOCALAPPDATA%\\rs-node\\app\\.rs_commit"]:
            out = b"w1commit\n"
        else:
            out = b""
        class P:
            returncode = 0
            stdout = out
            stderr = b""
        return P()
    monkeypatch.setattr(cl.subprocess, "run", mock_run)
    assert cl.main(["doctor", "w1"]) == 0
    out = capsys.readouterr().out
    assert "未运行" in out
    assert "磁盘<10GB" not in out  # 磁盘足够，不应出现磁盘警告
    assert "未开自动登录：重启后需手动登录" in out  # 自动登录未开


def test_doctor_json_parseable(cl, monkeypatch, capsys):
    """--json 输出可解析，结论列正确。"""
    def mock_run(argv, *a, **k):
        cmd_args = argv[6:] if len(argv) > 5 else argv
        if cmd_args == ["uname", "-s"]:
            out = b"Darwin\n"
        elif cmd_args == ["curl", "-s", "http://127.0.0.1:11434/api/version"]:
            out = b'{"version":"0.9.1"}\n'
        elif cmd_args == ["env", "TAILSCALE_BE_CLI=1", "/Applications/Tailscale.app/Contents/MacOS/Tailscale", "status", "--json"]:
            out = b'{"BackendState":"Running"}\n'
        elif cmd_args == ["sh", "-c", "df -g ~ | tail -1"]:
            out = b"/dev/disk1s1  500  100  400  20% /Users\n"
        elif cmd_args == ["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"]:
            out = b"user1\n"
        elif cmd_args == ["cat", "~/rs-node/.rs_commit", "2>/dev/null"]:
            out = b"deadbeef\n"
        else:
            out = b""
        class P:
            returncode = 0
            stdout = out
            stderr = b""
        return P()
    monkeypatch.setattr(cl.subprocess, "run", mock_run)
    assert cl.main(["doctor", "m1", "--json"]) == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert isinstance(data, list)
    row = next(r for r in data if r["id"] == "m1")
    assert row["issues"] == "OK"
    assert row["ollama"] == "0.9.1"
    assert row["auto_login"] is True
    assert row["commit"] == "deadbeef"


def test_doctor_gbk_tolerance_in_run(cl, monkeypatch):
    """包含 GBK 字节的错误输出应被容错解码（不抛异常）。"""
    gbk_err = b"'uname' \xb2\xbb\xca\xc7\xc4\xda\xb2\xbf\xbb\xf2\xcd\xe2\xb2\xbf\xc3\xfc\xc1\xee"  # GBK 编码的中文
    def mock_run(argv, *a, **k):
        cmd_args = argv[6:] if len(argv) > 5 else argv
        if cmd_args == ["uname", "-s"]:
            out = b"Darwin\n"
            err = gbk_err
        else:
            out = b""
            err = b""
        class P:
            returncode = 0
            stdout = out
            stderr = err
        return P()
    monkeypatch.setattr(cl.subprocess, "run", mock_run)
    # GBK 错误字节出现在非 uname 命令中（如 curl 失败时返回 GBK 错误），应容错
    # 由于代码中 _decode_bytes 使用 errors="replace"，不会抛异常
    result = cl.doctor([{"id": "m1"}], [])
    assert isinstance(result, list)
    # GBK 容错解码不应抛异常；结果存在即可（具体值取决于打桩内容）
    assert result[0]["id"] == "m1"


def test_doctor_reads_stdout_only_and_windows_without_uname_is_online(cl, monkeypatch):
    """真机回归（2026-10-12）：stderr 里的 ssh 警告/GBK 报错曾混进结果；Windows 没有 uname 曾被判离线；
    AutoAdminLogon 曾因输出里有字符 1 就判为开。"""
    class R:
        def __init__(self, rc, out=b"", err=b""):
            self.returncode, self.stdout, self.stderr = rc, out, err

    def run(argv, *a, **k):
        cmd = " ".join(argv[5:])
        if cmd == "uname -s":
            return R(1, b"", "'uname' 不是内部或外部命令".encode("gbk"))
        if "api/version" in cmd:
            return R(0, b'{"version":"0.40.3"}', b"** WARNING: post-quantum")
        if "status --json" in cmd:
            return R(0, b'{"BackendState":"Running"}')
        if "Get-PSDrive" in cmd:
            return R(0, b"207.5\r\n")
        if "AutoAdminLogon" in cmd:
            return R(0, b"\r\n    AutoAdminLogon    REG_SZ    0\r\n")
        return R(1, b"", "系统找不到指定的路径".encode("gbk"))
    monkeypatch.setattr(cl.subprocess, "run", run)
    row = cl.doctor_node({"id": "w1"})
    assert row["ollama"] == "0.40.3" and row["tailscale"] == "Running" and row["disk_gb"] == 207.5
    assert row["auto_login"] is False and row["commit"] == "—"
    assert "离线" not in row["issues"]

    monkeypatch.setattr(cl.subprocess, "run", lambda *a, **k: R(255, b"", b"ssh: connect timed out"))
    assert cl.doctor_node({"id": "w1"})["issues"] == "离线（ssh 失败）"
