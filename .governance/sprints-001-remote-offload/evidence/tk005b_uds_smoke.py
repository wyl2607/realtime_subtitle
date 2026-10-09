#!/usr/bin/env python3
# /// script
# dependencies = [
#   "websockets",
# ]
# ///
import asyncio
import json
import logging
import os
import signal
import subprocess
import sys
import textwrap
from pathlib import Path

# 仓库根目录
REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from realtime_subtitle.node import gateway as gw_mod
from realtime_subtitle.node import info as node_info
from realtime_subtitle.node.gateway import Gateway
from websockets.asyncio.server import unix_serve

TOKEN = "dummy-token-for-uds-testing-12345678"
NODE_ID = "smoke-uds-node-001"

FAKE_WORKER = textwrap.dedent("""\
import json, sys
stdin, out = sys.stdin.buffer, sys.stdout.buffer
SEG = 16000 * 2 * 3
total = 0
seg_start = 0
seg_id = 0

def emit(d):
    out.write((json.dumps(d, ensure_ascii=False) + "\\n").encode())
    out.flush()

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


def check(ok: bool, label: str):
    if ok:
        results["pass"] += 1
        print(f"  PASS: {label}")
    else:
        results["fail"] += 1
        print(f"  FAIL: {label}")


async def start_gateway_uds_only(gw: Gateway):
    gw._prepare_uds()
    gw._common = dict(
        max_size=gw_mod.MAX_MESSAGE_BYTES,
        ping_interval=gw_mod.PING_INTERVAL_S,
        ping_timeout=gw_mod.PING_TIMEOUT_S,
        close_timeout=gw_mod.CLOSE_TIMEOUT_S,
        compression=None,
        logger=gw_mod._ws_log,
    )
    gw._uds_server = await unix_serve(
        gw._handle_uds,
        str(gw.uds_path),
        process_request=gw._process_request_uds,
        **gw._common,
    )
    os.chmod(gw.uds_path, 0o600)


class FakeInfo:
    def __init__(self, uds_dir: Path, hw_hash: str | None = None):
        self.uds_dir = uds_dir
        self.custom_hw_hash = hw_hash

    def hw_hash(self) -> str:
        if self.custom_hw_hash is not None:
            return self.custom_hw_hash
        return node_info.NodeInfo(state_dir=self.uds_dir).hw_hash()

    def collect(self, *, busy: bool, worker: str) -> dict:
        return {
            "v": 2,
            "node_id": NODE_ID,
            "hw_hash": self.hw_hash(),
            "asr": {"model": "whisper-large-v3-turbo", "backend": "mlx", "rtf": 0.01},
            "translator": "apple",
            "busy": bool(busy),
            "on_ac": True,
            "in_use": False,
            "worker": worker,
        }


def generate_speech_wav(out_wav: Path) -> None:
    aiff_path = out_wav.with_suffix(".aiff")
    text = (
        "Guten Tag. Dies ist ein automatischer Rauchtest für die lokale UDS-Schnittstelle von realtime subtitle. "
        "Wir testen die Verbindung und die Spracherkennung."
    )
    subprocess.run(["say", "-v", "Anna", "-o", str(aiff_path), text], check=True)
    subprocess.run(
        ["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff_path), str(out_wav)],
        check=True,
    )
    if aiff_path.exists():
        aiff_path.unlink()


async def sample_tcp(pid: int, stop_event: asyncio.Event, samples: list, check_count: list):
    while not stop_event.is_set():
        try:
            res = subprocess.run(
                ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            check_count[0] += 1
            if res.returncode == 0 and res.stdout.strip():
                samples.append(res.stdout.strip())
        except Exception:
            pass
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.5)
        except asyncio.TimeoutError:
            pass


async def run_rslite(
    rslite_bin: Path,
    wav_path: Path,
    config_dir: Path,
    out_file: Path,
    err_file: Path,
    timeout: float = 30.0,
) -> tuple[int, list[str], int]:
    stop_event = asyncio.Event()
    tcp_samples: list[str] = []
    tcp_check_count = [0]
    with open(out_file, "w") as f_out, open(err_file, "w") as f_err:
        proc = await asyncio.create_subprocess_exec(
            str(rslite_bin),
            "--headless",
            "--source", f"file:{wav_path}",
            "--mode", "auto",
            stdout=f_out,
            stderr=f_err,
            env={**os.environ, "RSLITE_CONFIG_DIR": str(config_dir)},
        )
        sample_task = asyncio.create_task(sample_tcp(proc.pid, stop_event, tcp_samples, tcp_check_count))
        try:
            rc = await asyncio.wait_for(proc.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.send_signal(signal.SIGTERM)
            await proc.wait()
            rc = 124
        finally:
            stop_event.set()
            await sample_task
    return rc, tcp_samples, tcp_check_count[0]


async def run_smoke():
    # 临时文件目录：只放 /tmp/tk005b-smoke/
    smoke_tmp = Path("/tmp/tk005b-smoke")
    smoke_tmp.mkdir(parents=True, exist_ok=True)

    # 确认 rslite 二进制
    rslite_bin = REPO / "macos-native" / ".build" / "release" / "rslite"
    if not rslite_bin.exists():
        print(f"ABORT: rslite 二进制不存在: {rslite_bin}")
        sys.exit(1)

    # Socket 路径：按 NodeRouter.swift / NodeClient.swift 硬编码路径
    uds_sock = Path(os.path.expanduser("~/Library/Application Support/rs-node/gw.sock"))
    uds_dir = uds_sock.parent

    # 启动前安全检查：若该路径已存在任何东西就中止退出，不得覆盖
    if uds_sock.exists() or uds_sock.is_symlink():
        print(f"ABORT: socket 路径已存在任何东西: {uds_sock}，不得覆盖，中止退出。")
        sys.exit(1)

    created_uds_dir = False
    if not uds_dir.exists():
        uds_dir.mkdir(parents=True, mode=0o700)
        created_uds_dir = True

    created_uds_sock = False

    # 配置日志记录到临时目录
    gw_log_file = smoke_tmp / "gateway.log"
    file_handler = logging.FileHandler(str(gw_log_file), mode="w", encoding="utf-8")
    file_handler.setFormatter(logging.Formatter("%(asctime)s [node.gateway] %(levelname)s %(message)s"))
    gw_logger = logging.getLogger("realtime_subtitle.node.gateway")
    gw_logger.setLevel(logging.INFO)
    gw_logger.addHandler(file_handler)

    # 生成 fake worker
    worker_script = smoke_tmp / "fake_worker.py"
    worker_script.write_text(FAKE_WORKER, encoding="utf-8")

    # 生成 16k mono wav 音频
    wav_path = smoke_tmp / "test_anna_16k.wav"
    generate_speech_wav(wav_path)

    # rslite 配置目录：nodes.json 为空数组 []
    config_dir = smoke_tmp / "rslite-config"
    config_dir.mkdir(parents=True, exist_ok=True)
    nodes_file = config_dir / "nodes.json"
    nodes_file.write_text("[]\n", encoding="utf-8")

    gw = None
    try:
        print("=== 启动真 Gateway (只开 UDS，不开 TCP) ===")
        fake_info = FakeInfo(uds_dir=uds_dir)
        gw = Gateway(
            token=TOKEN,
            uds_dir=uds_dir,
            port=0,
            worker_argv=[sys.executable, str(worker_script)],
            info=fake_info,
            allow_non_tailscale_for_tests=True,
        )
        await start_gateway_uds_only(gw)
        created_uds_sock = True
        print(f"✓ Gateway UDS 已监听: {uds_sock}")

        print("\n=== 正例测试：rslite auto 模式回放（nodes.json=[]） ===")
        out1_f = smoke_tmp / "out1.log"
        err1_f = smoke_tmp / "err1.log"
        rc1, tcp_samples1, tcp_checks1 = await run_rslite(
            rslite_bin=rslite_bin,
            wav_path=wav_path,
            config_dir=config_dir,
            out_file=out1_f,
            err_file=err1_f,
            timeout=30.0,
        )

        stdout1 = out1_f.read_text(errors="replace")
        stderr1 = err1_f.read_text(errors="replace")
        
        # 等待 gateway 记录 session_end（若有会话发生）
        for _ in range(50):
            gw_log_text = gw_log_file.read_text(errors="replace")
            if "session_end" in gw_log_text:
                break
            await asyncio.sleep(0.1)
        gw_log_text = gw_log_file.read_text(errors="replace")

        # 解析 rslite stdout 事件
        evs1 = []
        for line in stdout1.splitlines():
            try:
                evs1.append(json.loads(line))
            except ValueError:
                pass
        node_finals = [
            e for e in evs1
            if e.get("ev") == "final" and str(e.get("text", "")).startswith("[local_uds] ")
        ]

        print(f"    rslite 退出码: {rc1}")
        print(f"    TCP 连接采样次数: {tcp_checks1}, 发现 TCP 连接数: {len(tcp_samples1)}")
        print(f"    输出 local_uds final 条数目: {len(node_finals)}")
        if stderr1.strip():
            print("    stderr 末尾 5 行:")
            for l in stderr1.strip().splitlines()[-5:]:
                print(f"      {l}")
        if gw_log_text.strip():
            print("    gateway.log 末尾 5 行:")
            for l in gw_log_text.strip().splitlines()[-5:]:
                print(f"      {l}")

        # 检查项 1: rslite 退出码 0
        check(rc1 == 0, "rslite 退出码 0")

        # 检查项 2: stderr 有选中本机节点（isLocal）的路由日志
        check(
            "select node=local_uds" in stderr1 or "select node=local" in stderr1,
            "stderr 有选中本机节点（isLocal）的路由日志",
        )

        # 检查项 3: 假 worker 收到了音频并产出 final 被 rslite 输出
        check(
            len(node_finals) >= 1,
            f"假 worker 收到了音频并产出 final 被 rslite 输出 (共 {len(node_finals)} 条)",
        )

        # 检查项 4: gateway 侧记录的会话 transport 为 uds
        check(
            "session_start transport=uds" in gw_log_text,
            "gateway 侧记录的会话 transport 为 uds",
        )

        # 检查项 5: 整个过程中 rslite 进程没有建立任何 TCP 连接
        check(
            len(tcp_samples1) == 0,
            f"整个过程中 rslite 进程没有建立任何 TCP 连接 (采样 {tcp_checks1} 次，TCP 连接数 0)",
        )

        # 检查项 6: rslite stderr/stdout 里不出现 token 字样
        token_found = ("token" in stdout1.lower()) or ("token" in stderr1.lower())
        check(not token_found, "rslite stderr/stdout 里不出现 token 字样")

        print("\n=== 负例测试：hw_hash 不一致时 rslite 不应选本机节点 ===")
        # 通过 monkeypatch gateway 的 info 返回不一致的 hw_hash
        gw._info = FakeInfo(uds_dir=uds_dir, hw_hash="mismatched-hw-hash-deadbeef")
        
        out2_f = smoke_tmp / "out2.log"
        err2_f = smoke_tmp / "err2.log"
        rc2, tcp_samples2, tcp_checks2 = await run_rslite(
            rslite_bin=rslite_bin,
            wav_path=wav_path,
            config_dir=config_dir,
            out_file=out2_f,
            err_file=err2_f,
            timeout=10.0,
        )

        stdout2 = out2_f.read_text(errors="replace")
        stderr2 = err2_f.read_text(errors="replace")

        evs2 = []
        for line in stdout2.splitlines():
            try:
                evs2.append(json.loads(line))
            except ValueError:
                pass
        node_finals2 = [
            e for e in evs2
            if e.get("ev") == "final" and str(e.get("text", "")).startswith("[local_uds] ")
        ]
        local_uds_selected = ("select node=local_uds" in stderr2) or (len(node_finals2) > 0)

        check(
            not local_uds_selected,
            "hw_hash 不一致时 rslite 不应选本机节点",
        )

    finally:
        print("\n=== 清理 Gateway 与环境 ===")
        if gw is not None:
            await gw.stop()
            print("✓ Gateway 已停止")

        gw_logger.removeHandler(file_handler)
        file_handler.close()

        # 结束 finally 里只删除自己创建的 socket 与自己创建的空目录
        if created_uds_sock and (uds_sock.exists() or uds_sock.is_symlink()):
            try:
                uds_sock.unlink()
                print(f"✓ 清理自己创建的 socket: {uds_sock}")
            except OSError as e:
                print(f"! 清理 socket 失败: {e}")

        if created_uds_dir and uds_dir.exists():
            try:
                uds_dir.rmdir()  # 仅当为空目录时才会成功删除
                print(f"✓ 清理自己创建的空目录: {uds_dir}")
            except OSError:
                pass


def main():
    asyncio.run(run_smoke())
    total = results["pass"] + results["fail"]
    print(f"\n==========================================")
    print(f"Smoke Summary: {results['pass']}/{total} PASS")
    if results["fail"] > 0:
        print(f"SMOKE FAIL ({results['fail']}/{total} failed)")
        sys.exit(1)
    else:
        print(f"SMOKE ALL PASS ({results['pass']}/{total})")
        sys.exit(0)


if __name__ == "__main__":
    main()
