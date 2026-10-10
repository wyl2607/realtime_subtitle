#!/usr/bin/env python3
"""
Multi-node routing acceptance probe for rslite (TK-007 Done criteria #5).

Starts two fake nodes with different RTF (A worse, B better), B comes online later
and goes offline later. Runs rslite in --mode auto and verifies RFC Done criteria 5:
- F1 不重复: 可见字幕轨严格唯一; 空列表判失败; 节点句(id 为 null)用 t0/t1 重叠或正文重复判重
- F2 不丢句: 假 worker 按 worker.py 在每条 hello 时把 id 与样本时钟清零; 用 t0/t1 覆盖整段音频(间隙 ≤ 静音阈值+切换预算); 时间顺序 A 段→B 段→A 段; 读 ev=replace, 被替换的本机句不得留在可见轨
- F3 静音点: 至少两次连续 select node=node-b 之后, stdout 出现 status「混合·node-b」; 切换音频点由 A 尾句 t1 与 B 首句 t0 定位, 落在 wav 中同一扇 ≥0.6s 静音窗; 旧会话 drain 的 audio_s 落在该窗; 新会话首条 final 的 t0 不早于窗起点
- F4 回退: fallback failed_node=node-b 行之后出现 select node=node-a, 且其后有 t0 晚于最后一条 B 句的 A final
- F5 整行正则匹配 rslite.route select 的全字段; B 条数对应在线窗口; 删重复项
- F6 try/finally 停两个 gateway; rslite 用进程组 TERM→超时 KILL; rmtree 临时目录(含 token、转录)
- F7 先扫描再打印, 打印只留打分字段
"""

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import signal
import struct
import sys
import tempfile
import textwrap
import time
import wave
from pathlib import Path

# Repo root for imports
REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

from realtime_subtitle.node.gateway import Gateway as GatewayClass

RSLITE = str(REPO / "macos-native" / ".build" / "release" / "rslite")

# Use fixed ports to avoid port allocation issues
PORT_A = 18791
PORT_B = 18792

ROUTE_SELECT_RE = re.compile(
    r"^rslite\.route select node=(?P<node>[^\s]+) score=(?P<score>-?\d+(?:\.\d+)?) "
    r"quality=(?P<quality>-?\d+(?:\.\d+)?) speed=(?P<speed>-?\d+(?:\.\d+)?) "
    r"penalties=(?P<penalties>-?\d+(?:\.\d+)?) bonus=(?P<bonus>-?\d+(?:\.\d+)?) "
    r"rtt_ms=(?P<rtt_ms>\d+(?:\.\d+)?)$"
)


def make_fake_worker(name: str, seg_seconds: float = 5.0, drain_file: Path | None = None) -> str:
    seg_bytes = int(16000 * 2 * seg_seconds)
    drain_str = str(drain_file) if drain_file else ""
    return textwrap.dedent(f"""\
import json, sys
stdin, out = sys.stdin.buffer, sys.stdout.buffer
SEG = {seg_bytes}
NAME = {json.dumps(name)}
DRAIN_PATH = {json.dumps(drain_str)}
total = 0
seg_start = 0
seg_id = 0
global_seg = 0
def emit(d):
    out.write((json.dumps(d, ensure_ascii=False) + "\\n").encode())
    out.flush()
def close_seg():
    global seg_start, seg_id, global_seg
    if total - seg_start < 3200:
        return
    seg_id += 1
    global_seg += 1
    emit({{"ev": "final", "id": seg_id, "a0": seg_start / 32000.0, "a1": total / 32000.0, "text": "%s-seg%d" % (NAME, global_seg)}})
    emit({{"ev": "translation", "id": seg_id, "text": "%s-zh%d" % (NAME, global_seg)}})
    seg_start = total
while True:
    b = stdin.read(1)
    if not b:
        sys.exit(0)
    if b == b"{{":
        t = json.loads(b + stdin.readline())["type"]
        if t == "hello":
            total = 0
            seg_start = 0
            seg_id = 0
            emit({{"ev": "ready", "cold": True, "load_s": 0.0, "engine": {{}}}})
        elif t == "flush":
            close_seg()
        elif t == "drain":
            close_seg()
            if DRAIN_PATH:
                try:
                    with open(DRAIN_PATH, "a") as f:
                        f.write(json.dumps({{"ev": "drained", "audio_s": total / 32000.0}}) + "\\n")
                except Exception:
                    pass
            emit({{"ev": "drained"}})
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


def find_silence_windows(wav_path: str, threshold: float = 0.003, min_duration: float = 0.6) -> list[tuple[float, float]]:
    with wave.open(str(wav_path), "rb") as wf:
        sr = wf.getframerate()
        nframes = wf.getnframes()
        frames = wf.readframes(nframes)
    samples = struct.unpack(f"<{len(frames)//2}h", frames)
    chunk_size = int(sr * 0.05)  # 50ms chunks
    silences = []
    current_silence = None
    for i in range(0, len(samples), chunk_size):
        chunk = samples[i:i + chunk_size]
        if not chunk:
            break
        rms = (sum(s * s for s in chunk) / len(chunk)) ** 0.5 / 32768.0
        t = i / sr
        if rms < threshold:
            if current_silence is None:
                current_silence = [t, t + len(chunk) / sr]
            else:
                current_silence[1] = t + len(chunk) / sr
        else:
            if current_silence:
                if current_silence[1] - current_silence[0] >= min_duration:
                    silences.append((current_silence[0], current_silence[1]))
                current_silence = None
    if current_silence and current_silence[1] - current_silence[0] >= min_duration:
        silences.append((current_silence[0], current_silence[1]))
    return silences


def parse_stdout_events(stdout_text):
    events = []
    for line in stdout_text.splitlines():
        try:
            ev = json.loads(line)
            events.append(ev)
        except json.JSONDecodeError:
            pass
    return events


def get_wav_path() -> Path:
    env_wav = os.environ.get("WAV_FILE")
    if env_wav and Path(env_wav).exists():
        return Path(env_wav)
    candidates = [
        Path("/tmp/german_test_long_silence.wav"),
        Path("/tmp/german_test.wav"),
    ]
    for c in candidates:
        if c.exists() and c.stat().st_size > 1_000_000:
            return c
    return candidates[0]


async def run_probe():
    wav_file = get_wav_path()
    if not wav_file.exists():
        print(f"Error: test wav file not found at {wav_file}")
        sys.exit(1)

    temp_root = Path("/tmp/tk007")
    temp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="multi_node_", dir=temp_root))
    print(f"Working dir: {tmp}")

    gw_a = None
    gw_b = None
    proc = None
    f_out = None
    f_err = None

    try:
        logging.basicConfig(
            filename=str(tmp / "gateway.log"),
            level=logging.INFO,
            format="%(asctime)s [node.gateway] %(levelname)s %(message)s",
            force=True,
        )

        drain_file_a = tmp / "node_a_drained.json"

        # Node A: worse RTF (0.50), starts first and stays running
        uds_dir_a = tmp / "rs-node-a"
        uds_dir_a.mkdir(mode=0o700)
        (uds_dir_a / "node_id").write_text("node-A-slow")

        worker_script_a = tmp / "fake_worker_a.py"
        worker_script_a.write_text(make_fake_worker("A", seg_seconds=5.0, drain_file=drain_file_a))

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
        worker_script_b.write_text(make_fake_worker("B", seg_seconds=5.0))

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

        out_f = tmp / "rslite_out.log"
        err_f = tmp / "rslite_err.log"
        f_out = open(out_f, "w")
        f_err = open(err_f, "w")

        print("--- Starting rslite with both nodes configured (B offline initially) ---")
        proc = await asyncio.create_subprocess_exec(
            RSLITE, "--headless", "--source", f"file:{wav_file}",
            "--mode", "auto", "--src", "de-DE", "--dst", "zh-Hans",
            stdout=f_out, stderr=f_err,
            env={**os.environ, "RSLITE_CONFIG_DIR": str(config_dir)},
            start_new_session=True,
        )

        # Wait 15s, then start node B (simulate B coming online)
        await asyncio.sleep(15)
        print("--- Starting node B (better RTF comes online) ---")
        await start_gateway(gw_b)
        await asyncio.wait_for(gw_b.tcp_ready.wait(), 5)
        print(f"✓ Gateway B started on port {PORT_B} (RTF=0.02)")

        # Wait for migration and active window:
        # Probe 2 at t~30s (15s after B started): B wins 1st time
        # Probe 3 at t~60s (45s after B started): B wins 2nd time -> pendingMigration
        # Migration happens at silence point around t~61s
        # Keep B running until t~85s (70s after B started) so B has ~24s active window (4-5 segments)
        print("--- Waiting for migration and active window on node B ---")
        await asyncio.sleep(70)

        # Stop node B (simulate offline) -> triggers fallback
        print("--- Stopping node B (simulate offline) ---")
        await gw_b.stop()
        gw_b = None

        # Wait for fallback and rslite completion (WAV duration ~105s, finishes at ~106-110s)
        print("--- Waiting for fallback and completion ---")
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=50)
        except asyncio.TimeoutError:
            print("rslite timed out; terminating process group...")
            pgid = os.getpgid(proc.pid)
            with contextlib.suppress(ProcessLookupError):
                os.killpg(pgid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), timeout=3.0)
            except asyncio.TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGKILL)
                await proc.wait()
            rc = 124

        print(f"rslite exited with code {rc}")

        # Flush files
        f_out.flush()
        f_err.flush()
        f_out.close()
        f_err.close()
        f_out = None
        f_err = None

        await asyncio.sleep(0.5)

        # Read outputs into memory before cleanup
        stdout_text = out_f.read_text(errors="replace")
        stderr_text = err_f.read_text(errors="replace")
        gateway_log_path = tmp / "gateway.log"
        gateway_log = gateway_log_path.read_text(errors="replace") if gateway_log_path.exists() else ""

        drained_events = []
        if drain_file_a.exists():
            for line in drain_file_a.read_text(errors="replace").splitlines():
                try:
                    drained_events.append(json.loads(line))
                except Exception:
                    pass

    finally:
        # F6: try/finally clean up rslite process group, gateways, and tmp dir
        if f_out is not None:
            f_out.close()
        if f_err is not None:
            f_err.close()
        if proc is not None and proc.returncode is None:
            try:
                pgid = os.getpgid(proc.pid)
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(pgid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except asyncio.TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(pgid, signal.SIGKILL)
                    with contextlib.suppress(Exception):
                        await proc.wait()
            except Exception:
                pass
        if gw_b is not None:
            with contextlib.suppress(Exception):
                await gw_b.stop()
        if gw_a is not None:
            with contextlib.suppress(Exception):
                await gw_a.stop()
        shutil.rmtree(tmp, ignore_errors=True)

    # === Verification & Reporting ===
    import scripts.bench.multi_node_verify as verify
    sys.exit(verify.verify_probe(
        stdout_text=stdout_text,
        stderr_text=stderr_text,
        gateway_log=gateway_log,
        drained_events=drained_events,
        wav_file=str(wav_file),
        rc=rc,
        token_a=token_a,
        token_b=token_b
    ))

if __name__ == "__main__":
    asyncio.run(run_probe())
