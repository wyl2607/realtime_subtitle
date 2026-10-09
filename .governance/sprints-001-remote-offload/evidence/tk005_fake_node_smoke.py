import asyncio, json, logging, os, re, signal, sys, tempfile, textwrap
from pathlib import Path

REPO = Path(os.path.expanduser("~/projects/rs-tk-005"))
sys.path.insert(0, str(REPO))
from realtime_subtitle.node import gateway as gw_mod
from realtime_subtitle.node.gateway import Gateway

TOKEN = "smoke-test-token-tk005-abcdefg-1234"
NODE_ID = "smoke-node-001"

FAKE_WORKER = textwrap.dedent("""\
import json, sys
stdin, out = sys.stdin.buffer, sys.stdout.buffer
SEG = 16000 * 2 * 15         # 每 15 秒产出一句（真 worker 单段上限 15s），时间戳按收到的样本数计
total = 0                    # 已收字节
seg_start = 0
seg_id = 0
def emit(d):
    out.write((json.dumps(d, ensure_ascii=False) + "\\n").encode()); out.flush()
def close_seg():
    global seg_start, seg_id
    if total - seg_start < 3200:
        return
    seg_id += 1
    emit({"ev": "final", "id": seg_id, "a0": seg_start / 32000.0, "a1": total / 32000.0, "text": "seg%d" % seg_id})
    emit({"ev": "translation", "id": seg_id, "text": "zh%d" % seg_id})
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

results = {"pass": 0, "fail": 0}

def check(ok, label):
    if ok:
        results["pass"] += 1
        print(f"  ✓ {label}")
    else:
        results["fail"] += 1
        print(f"  ✗ {label}")

async def start_gateway_tcp_only(gw):
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
        # Codex 沙箱可能禁止 AF_UNIX socket；本 smoke 只验证 rslite 的 TCP v2 会话。
        await start_gateway_tcp_only(gw)

class FakeInfo:
    def __init__(self, state_dir):
        self.state_dir = Path(state_dir)

    def collect(self, *, busy, worker):
        return {
            "v": 2,
            "node_id": (self.state_dir / "node_id").read_text().strip(),
            "hw_hash": "smoke-hw",
            "asr": {"model": "fake", "backend": "fake", "rtf": 0.01},
            "translator": "fake",
            "busy": bool(busy),
            "on_ac": True,
            "in_use": False,
            "worker": worker,
        }

SMOKE_HOME = None

async def run_rslite(rslite, wav, out_f, err_f, timeout):
    with open(out_f, "w") as f_out, open(err_f, "w") as f_err:
        proc = await asyncio.create_subprocess_exec(
            rslite, "--headless", "--source", f"file:{wav}",
            "--mode", "hybrid", "--src", "de-DE", "--dst", "zh-Hans",
            stdout=f_out, stderr=f_err,
            # 节点清单放临时 HOME，绝不碰用户真实的 ~/.config/rslite（Coordinator 16:50 修）
            env={**os.environ, "RSLITE_CONFIG_DIR": str(SMOKE_HOME / ".config" / "rslite")},
        )
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.send_signal(signal.SIGTERM)
            await proc.wait()
            return 124
        return proc.returncode

def mode_texts(stdout_text):
    modes = []
    for line in stdout_text.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        text = ev.get("text")
        if ev.get("ev") == "status" and isinstance(text, str) and text.startswith("模式："):
            modes.append(text.removeprefix("模式："))
    return modes

async def run_smoke():
    temp_root = Path("/tmp/tk005/codex")
    temp_root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix="tk005_smoke_", dir=temp_root))
    logging.basicConfig(
        filename=str(tmp / "gateway.log"),
        level=logging.INFO,
        format="%(asctime)s [node.gateway] %(levelname)s %(message)s",
    )
    worker_script = tmp / "fake_worker.py"
    worker_script.write_text(FAKE_WORKER)
    
    uds_dir = tmp / "rs-node"
    uds_dir.mkdir(mode=0o700)
    os.chmod(str(uds_dir), 0o700)
    (uds_dir / "node_id").write_text(NODE_ID)

    gw = Gateway(
        token=TOKEN, uds_dir=uds_dir, port=0,
        worker_argv=[sys.executable, str(worker_script)],
        resolve_host=lambda: "127.0.0.1",
        info=FakeInfo(uds_dir),
        allow_non_tailscale_for_tests=True,
    )
    await start_gateway(gw)
    try:
        await asyncio.wait_for(gw.tcp_ready.wait(), 5)
    except asyncio.TimeoutError:
        gateway_log = tmp / "gateway.log"
        print("SMOKE FAIL: gateway TCP 监听未就绪")
        if gateway_log.exists():
            for line in gateway_log.read_text(errors="replace").strip().splitlines()[-5:]:
                print(f"    gateway: {line}")
        await gw.stop()
        sys.exit(1)
    port = gw.tcp_port
    print(f"✓ gateway 已启动 port={port}")

    config_dir = tmp / "rslite-config"
    config_dir.mkdir(mode=0o700)
    token_file = config_dir / "token"
    token_file.write_text(TOKEN)
    os.chmod(str(token_file), 0o600)

    rslite = str(REPO / "macos-native" / ".build" / "release" / "rslite")
    wav = os.path.expanduser("~/projects/rs-mac-native-data/concat5.wav")

    global SMOKE_HOME
    SMOKE_HOME = tmp / "home"
    default_config = SMOKE_HOME / ".config" / "rslite"
    default_config.mkdir(parents=True, exist_ok=True)
    default_nodes = default_config / "nodes.json"
    backup_nodes = None

    try:
        print("--- 测试 1：正常握手 ---")
        nodes = [{
            "id": "smoke", "node_id": NODE_ID,
            "url": f"ws://127.0.0.1:{port}",
            "token_file": str(token_file),
        }]
        default_nodes.write_text(json.dumps(nodes))
        os.chmod(str(default_nodes), 0o600)
        
        out_f = tmp / "out1.log"
        err_f = tmp / "err1.log"
        rc1 = await run_rslite(rslite, wav, out_f, err_f, timeout=100)

        stdout_text = out_f.read_text(errors="replace")
        stderr_text = err_f.read_text(errors="replace")
        gateway_text = (tmp / "gateway.log").read_text(errors="replace")
        modes = mode_texts(stdout_text)

        # gateway 的 session_end 在客户端退出后才写日志：轮询等到它，别和日志写入抢跑
        for _ in range(100):
            gateway_text = (tmp / "gateway.log").read_text(errors="replace")
            if "session_end" in gateway_text:
                break
            await asyncio.sleep(0.1)

        evs = []
        for line in stdout_text.splitlines():
            try:
                evs.append(json.loads(line))
            except ValueError:
                pass
        node_finals = [e for e in evs if e.get("ev") == "final" and str(e.get("text", "")).startswith("[smoke] ")]
        node_trans = [e for e in evs if e.get("ev") == "translation" and str(e.get("text", "")).startswith("[smoke] ")]
        replaces = [e for e in evs if e.get("ev") == "replace"]
        replaced_total = 0
        for e in replaces:
            m = re.search(r"replaced=(\d+)", e.get("text", ""))
            replaced_total += int(m.group(1)) if m else 0
        m_end = re.search(r"session_end .*audio_bytes=(\d+) events_out=(\d+)", gateway_text)
        audio_bytes = int(m_end.group(1)) if m_end else -1
        events_out = int(m_end.group(2)) if m_end else -1
        first_hybrid = next((i for i, m in enumerate(modes) if m.startswith("混合·")), None)
        print(f"    证据：模式序列={modes}")
        print(f"    证据：节点 final={len(node_finals)} translation={len(node_trans)} 替换事件={len(replaces)} 共替换本机句={replaced_total}")
        print(f"    证据：gateway audio_bytes={audio_bytes} events_out={events_out}")

        check(rc1 == 0, "rslite 正常跑完整个回放（退出码 0）")
        check("switch_failed" not in stderr_text, "stderr 无 switch_failed")
        check("no_available_node" not in stderr_text, "stderr 无 no_available_node（没有中途判节点不可用）")
        check(first_hybrid is not None and modes[0] == "本机", "起步本机，随后进入混合")
        check(first_hybrid is not None and all(not m == "本机" for m in modes[first_hybrid:]),
              "进入混合后模式序列中不再出现「本机」")
        check(first_hybrid is not None and modes[-1].startswith("混合·"), "回放结束时仍是混合模式")
        check("session_start transport=tcp" in gateway_text and "session_attached" in gateway_text,
              "gateway 日志：TCP 会话建立并 attach worker")
        check(audio_bytes >= 2_000_000, "gateway 收到整段音频（约 67s×32000B）")
        check(events_out - (1 + len(node_finals) + len(node_trans)) in (0, 1),
              "gateway 发出的事件数 = ready + 客户端收到的 final/translation（+ 结尾 drained）")
        check(len(node_finals) >= 4 and len(node_trans) == len(node_finals),
              "客户端输出里有 ≥4 条节点精修 final，且每条都有译文")
        check(all(e.get("t0") is not None and e.get("t1") is not None and 0 <= e["t0"] < e["t1"] <= 75
                  for e in node_finals), "节点句时间戳 t0<t1 且落在回放时间轴内")
        check(replaced_total >= 1, "P5：节点句至少替换了一条本机句")
        check("worker_crashed" not in gateway_text, "gateway 无 worker_crashed")
        check(TOKEN not in stdout_text + stderr_text + gateway_text, "token 未出现在 stdout/stderr/gateway 日志")

        for line in stderr_text.strip().splitlines()[-5:]:
            print(f"    stderr: {line}")
        for line in gateway_text.strip().splitlines()[-5:]:
            print(f"    gateway: {line}")

        print("--- 测试 2：node_id 不匹配 ---")
        bad_nodes = [{
            "id": "smoke", "node_id": "WRONG-NODE-ID",
            "url": f"ws://127.0.0.1:{port}",
            "token_file": str(token_file),
        }]
        default_nodes.write_text(json.dumps(bad_nodes))
        os.chmod(str(default_nodes), 0o600)

        out2_f = tmp / "out2.log"
        err2_f = tmp / "err2.log"
        session_starts_before = gateway_text.count("session_start transport=tcp")
        _ = await run_rslite(rslite, wav, out2_f, err2_f, timeout=5)

        stderr2_text = err2_f.read_text(errors="replace")
        gateway2_text = (tmp / "gateway.log").read_text(errors="replace")
        out2_text = out2_f.read_text(errors="replace")
        check("rejected=node_id_mismatch" in stderr2_text, "node_id 不匹配：stderr 明确报 node_id_mismatch 拒绝")
        check(gateway2_text.count("session_start") == session_starts_before,
              "node_id 不匹配：gateway 没有新会话（没发 token 的 WS 升级、没发音频）")
        check("[smoke]" not in out2_text and "混合·" not in out2_text, "node_id 不匹配：未进入混合、无节点结果")
        check(TOKEN not in stderr2_text + out2_text, "node_id 不匹配：token 未出现在输出")
        for line in stderr2_text.strip().splitlines()[-3:]:
            print(f"    stderr: {line}")


    finally:
        if default_nodes.exists():
            default_nodes.unlink()
        if backup_nodes and backup_nodes.exists():
            backup_nodes.rename(default_nodes)

    await gw.stop()
    print(f"✓ gateway 已停止")

    total = results["pass"] + results["fail"]
    if results["fail"] == 0:
        print(f"SMOKE OK ({results['pass']}/{total})")
    else:
        print(f"SMOKE FAIL ({results['fail']}/{total} failed)")
        sys.exit(1)

if __name__ == "__main__":
    asyncio.run(run_smoke())
