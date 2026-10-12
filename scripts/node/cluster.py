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
    args = ap.parse_args(argv)
    nodes = load_nodes()
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
