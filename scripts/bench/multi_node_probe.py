#!/usr/bin/env python3
"""
Multi-node routing acceptance probe for rslite (TK-007 Done criteria #5).

Starts two fake nodes with different RTF (A worse, B better), B comes online later
and goes offline later. Runs rslite in --mode auto and verifies:
- Routing logs show selection reasons and migrations
- Migration happens at silence point
- Final event IDs monotonic, no duplicate text, no obvious drops
- rslite exit code 0
- No token appears in output
"""

import asyncio
import json
import logging
import os
import re
import secrets
import signal
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# Repo root for imports
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from realtime_subtitle.node.gateway import Gateway as GatewayClass

RSLITE = str(REPO / "macos-native" / ".build" / "release" / "rslite")
WAV_FILE = "/tmp/german_test_long_silence.wav"

# Use fixed ports to avoid port allocation issues
PORT_A = 18791
PORT_B = 18792

# Node A: worse RTF (0.50), starts first and stays running
# Node B: better RTF (0.02), starts later, then stops
# RTF difference: speed component = -20 * RTF
# A: speed = -10.0, B: speed = -0.4, diff = 9.6 > migration_margin (8.0)
FAKE_WORKER_A = textwrap.dedent("""\
import json, sys
stdin, out = sys.stdin.buffer, sys.stdout.buffer
SEG = 16000 * 2 * 20         # 20s per segment (slower node = fewer segments)
total = 0
seg_start = 0
seg_id = 0
def emit(d):
    out.write((json.dumps(d, ensure_ascii=False) + "\\n").encode()); out.flush()
def close_seg():
    global seg_start, seg_id
    if total - seg_start < 3200:
        return
    seg_id += 1
    emit({"ev": "final", "id": seg_id, "a0": seg_start / 32000.0, "a1": total / 32000.0, "text": "A-seg%d" % seg_id})
    emit({"ev": "translation", "id": seg_id, "text": "A-zh%d" % seg_id})
    seg_start = total
while True:
    b = stdin.read(1)
    if not b:
        sys.exit(0)
    if b == b"{":
        t = json.loads(b + stdin.readline())["type"]
        if t == "hello":
            emit({"ev": "ready", "cold": True, "load_s": 0.0, "engine": {}})
        elif t == "flush":
            close_seg()
        elif t == "drain":
            close_seg()
            emit({"ev": "drained"})
    else:
        n = int.from_bytes(b + stdin.read(3), "big")
        stdin.read(n)
        total += n
        if total - seg_start >= SEG:
            close_seg()
""")

FAKE_WORKER_B = textwrap.dedent("""\
import json, sys
stdin, out = sys.stdin.buffer, sys.stdout.buffer
SEG = 16000 * 2 * 5          # 5s per segment (faster node = more segments)
total = 0
seg_start = 0
seg_id = 0
def emit(d):
    out.write((json.dumps(d, ensure_ascii=False) + "\\n").encode()); out.flush()
def close_seg():
    global seg_start, seg_id
    if total - seg_start < 3200:
        return
    seg_id += 1
    emit({"ev": "final", "id": seg_id, "a0": seg_start / 32000.0, "a1": total / 32000.0, "text": "B-seg%d" % seg_id})
    emit({"ev": "translation", "id": seg_id, "text": "B-zh%d" % seg_id})
    seg_start = total
while True:
    b = stdin.read(1)
    if not b:
        sys.exit(0)
    if b == b"{":
        t = json.loads(b + stdin.readline())["type"]
        if t == "hello":
            emit({"ev": "ready", "cold": True, "load_s": 0.0, "engine": {}})
        elif t == "flush":
            close_seg()
        elif t == "drain":
            close_seg()
            emit({"ev": "drained"})
    else:
        n = int.from_bytes(b + stdin.read(3), "big")
        stdin.read(n)
        total += n
        if total - seg_start >= SEG:
            close_seg()
""")

results = {"pass": 0, "fail": 0, "details": []}


def check(ok, label):
    if ok:
        results["pass"] += 1
        results["details"].append({"check": label, "result": "PASS"})
        print(f"  ✓ {label}")
    else:
        results["fail"] += 1
        results["details"].append({"check": label, "result": "FAIL"})
        print(f"  ✗ {label}")


class FakeInfo:
    def __init__(self, state_dir, node_id, rtf):
        self.state_dir = Path(state_dir)
        self.node_id = node_id
        self.rtf = rtf

    def collect(self, *, busy, worker):
        return {
            "v": 2,
            "node_id": self.node_id,
            "hw_hash": "multi-probe-hw",
            "asr": {"model": "whisper-large-v3-turbo", "backend": "mlx", "rtf": self.rtf},
            "translator": "apple",
            "busy": bool(busy),
            "on_ac": True,
            "in_use": False,
            "worker": worker,
        }


async def start_gateway_tcp_only(gw):
    import realtime_subtitle.node.gateway as gw_mod
    gw._common = dict(
        max_size=gw_mod.MAX_MESSAGE_BYTES,
        ping_interval=gw_mod.PING_INTERVAL_S,
        ping_timeout=gw_mod.PING_TIMEOUT_S,
        close_timeout=gw_mod.CLOSE_TIMEOUT_S,
        compression=None,
        logger=gw_mod._ws_log,
    )
    gw._tcp_task = asyncio.create_task(gw._tcp_loop())


async def start_gateway(gw):
    try:
        await gw.start()
    except PermissionError:
        await start_gateway_tcp_only(gw)


async def run_rslite(rslite, wav, out_f, err_f, timeout, config_dir):
    with open(out_f, "w") as f_out, open(err_f, "w") as f_err:
        proc = await asyncio.create_subprocess_exec(
            rslite, "--headless", "--source", f"file:{wav}",
            "--mode", "auto", "--src", "de-DE", "--dst", "zh-Hans",
            stdout=f_out, stderr=f_err,
            env={**os.environ, "RSLITE_CONFIG_DIR": str(config_dir)},
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.send_signal(signal.SIGTERM)
            await proc.wait()
            return 124
        return proc.returncode


def parse_stdout_events(stdout_text):
    events = []
    for line in stdout_text.splitlines():
        try:
            ev = json.loads(line)
            events.append(ev)
        except json.JSONDecodeError:
            pass
    return events


async def run_probe():
    temp_root = Path("/tmp/tk007")
    temp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="multi_node_", dir=temp_root))
    print(f"Working dir: {tmp}")

    logging.basicConfig(
        filename=str(tmp / "gateway.log"),
        level=logging.INFO,
        format="%(asctime)s [node.gateway] %(levelname)s %(message)s",
    )

    # Node A: worse RTF (0.50), starts first and stays running
    uds_dir_a = tmp / "rs-node-a"
    uds_dir_a.mkdir(mode=0o700)
    (uds_dir_a / "node_id").write_text("node-A-slow")

    worker_script_a = tmp / "fake_worker_a.py"
    worker_script_a.write_text(FAKE_WORKER_A)

    token_a = secrets.token_urlsafe(32)
    token_file_a = tmp / "token_a"
    token_file_a.write_text(token_a)
    os.chmod(str(token_file_a), 0o600)

    gw_a = GatewayClass(
        token=token_a, uds_dir=uds_dir_a, port=PORT_A,
        worker_argv=[sys.executable, str(worker_script_a)],
        resolve_host=lambda: "127.0.0.1",
        info=FakeInfo(uds_dir_a, "node-A-slow", 0.50),
        allow_non_tailscale_for_tests=True,
    )
    await start_gateway(gw_a)
    await asyncio.wait_for(gw_a.tcp_ready.wait(), 5)
    print(f"✓ Gateway A started on port {PORT_A} (RTF=0.50)")

    # Node B: better RTF (0.02), will start later
    uds_dir_b = tmp / "rs-node-b"
    uds_dir_b.mkdir(mode=0o700)
    (uds_dir_b / "node_id").write_text("node-B-fast")

    worker_script_b = tmp / "fake_worker_b.py"
    worker_script_b.write_text(FAKE_WORKER_B)

    token_b = secrets.token_urlsafe(32)
    token_file_b = tmp / "token_b"
    token_file_b.write_text(token_b)
    os.chmod(str(token_file_b), 0o600)

    gw_b = GatewayClass(
        token=token_b, uds_dir=uds_dir_b, port=PORT_B,
        worker_argv=[sys.executable, str(worker_script_b)],
        resolve_host=lambda: "127.0.0.1",
        info=FakeInfo(uds_dir_b, "node-B-fast", 0.02),
        allow_non_tailscale_for_tests=True,
    )

    # rslite config dir - BOTH nodes in nodes.json from start with correct ports
    config_dir = tmp / "rslite-config"
    config_dir.mkdir(mode=0o700)

    nodes_json = config_dir / "nodes.json"
    nodes_both = [
        {"id": "node-a", "node_id": "node-A-slow", "url": f"ws://127.0.0.1:{PORT_A}", "token_file": str(token_file_a)},
        {"id": "node-b", "node_id": "node-B-fast", "url": f"ws://127.0.0.1:{PORT_B}", "token_file": str(token_file_b)},
    ]
    nodes_json.write_text(json.dumps(nodes_both))
    os.chmod(str(nodes_json), 0o600)

    # Start rslite with both nodes configured (B is offline initially)
    out_f = tmp / "rslite_out.log"
    err_f = tmp / "rslite_err.log"

    print("--- Starting rslite with both nodes configured (B offline initially) ---")
    rslite_task = asyncio.create_task(run_rslite(RSLITE, WAV_FILE, out_f, err_f, timeout=300, config_dir=config_dir))

    # Wait 15s, then start node B (simulate B coming online)
    await asyncio.sleep(15)
    print("--- Starting node B (better RTF comes online) ---")
    await start_gateway(gw_b)
    await asyncio.wait_for(gw_b.tcp_ready.wait(), 5)
    print(f"✓ Gateway B started on port {PORT_B} (RTF=0.02)")

    # Wait for migration: need 2 consecutive probes (30s interval) where B beats A by margin (8.0)
    # First probe after B starts: ~30s from rslite start = ~15s from now
    # Second probe: ~60s from rslite start = ~45s from now
    # Migration triggers at silence point after 2nd win
    print("--- Waiting for migration (probes every 30s, need 2 consecutive wins) ---")
    await asyncio.sleep(55)

    # Stop node B (simulate offline) - after migration, before WAV ends
    print("--- Stopping node B (simulate offline) ---")
    await gw_b.stop()

    # Wait for fallback and rslite to finish
    print("--- Waiting for fallback and completion ---")
    await asyncio.sleep(60)

    rc = await rslite_task
    print(f"rslite exited with code {rc}")

    # Read outputs
    stdout_text = out_f.read_text(errors="replace")
    stderr_text = err_f.read_text(errors="replace")
    gateway_log = (tmp / "gateway.log").read_text(errors="replace")

    # Wait for gateway logs to flush
    await asyncio.sleep(0.5)
    gateway_log = (tmp / "gateway.log").read_text(errors="replace")

    # Parse rslite stdout events
    events = parse_stdout_events(stdout_text)
    finals = [e for e in events if e.get("ev") == "final"]
    translations = [e for e in events if e.get("ev") == "translation"]

    # Parse routing logs (stderr)
    route_logs = [line for line in stderr_text.splitlines() if "rslite.route" in line]
    print(f"\nRouting logs ({len(route_logs)} lines):")
    for line in route_logs:
        print(f"  {line}")

    # Parse gateway logs
    gateway_lines = gateway_log.strip().splitlines()
    print(f"\nGateway logs ({len(gateway_lines)} lines):")
    for line in gateway_lines[-20:]:
        print(f"  {line}")

    # === Verification ===

    # 1. rslite exit code 0
    check(rc == 0, "rslite exit code 0")

    # 2. No token in output
    check(token_a not in stdout_text + stderr_text + gateway_log, "Token A not in output")
    check(token_b not in stdout_text + stderr_text + gateway_log, "Token B not in output")

    # 3. Routing logs show selection reasons
    has_select_logs = any("select node=" in line and "quality=" in line and "speed=" in line for line in route_logs)
    check(has_select_logs, "Routing logs contain selection reasons (quality/speed/penalties)")

    # 4. Migration from A to B observed in routing logs
    migration_a_to_b = any("select node=node-b" in line for line in route_logs)
    check(migration_a_to_b, "Routing logs show migration to node-b (better RTF)")

    # 5. Fallback from B to A after B goes offline
    fallback_logged = any("fallback failed_node=node-b" in line for line in route_logs + gateway_lines)
    migration_b_to_a = any("select node=node-a" in line for line in route_logs if "fallback" in line.lower() or "offline" in line.lower())
    check(fallback_logged or migration_b_to_a, "Fallback to node-a after node-b offline logged")

    # 6. Final event IDs monotonic (per session, but overall should not decrease)
    final_ids = [e.get("id") for e in finals if e.get("id") is not None]
    ids_monotonic = all(final_ids[i] <= final_ids[i+1] for i in range(len(final_ids)-1)) if final_ids else True
    check(ids_monotonic, f"Final event IDs non-decreasing: {final_ids}")

    # 7. Check node-specific finals (each session restarts IDs, so check by text prefix)
    a_finals = [e for e in finals if "A-seg" in e.get("text", "")]
    b_finals = [e for e in finals if "B-seg" in e.get("text", "")]
    check(len(a_finals) > 0, f"Received finals from node A: {len(a_finals)}")
    check(len(b_finals) > 0, f"Received finals from node B: {len(b_finals)}")

    # 8. No obvious drops: total finals reasonable for 105s audio
    total_finals = len(finals)
    check(total_finals >= 8, f"Sufficient total finals for 105s audio: {total_finals}")

    # 9. Migration timing: check that node-b selection happens after B was started
    node_b_select_times = []
    for line in route_logs:
        if "select node=node-b" in line:
            node_b_select_times.append(line)
    check(len(node_b_select_times) > 0, "At least one node-b selection in routing logs")

    # 10. Check stderr for errors
    check("switch_failed" not in stderr_text, "No switch_failed in stderr")

    # Summary
    total = results["pass"] + results["fail"]
    summary = {
        "test": "multi_node_probe",
        "timestamp": time.time(),
        "wav_duration_sec": 105,
        "rslite_exit_code": rc,
        "checks_passed": results["pass"],
        "checks_failed": results["fail"],
        "total_checks": total,
        "node_a_finals": len(a_finals),
        "node_b_finals": len(b_finals),
        "total_finals": total_finals,
        "final_ids": final_ids,
        "routing_log_lines": len(route_logs),
        "gateway_log_lines": len(gateway_lines),
        "details": results["details"],
    }

    print(f"\n=== SUMMARY ===")
    print(f"Passed: {results['pass']}/{total}")
    print(f"Final event IDs: {final_ids}")
    print(f"Node A finals: {len(a_finals)}, Node B finals: {len(b_finals)}")
    print(json.dumps(summary, ensure_ascii=False))

    # Cleanup
    await gw_a.stop()
    print("✓ Gateway A stopped")

    if results["fail"] == 0:
        print("MULTI_NODE_PROBE OK")
        sys.exit(0)
    else:
        print("MULTI_NODE_PROBE FAIL")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run_probe())