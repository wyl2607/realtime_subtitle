#!/usr/bin/env python3
"""节点集群运维：一条命令看所有节点状态 / 批量更新。

用法：
  python3 scripts/node/cluster.py status [--json]
  python3 scripts/node/cluster.py update [--force] [id ...]

节点清单读 ~/.config/rslite/nodes.json（RSLITE_CONFIG_DIR 可改）。
status 不 ssh：用本机 tailscale 把节点主机名解析成 IP，校验在 100.64.0.0/10 后
直连 /v1/info（不走代理、钉 IP，CLAUDE.md 第 4 节第 47 条）。token 只进请求头，
不进命令行、不进输出。
update 默认跳过有会话在跑的节点（--force 才装），Mac 走 install_node.sh、
Windows 走 install_node_windows.sh，装完再查一次状态。
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import re
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

PORT = 8791
TAILNET = ipaddress.ip_network("100.64.0.0/10")
HERE = Path(__file__).resolve().parent
MAC_TAILSCALE = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
# 与 install_node.sh 的 ssh 主机校验一致
_HOST_OK = set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._@-")


def config_dir() -> Path:
    return Path(os.environ.get("RSLITE_CONFIG_DIR") or Path.home() / ".config" / "rslite")


def load_nodes() -> list[dict]:
    data = json.loads((config_dir() / "nodes.json").read_text(encoding="utf-8"))
    return data if isinstance(data, list) else data.get("nodes", [])


def read_token(node: dict) -> str:
    p = Path(os.path.expanduser(node["token_file"]))
    if not p.is_absolute():
        p = config_dir() / p
    return p.read_text(encoding="utf-8").strip()


def tailscale_bin() -> str:
    return MAC_TAILSCALE if Path(MAC_TAILSCALE).exists() else (shutil.which("tailscale") or "tailscale")


def resolve_tailnet_ip(host: str) -> str:
    """节点主机名 → Tailscale IPv4；不在 100.64.0.0/10 就拒绝。"""
    env = {**os.environ, "TAILSCALE_BE_CLI": "1"}
    out = subprocess.run([tailscale_bin(), "ip", "-4", host], capture_output=True,
                         text=True, timeout=8, env=env).stdout.strip().splitlines()
    ip = out[0].strip() if out else ""
    try:
        if ipaddress.ip_address(ip) in TAILNET:
            return ip
    except ValueError:
        pass
    raise ValueError("not_tailnet_ip")


def fetch_info(ip: str, port: int, token: str) -> tuple[dict, float]:
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    req = urllib.request.Request(f"http://{ip}:{port}/v1/info",
                                 headers={"Authorization": f"Bearer {token}"})
    t0 = time.perf_counter()
    with opener.open(req, timeout=6) as resp:
        body = json.loads(resp.read().decode("utf-8"))
    return body, (time.perf_counter() - t0) * 1000


def node_status(node: dict) -> dict:
    row = {"id": node.get("id", "?"), "online": False}
    try:
        u = urllib.parse.urlsplit(node["url"])
        ip = resolve_tailnet_ip(u.hostname or "")
        info, ms = fetch_info(ip, u.port or PORT, read_token(node))
    except ValueError as e:
        row["reason"] = str(e) if str(e) == "not_tailnet_ip" else "bad_response"
        return row
    except FileNotFoundError:
        row["reason"] = "token_missing"
        return row
    except urllib.error.HTTPError as e:
        row["reason"] = f"http_{e.code}"
        return row
    except Exception as e:  # noqa: BLE001 - 离线原因只给短语，不夹带内容
        row["reason"] = type(e).__name__
        return row
    if info.get("node_id") and node.get("node_id") and info["node_id"] != node["node_id"]:
        row["reason"] = "node_id_mismatch"
        return row
    asr = info.get("asr") or {}
    row.update({
        "online": True, "latency_ms": round(ms), "asr_model": asr.get("model"),
        "rtf": asr.get("rtf"), "translator": info.get("translator"),
        "translator_ok": info.get("translator_ok"), "sessions": info.get("sessions"),
        "capacity": info.get("capacity"), "mem_pressure": info.get("mem_pressure"),
        "swap_used_gb": info.get("swap_used_gb"), "swapout_rate": info.get("swapout_rate"),
    })
    return row


def all_status(nodes: list[dict]) -> list[dict]:
    if not nodes:
        return []
    with ThreadPoolExecutor(max_workers=len(nodes)) as pool:
        return list(pool.map(node_status, nodes))


def _fmt(v) -> str:
    return "—" if v is None else str(v)


def print_table(rows: list[dict]) -> None:
    head = ["节点", "状态", "延迟ms", "识别模型", "rtf", "翻译", "翻译在线", "会话", "内存", "swapGB", "换页/分"]
    lines = [head]
    for r in rows:
        if not r["online"]:
            lines.append([r["id"], f"离线({r.get('reason', '?')})"] + ["—"] * 9)
            continue
        model = (r.get("asr_model") or "—").rsplit("/", 1)[-1]
        sess = f"{_fmt(r.get('sessions'))}/{_fmt(r.get('capacity'))}"
        tok = r.get("translator_ok")
        ok = "是" if tok is True else "否" if tok is False else "—"
        lines.append([r["id"], "在线", _fmt(r.get("latency_ms")), model, _fmt(r.get("rtf")),
                      _fmt(r.get("translator")), ok, sess, _fmt(r.get("mem_pressure")),
                      _fmt(r.get("swap_used_gb")), _fmt(r.get("swapout_rate"))])
    widths = [max(len(str(line[i])) for line in lines) for i in range(len(head))]
    for line in lines:
        print("  ".join(str(c).ljust(w) for c, w in zip(line, widths)).rstrip())


def _run_ssh(host: str, cmd: list[str]) -> tuple[int, bytes]:
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host] + cmd,
        capture_output=True, timeout=20, stdin=subprocess.DEVNULL,
    )
    # 只要 stdout：stderr 里是 ssh 警告和命令报错（中文 Windows 还是 GBK），混进来会被当成结果
    return result.returncode, result.stdout


def _decode_bytes(data: bytes) -> str:
    return data.decode("utf-8", "replace")


def doctor_node(node: dict) -> dict:
    nid = node.get("id", "?")
    row = {
        "id": nid,
        "ollama": "—",
        "tailscale": "—",
        "disk_gb": None,
        "auto_login": False,
        "commit": "—",
        "issues": "—",
    }
    # 先判断 OS；若 ssh 完全失败则标记离线
    # ssh 自身连不上返回 255；Windows 没有 uname，stdout 为空但能连上
    try:
        rc, out_bytes = _run_ssh(nid, ["uname", "-s"])
    except Exception:
        rc, out_bytes = 255, b""
    if rc == 255:
        return {**row, "issues": "离线（ssh 失败）"}
    text_uname = _decode_bytes(out_bytes)
    os_kind = "mac" if text_uname.strip().splitlines()[:1] == ["Darwin"] else "windows"

    # Ollama
    if os_kind == "mac":
        ollama_cmd = ["curl", "-s", "http://127.0.0.1:11434/api/version"]
    else:
        ollama_cmd = ["curl.exe", "-s", "http://127.0.0.1:11434/api/version"]
    try:
        rc, out_bytes = _run_ssh(nid, ollama_cmd)
        text = _decode_bytes(out_bytes)
        # 尝试解析 JSON 提取 version
        try:
            data = json.loads(text)
            ver = data.get("version")
            row["ollama"] = ver if ver else "未运行"
        except Exception:
            if '"version"' in text or text.strip().startswith("{"):
                m = re.search(r'"version"\s*:\s*"([^"]+)"', text)
                row["ollama"] = m.group(1) if m else "有响应"
            else:
                row["ollama"] = "未运行"
    except Exception:
        row["ollama"] = "未运行"

    # Tailscale
    if os_kind == "mac":
        env_cmd = [
            "env", "TAILSCALE_BE_CLI=1",
            "/Applications/Tailscale.app/Contents/MacOS/Tailscale",
            "status", "--json",
        ]
    else:
        env_cmd = ['"C:\\Program Files\\Tailscale\\tailscale.exe"', "status", "--json"]
    try:
        rc, out_bytes = _run_ssh(nid, env_cmd)
        text = _decode_bytes(out_bytes)
        try:
            data = json.loads(text)
            state = data.get("BackendState")
            row["tailscale"] = state if state else "—"
        except Exception:
            row["tailscale"] = "—"
    except Exception:
        row["tailscale"] = "—"

    # 磁盘余量（GB）
    if os_kind == "mac":
        disk_cmd = ["sh -c 'df -k \"$HOME\" | tail -1'"]
    else:
        disk_cmd = ["powershell", "-NoProfile", "-Command", "(Get-PSDrive C).Free/1GB"]
    try:
        rc, out_bytes = _run_ssh(nid, disk_cmd)
        text = _decode_bytes(out_bytes)
        lines = text.strip().splitlines()
        if lines:
            parts = lines[-1].split()
            if os_kind == "mac" and len(parts) >= 4:
                try:
                    row["disk_gb"] = round(float(parts[3]) / 1024 / 1024, 1)
                except ValueError:
                    pass
            else:
                m = re.search(r"([\d\.]+)", text)
                if m:
                    try:
                        row["disk_gb"] = float(m.group(1))
                    except ValueError:
                        pass
    except Exception:
        pass

    # 自动登录
    if os_kind == "mac":
        auto_cmd = ["defaults", "read", "/Library/Preferences/com.apple.loginwindow", "autoLoginUser"]
    else:
        auto_cmd = [
            "reg", "query",
            "HKLM\\SOFTWARE\\Microsoft\\Windows NT\\CurrentVersion\\Winlogon",
            "/v", "AutoAdminLogon",
        ]
    try:
        rc, out_bytes = _run_ssh(nid, auto_cmd)
        text = _decode_bytes(out_bytes) if rc == 0 else ""
        if os_kind == "mac":
            row["auto_login"] = bool(text.strip() and text.strip() != "(null)")
        else:
            # 只认 AutoAdminLogon 这一行的值为 1（任意输出里出现字符 "1" 不算）
            row["auto_login"] = re.search(r"AutoAdminLogon\s+REG_\w+\s+(0x)?1\s*$", text, re.MULTILINE) is not None
    except Exception:
        row["auto_login"] = False

    # 节点代码版本
    if os_kind == "mac":
        commit_cmd = ["cat", "~/rs-node/.rs_commit", "2>/dev/null"]
    else:
        commit_cmd = ["cmd", "/c", "type", "%USERPROFILE%\\rs-node\\.rs_commit"]
    try:
        rc, out_bytes = _run_ssh(nid, commit_cmd)
        text = _decode_bytes(out_bytes) if rc == 0 else ""
        val = text.strip()
        row["commit"] = val if val else "—"
    except Exception:
        row["commit"] = "—"

    # 结论列
    issues = []
    if row["ollama"] == "未运行":
        issues.append("Ollama 未运行")
    if row["tailscale"] not in ("Running", "—"):
        issues.append(f"Tailscale {row['tailscale']}")
    if row["disk_gb"] is not None and row["disk_gb"] < 10:
        issues.append("磁盘<10GB")
    if not row["auto_login"]:
        issues.append("未开自动登录：重启后需手动登录")
    if not issues:
        row["issues"] = "OK"
    else:
        row["issues"] = "; ".join(issues)
    return row


def doctor(nodes: list[dict], ids: list[str]) -> list[dict]:
    chosen = [n for n in nodes if not ids or n.get("id") in ids]
    unknown = set(ids) - {n.get("id") for n in chosen}
    # 不打印 error，只在结果里标记
    results: list[dict] = []
    def work(node: dict) -> dict:
        return doctor_node(node)
    if not chosen:
        return []
    with ThreadPoolExecutor(max_workers=max(1, len(chosen))) as pool:
        results = list(pool.map(work, chosen))
    # 对未知 id 的处理：若有，附加离线记录（不影响主流）
    for unk in sorted(unknown):
        results.append({
            "id": unk,
            "ollama": "—",
            "tailscale": "—",
            "disk_gb": None,
            "auto_login": False,
            "commit": "—",
            "issues": "节点不存在",
        })
    return results


def print_doctor_table(rows: list[dict]) -> None:
    head = ["节点", "Ollama", "Tailscale", "磁盘GB", "自动登录", "代码版本", "结论"]
    lines = [head]
    for r in rows:
        auto_text = "是" if r.get("auto_login") else "否"
        disk_text = f"{r['disk_gb']:.1f}" if r.get("disk_gb") is not None else "—"
        lines.append([
            r.get("id", "?"),
            r.get("ollama", "—"),
            r.get("tailscale", "—"),
            disk_text,
            auto_text,
            r.get("commit", "—"),
            r.get("issues", "—"),
        ])
    widths = [max(len(str(line[i])) for line in lines) for i in range(len(head))]
    for line in lines:
        print("  ".join(str(c).ljust(w) for c, w in zip(line, widths)).rstrip())


def detect_os(host: str) -> str:
    # 中文 Windows 的 cmd 会用 GBK 回「'uname' 不是内部或外部命令」，按 UTF-8 解会抛错：
    # 只认 Darwin，其余字节一律容错解码
    out = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", host, "uname -s"],
                         capture_output=True, timeout=20, stdin=subprocess.DEVNULL).stdout
    return "mac" if out.decode("utf-8", "replace").strip().splitlines()[:1] == ["Darwin"] else "windows"


def update(nodes: list[dict], ids: list[str], force: bool) -> int:
    chosen = [n for n in nodes if not ids or n.get("id") in ids]
    unknown = set(ids) - {n.get("id") for n in chosen}
    if unknown:
        print(f"❌ nodes.json 里没有：{', '.join(sorted(unknown))}", file=sys.stderr)
        return 2
    results: list[tuple[str, str]] = []
    for node in chosen:
        nid = node.get("id", "")
        if not nid or not set(nid) <= _HOST_OK or nid.startswith("-"):
            results.append((nid, "失败（id 不合法）"))
            continue
        st = node_status(node)
        if st["online"] and (st.get("sessions") or 0) > 0 and not force:
            results.append((nid, f"跳过（{st['sessions']} 个会话在用，--force 才装）"))
            continue
        script = "install_node.sh" if detect_os(nid) == "mac" else "install_node_windows.sh"
        print(f"== {nid}：bash {script} {nid}")
        rc = subprocess.run(["bash", str(HERE / script), nid]).returncode
        if rc != 0:
            results.append((nid, f"失败（{script} 退出码 {rc}）"))
            continue
        after = node_status(node)
        if after["online"] and after.get("translator_ok") is not False:
            results.append((nid, "成功"))
        else:
            results.append((nid, f"装完但状态不对（{after.get('reason') or '翻译不在线'}）"))
    print("\n汇总：")
    for nid, msg in results:
        print(f"  {nid}: {msg}")
    return 0 if all(m.startswith(("成功", "跳过")) for _, m in results) else 1


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="节点集群运维")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--json", action="store_true")
    u = sub.add_parser("update")
    u.add_argument("--force", action="store_true")
    u.add_argument("ids", nargs="*")
    d = sub.add_parser("doctor")
    d.add_argument("--json", action="store_true")
    d.add_argument("ids", nargs="*")
    args = ap.parse_args(argv)
    nodes = load_nodes()
    if args.cmd == "doctor":
        rows = doctor(nodes, args.ids or [])
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print_doctor_table(rows)
        return 0
    if args.cmd == "status":
        rows = all_status(nodes)
        if args.json:
            print(json.dumps(rows, ensure_ascii=False, indent=1))
        else:
            print_table(rows)
        return 0
    return update(nodes, args.ids, args.force)


if __name__ == "__main__":
    sys.exit(main())
