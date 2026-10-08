"""TK-002：节点 gateway（生命周期 / 协议 v2 / 鉴权 / 上限 / 绑定 / info / S7）。

全部用「假 worker」脚本代替真 worker（TK-001）：脚本在 tmp_path 里生成，
把自己收到的东西逐行记到记录文件，测试靠轮询记录文件判断，不写固定 sleep。
"""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import socket
import stat
import subprocess
import sys
import textwrap
import time
from pathlib import Path

import pytest

websockets = pytest.importorskip("websockets")
from websockets.asyncio.client import connect, unix_connect  # noqa: E402
from websockets.exceptions import InvalidStatus  # noqa: E402

from realtime_subtitle.node import gateway as gw_mod  # noqa: E402
from realtime_subtitle.node import info as info_mod  # noqa: E402
from realtime_subtitle.node.gateway import Gateway, GatewayStartError  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKEN = "tok-S7-secret-value-9f3a"
FEATURE = "ZXQ-FEATURE-STRING-7731"  # 假 worker 吐出的「正文」，任何日志里都不许出现
HELLO = {"type": "hello", "v": 2, "src": "de", "dst": "zh", "sample_rate": 16000, "format": "s16le"}

_quiet = logging.getLogger("test.client")
_quiet.setLevel(logging.CRITICAL)
_quiet.propagate = False

FAKE_WORKER = textwrap.dedent(
    f'''
    import json, os, signal, sys, time
    mode, rec = sys.argv[1], sys.argv[2]
    def record(s):
        with open(rec, "a") as f:
            f.write(s + "\\n")
    record("start %d" % os.getpid())
    if mode == "ignore-term":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
    stdin, out = sys.stdin.buffer, sys.stdout.buffer
    def emit(d):
        out.write((json.dumps(d, ensure_ascii=False) + "\\n").encode()); out.flush()
    while True:
        b = stdin.read(1)
        if not b:
            record("eof"); sys.exit(0)
        if b == b"{{":
            t = json.loads(b + stdin.readline())["type"]
            record(t)
            if t == "hello":
                if mode == "stale":
                    emit({{"ev": "final", "id": 99, "a0": 0.0, "a1": 1.0, "text": "STALE"}})
                emit({{"ev": "ready", "cold": True, "load_s": 0.0, "engine": {{}}}})
                if mode == "stall":
                    time.sleep(3600)
            elif t == "flush":
                emit({{"ev": "final", "id": 1, "a0": 0.0, "a1": 1.0, "text": "{FEATURE}"}})
                emit({{"ev": "translation", "id": 1, "text": "{FEATURE}-zh"}})
            elif t == "drain":
                emit({{"ev": "drained"}})
        else:
            n = int.from_bytes(b + stdin.read(3), "big")
            stdin.read(n)
            record("pcm %d" % n)
            if mode == "crash":
                sys.exit(3)
    '''
)


# ------------------------------------------------------------------ 脚手架

def _rec_lines(rec: Path) -> list[str]:
    return rec.read_text().splitlines() if rec.exists() else []


def _starts(rec: Path) -> list[int]:
    return [int(x.split()[1]) for x in _rec_lines(rec) if x.startswith("start ")]


async def until(pred, timeout: float = 8.0, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timeout waiting for {what}")


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def make_dir(tmp_path: Path) -> Path:
    d = tmp_path / "rs-node"
    d.mkdir(mode=0o700)
    d.chmod(0o700)  # 不受 umask 影响
    return d


@contextlib.asynccontextmanager
async def running(tmp_path: Path, mode: str = "echo", **kw):
    script = tmp_path / "fake_worker.py"
    script.write_text(FAKE_WORKER)
    rec = tmp_path / "worker.rec"
    kw.setdefault("resolve_host", lambda: "127.0.0.1")
    gw = Gateway(
        token=TOKEN,
        uds_dir=make_dir(tmp_path) if not (tmp_path / "rs-node").exists() else tmp_path / "rs-node",
        port=0,
        worker_argv=[sys.executable, str(script), mode, str(rec)],
        **kw,
    )
    await gw.start()
    await asyncio.wait_for(gw.tcp_ready.wait(), 5)
    try:
        yield gw, rec
    finally:
        await gw.stop()


def tcp_connect(gw: Gateway, token: str | None = TOKEN):
    headers = {"Authorization": f"Bearer {token}"} if token is not None else {}
    return connect(
        f"ws://127.0.0.1:{gw.tcp_port}/v2/session", additional_headers=headers, logger=_quiet
    )


def uds_connect(gw: Gateway):
    return unix_connect(str(gw.uds_path), "ws://localhost/v2/session", logger=_quiet)


async def hello_ready(ws, hello: dict | None = None) -> dict:
    await ws.send(json.dumps(hello or HELLO))
    return json.loads(await asyncio.wait_for(ws.recv(), 8))


async def http_get(path: str, *, port: int | None = None, uds: Path | None = None,
                   token: str | None = None) -> tuple[int, dict]:
    if uds is not None:
        r, w = await asyncio.open_unix_connection(str(uds))
    else:
        r, w = await asyncio.open_connection("127.0.0.1", port)
    auth = f"Authorization: Bearer {token}\r\n" if token is not None else ""
    w.write(f"GET {path} HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{auth}\r\n".encode())
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    status = int(head.split(b" ", 2)[1])
    return status, (json.loads(body) if body.strip().startswith(b"{") else {})


@pytest.fixture(autouse=True)
def _log_level(caplog):
    caplog.set_level(logging.INFO)


# ------------------------------------------------------------------ 要求 1：轻量

def test_gateway_does_not_import_heavy_modules():
    code = (
        "import sys, realtime_subtitle.node.gateway;"
        "bad=[m for m in ('numpy','mlx','mlx_whisper','torch','faster_whisper','ctranslate2') if m in sys.modules];"
        "print(','.join(bad))"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(REPO_ROOT), env.get("PYTHONPATH", "")]))
    out = subprocess.run([sys.executable, "-c", code], cwd=REPO_ROOT, env=env,
                         capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == ""


# ------------------------------------------------------------------ 要求 6：绑定（S2）

@pytest.mark.parametrize("host", ["0.0.0.0", "::", "", "0:0:0:0:0:0:0:0", "  ", "localhost", "ff02::1"])
def test_validate_host_rejects_wildcards_and_non_ips(host):
    with pytest.raises(ValueError):
        gw_mod.validate_host(host)


def test_validate_host_accepts_tailscale_ip():
    assert gw_mod.validate_host("100.101.102.103") == "100.101.102.103"


def test_tailscale_ip_runner_is_injected():
    calls = []

    def run(argv):
        calls.append(argv)
        return "100.64.0.9\nextra\n"

    assert gw_mod.resolve_tailscale_ipv4(run) == "100.64.0.9"
    assert calls == [["tailscale", "ip", "-4"]]
    with pytest.raises(ValueError):
        gw_mod.resolve_tailscale_ipv4(lambda argv: "\n")


@pytest.mark.parametrize("bad", ["0.0.0.0", "::", ""])
def test_never_binds_wildcard_and_backs_off(tmp_path, monkeypatch, caplog, bad):
    bound: list = []
    real_serve = gw_mod.serve

    async def spy_serve(handler, host=None, port=None, **kw):
        bound.append(host)
        return await real_serve(handler, host, port, **kw)

    monkeypatch.setattr(gw_mod, "serve", spy_serve)
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) >= 8:
            await asyncio.Event().wait()  # 挂住，等 stop() 取消

    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=make_dir(tmp_path), port=0,
                     resolve_host=lambda: bad, sleep=fake_sleep)
        await gw.start()
        await until(lambda: len(delays) >= 8, what="8 retries")
        assert not gw.tcp_ready.is_set()
        await gw.stop()

    asyncio.run(scenario())
    assert bound == []  # 通配地址一次都没传给 serve
    assert delays == [1, 2, 4, 8, 16, 32, 60, 60]  # 指数退避，封顶 60s
    assert "tcp_bind_failed" in caplog.text


def test_bind_retries_until_port_free(tmp_path):
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) == 2:
            blocker.close()
        await asyncio.sleep(0)

    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=make_dir(tmp_path), port=port,
                     resolve_host=lambda: "127.0.0.1", sleep=fake_sleep)
        await gw.start()
        await asyncio.wait_for(gw.tcp_ready.wait(), 5)
        assert gw.tcp_port == port and gw.tcp_host == "127.0.0.1"
        await gw.stop()

    asyncio.run(scenario())
    assert delays == [1, 2]
    blocker.close()


# ------------------------------------------------------------------ 要求 3：鉴权

def test_tcp_auth_info_and_session(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            for tok in (None, "wrong", TOKEN + "x"):
                status, _ = await http_get("/v1/info", port=gw.tcp_port, token=tok)
                assert status == 401
            status, body = await http_get("/v1/info", port=gw.tcp_port, token=TOKEN)
            assert status == 200 and body["v"] == 2
            for tok in (None, "wrong"):
                with pytest.raises(InvalidStatus) as ei:
                    await tcp_connect(gw, tok)
                assert ei.value.response.status_code == 401
            assert _starts(rec) == []  # 鉴权失败不会拉起 worker
            ws = await tcp_connect(gw)
            assert (await hello_ready(ws))["ev"] == "ready"
            await ws.close()

    asyncio.run(scenario())


def test_uds_needs_no_token(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            status, body = await http_get("/v1/info", uds=gw.uds_path)
            assert status == 200 and body["worker"] == "cold"
            ws = await uds_connect(gw)
            assert (await hello_ready(ws))["ev"] == "ready"
            await ws.close()

    asyncio.run(scenario())


def test_unknown_path_is_404(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, _rec):
            status, _ = await http_get("/v1", port=gw.tcp_port, token=TOKEN)
            assert status == 404

    asyncio.run(scenario())


def test_token_comes_from_file_or_stdin_never_argv(tmp_path):
    f = tmp_path / "token"
    f.write_text("  abc123 \n")
    assert gw_mod.load_token(f) == "abc123"
    f.write_text("\n")
    with pytest.raises(ValueError):
        gw_mod.load_token(f)
    with pytest.raises(SystemExit):
        gw_mod.parse_args(["--token", "abc"])
    with pytest.raises(SystemExit):
        gw_mod.parse_args([])
    assert gw_mod.parse_args(["--token-stdin"]).token_stdin is True
    with pytest.raises(ValueError):
        Gateway(token="", uds_dir=tmp_path)


def test_second_session_gets_1013(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, _rec):
            first = await tcp_connect(gw)
            assert (await hello_ready(first))["ev"] == "ready"
            status, body = await http_get("/v1/info", port=gw.tcp_port, token=TOKEN)
            assert body["busy"] is True
            for second in (await tcp_connect(gw), await uds_connect(gw)):  # TCP 与 UDS 共用同一个名额
                await asyncio.wait_for(second.wait_closed(), 5)
                assert second.close_code == 1013
            await first.close()
            await until(lambda: not gw._session_active, what="session slot freed")
            third = await tcp_connect(gw)
            assert (await hello_ready(third))["ev"] == "ready"
            await third.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ 要求 4：UDS（S3）

def test_uds_dir_must_be_0700_and_owned(tmp_path):
    async def start(uds_dir, **kw):
        gw = Gateway(token=TOKEN, uds_dir=uds_dir, port=0, resolve_host=lambda: "127.0.0.1", **kw)
        await gw.start()
        await gw.stop()

    loose = tmp_path / "loose"
    loose.mkdir()
    loose.chmod(0o755)
    with pytest.raises(GatewayStartError, match="mode"):
        asyncio.run(start(loose))

    ok = tmp_path / "ok"
    ok.mkdir()
    ok.chmod(0o700)
    with pytest.raises(GatewayStartError, match="owner"):
        asyncio.run(start(ok, uid=os.getuid() + 1))

    real = tmp_path / "real"
    real.mkdir()
    real.chmod(0o700)
    (tmp_path / "linkdir").symlink_to(real)
    with pytest.raises(GatewayStartError):
        asyncio.run(start(tmp_path / "linkdir"))


def test_uds_dir_is_created_0700_when_missing(tmp_path):
    d = tmp_path / "fresh" / "rs-node"

    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=d, port=0, resolve_host=lambda: "127.0.0.1")
        await gw.start()
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(gw.uds_path).st_mode) == 0o600
        await gw.stop()

    asyncio.run(scenario())


def test_uds_socket_path_symlink_and_stale(tmp_path):
    async def start(d):
        gw = Gateway(token=TOKEN, uds_dir=d, port=0, resolve_host=lambda: "127.0.0.1")
        await gw.start()
        ok = (await http_get("/v1/info", uds=gw.uds_path))[0] == 200
        await gw.stop()
        return ok

    # symlink：拒绝，且不碰链接指向的目标
    d1 = make_dir(tmp_path)
    victim = tmp_path / "victim"
    victim.write_text("keep")
    (d1 / gw_mod.UDS_FILENAME).symlink_to(victim)
    with pytest.raises(GatewayStartError, match="symlink"):
        asyncio.run(start(d1))
    assert victim.read_text() == "keep"

    # 普通文件：不是 socket，不删
    d2 = tmp_path / "d2"
    d2.mkdir(mode=0o700)
    d2.chmod(0o700)
    (d2 / gw_mod.UDS_FILENAME).write_text("not a socket")
    with pytest.raises(GatewayStartError, match="socket"):
        asyncio.run(start(d2))
    assert (d2 / gw_mod.UDS_FILENAME).read_text() == "not a socket"

    # 残留 socket：清掉后正常启动
    d3 = tmp_path / "d3"
    d3.mkdir(mode=0o700)
    d3.chmod(0o700)
    s = socket.socket(socket.AF_UNIX)
    s.bind(str(d3 / gw_mod.UDS_FILENAME))
    s.close()
    assert stat.S_ISSOCK(os.lstat(d3 / gw_mod.UDS_FILENAME).st_mode)
    assert asyncio.run(start(d3)) is True


def test_uds_peer_uid_mismatch_is_rejected(tmp_path, caplog):
    async def scenario():
        async with running(tmp_path, peer_uid=lambda sock: os.getuid() + 1) as (gw, rec):
            status, _ = await http_get("/v1/info", uds=gw.uds_path)
            assert status == 403
            with pytest.raises(InvalidStatus) as ei:
                await uds_connect(gw)
            assert ei.value.response.status_code == 403
            assert _starts(rec) == []

    asyncio.run(scenario())
    assert "peer_uid_rejected" in caplog.text


def test_uds_peer_uid_failure_fails_closed(tmp_path):
    def boom(sock):
        raise OSError("no creds")

    async def scenario():
        async with running(tmp_path, peer_uid=boom) as (gw, _rec):
            assert (await http_get("/v1/info", uds=gw.uds_path))[0] == 403

    asyncio.run(scenario())


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="SO_PEERCRED 仅 Linux；macOS 分支待 Mac 验证")
def test_default_peer_uid_reads_real_peercred():
    a, b = socket.socketpair(socket.AF_UNIX)
    try:
        assert gw_mod.default_peer_uid(a) == os.getuid()
        assert gw_mod.default_peer_uid(b) == os.getuid()
    finally:
        a.close()
        b.close()


# ------------------------------------------------------------------ 要求 5：上限（S4/P2）

@pytest.mark.parametrize("patch,code", [
    ({"sample_rate": 44100}, 1003),
    ({"format": "f32le"}, 1003),
    ({"v": 1}, 1008),
    ({"src": ""}, 1008),
    ({"dst": "zh cn; rm"}, 1008),
    ({"src": None}, 1008),
])
def test_bad_hello_is_rejected_without_spawning_worker(tmp_path, patch, code):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            ws = await uds_connect(gw)
            await ws.send(json.dumps({**HELLO, **patch}))
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == code
            assert _starts(rec) == [] and gw.worker.pid is None

    asyncio.run(scenario())


def test_hello_must_be_first_text_and_arrive_in_time(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "HELLO_TIMEOUT_S", 0.4)

    async def scenario():
        async with running(tmp_path) as (gw, rec):
            ws = await uds_connect(gw)  # 什么都不发
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1008
            ws = await uds_connect(gw)  # 先发二进制
            await ws.send(b"\x00\x00")
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1008
            ws = await uds_connect(gw)  # 先发 flush
            await ws.send(json.dumps({"type": "flush"}))
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1008
            assert _starts(rec) == []

    asyncio.run(scenario())


def test_frame_size_limit_and_alignment(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.send(b"\x01\x00" * (32 * 1024))  # 恰好 64KB：放行
            await ws.send(json.dumps({"type": "flush"}))
            await until(lambda: "flush" in _rec_lines(rec), what="64KB frame accepted")
            assert "pcm 65536" in _rec_lines(rec)
            await ws.send(b"\x01\x00" * (32 * 1024 + 1))  # 超 64KB：协议层直接 1009
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1009

            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.send(b"\x01\x00\x01")  # 奇数字节不是 s16le
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1003

    asyncio.run(scenario())


def test_audio_queue_drops_oldest_and_never_drops_control():
    async def scenario():
        q = gw_mod.AudioQueue(3)
        q.put_pcm(b"a0")
        q.put_ctl({"type": "flush"})
        q.put_pcm(b"b0")
        q.put_pcm(b"c0")
        assert q.put_pcm(b"d0") == (1, 2)  # 丢最旧的 a0
        assert q.put_pcm(b"e0") == (1, 2)  # 丢 b0；中间那条 flush 还在
        got = [await q.get() for _ in range(4)]
        assert got == [("ctl", {"type": "flush"}), ("pcm", b"c0"), ("pcm", b"d0"), ("pcm", b"e0")]

    asyncio.run(scenario())


def test_stalled_worker_overflows_queue_with_warning(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gw_mod, "AUDIO_QUEUE_MAX_FRAMES", 5)

    async def scenario():
        async with running(tmp_path, "stall") as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            frame = b"\x01\x00" * 2000
            for _ in range(300):  # worker 不读 stdin：管道塞满后队列只能丢旧的
                await ws.send(frame)
            await until(lambda: "audio_queue_drop" in caplog.text, what="drop warning")
            await ws.close()

    asyncio.run(scenario())
    line = next(r.getMessage() for r in caplog.records if "audio_queue_drop" in r.getMessage())
    assert "frames=" in line and "bytes=" in line


def test_session_max_duration(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "SESSION_MAX_S", 0.5)

    async def scenario():
        async with running(tmp_path) as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1000
            await until(lambda: not gw._session_active)

    asyncio.run(scenario())


def test_ping_timeout_ends_session(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "PING_INTERVAL_S", 0.2)
    monkeypatch.setattr(gw_mod, "PING_TIMEOUT_S", 0.3)
    monkeypatch.setattr(gw_mod, "CLOSE_TIMEOUT_S", 0.3)

    async def scenario():
        async with running(tmp_path) as (gw, _rec):
            ws = await tcp_connect(gw)
            await hello_ready(ws)
            ws.transport.pause_reading()  # 客户端「死机」：不再读，也就不会回 pong
            t0 = time.monotonic()
            await until(lambda: not gw._session_active, timeout=15, what="ping timeout")
            # 10s 的默认 close_timeout 若没被压低，这里会超过 10s
            assert time.monotonic() - t0 < 5
            ws.transport.abort()

    asyncio.run(scenario())


# ------------------------------------------------------------------ 要求 2：生命周期

def test_worker_lifecycle_spawn_keepalive_reuse_reap(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "WORKER_KEEPALIVE_S", 1.5)
    monkeypatch.setattr(gw_mod, "WORKER_KILL_GRACE_S", 2.0)

    async def scenario():
        async with running(tmp_path) as (gw, rec):
            assert gw.worker.pid is None and _starts(rec) == []  # 没会话就没 worker
            ws = await uds_connect(gw)
            assert (await hello_ready(ws))["ev"] == "ready"
            pid1 = gw.worker.pid
            assert pid1 and _alive(pid1)
            await ws.close()
            await until(lambda: not gw._session_active)
            assert gw.worker.state == "warm"  # 保温
            ws = await uds_connect(gw)  # 保温期内新会话：复用同一个 worker
            assert (await hello_ready(ws))["ev"] == "ready"
            assert gw.worker.pid == pid1 and _starts(rec) == [pid1]
            await ws.close()
            await until(lambda: not gw._session_active)
            await until(lambda: gw.worker.pid is None, what="idle reap")
            await until(lambda: not _alive(pid1), what="worker process gone")
            status, body = await http_get("/v1/info", uds=gw.uds_path)
            assert body["worker"] == "cold"
            ws = await uds_connect(gw)  # 回收之后再来：重新 spawn
            await hello_ready(ws)
            assert len(_starts(rec)) == 2
            await ws.close()

    asyncio.run(scenario())


def test_reap_escalates_to_sigkill(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gw_mod, "WORKER_KEEPALIVE_S", 0.2)
    monkeypatch.setattr(gw_mod, "WORKER_KILL_GRACE_S", 0.4)

    async def scenario():
        async with running(tmp_path, "ignore-term") as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            pid = gw.worker.pid
            await ws.close()
            await until(lambda: "worker_reaped" in caplog.text, what="reap")
            assert not _alive(pid)

    asyncio.run(scenario())
    assert "how=kill" in caplog.text


def test_new_session_during_reap_waits_and_respawns(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "WORKER_KEEPALIVE_S", 0.1)
    monkeypatch.setattr(gw_mod, "WORKER_KILL_GRACE_S", 0.6)

    async def scenario():
        async with running(tmp_path, "ignore-term") as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            pid1 = gw.worker.pid
            await ws.close()
            await asyncio.sleep(0.3)  # 回收已开始（正卡在 SIGTERM 被无视的宽限期里）
            ws = await uds_connect(gw)
            assert (await hello_ready(ws))["ev"] == "ready"
            assert gw.worker.pid not in (None, pid1)  # 绝不复用一个正在被杀的进程
            await ws.close()

    asyncio.run(scenario())


def test_worker_crash_notifies_client(tmp_path, caplog):
    async def scenario():
        async with running(tmp_path, "crash") as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.send(b"\x01\x00" * 100)  # 假 worker 收到第一帧就 exit(3)
            ev = json.loads(await asyncio.wait_for(ws.recv(), 8))
            assert ev["ev"] == "status" and ev["code"] == "worker_crashed"
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1011
            await until(lambda: not gw._session_active)
            assert gw.worker.state == "cold"
            ws = await uds_connect(gw)  # 崩溃之后下一个会话能拿到新 worker
            assert (await hello_ready(ws))["ev"] == "ready"
            assert len(_starts(rec)) == 2
            await ws.close()

    asyncio.run(scenario())
    assert "worker_exit code=3" in caplog.text


def test_worker_spawn_failure_is_reported(tmp_path):
    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=make_dir(tmp_path), port=0,
                     worker_argv=["/nonexistent/worker-binary"], resolve_host=lambda: "127.0.0.1")
        await gw.start()
        try:
            ws = await uds_connect(gw)
            await ws.send(json.dumps(HELLO))
            ev = json.loads(await asyncio.wait_for(ws.recv(), 5))
            assert ev["code"] == "worker_crashed"
            await asyncio.wait_for(ws.wait_closed(), 5)
        finally:
            await gw.stop()

    asyncio.run(scenario())


def test_events_and_controls_flow_in_order(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            for _ in range(3):
                await ws.send(b"\x01\x00" * 160)
            await ws.send(json.dumps({"type": "flush"}))
            await ws.send(json.dumps({"type": "drain"}))
            evs = [json.loads(await asyncio.wait_for(ws.recv(), 8)) for _ in range(3)]
            assert [e["ev"] for e in evs] == ["final", "translation", "drained"]
            assert evs[0]["text"] == FEATURE
            assert _rec_lines(rec)[1:] == ["hello", "pcm 320", "pcm 320", "pcm 320", "flush", "drain"]
            await ws.send(json.dumps({"type": "hello", **{k: v for k, v in HELLO.items() if k != "type"}}))
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1008  # 会话中重复 hello

    asyncio.run(scenario())


def test_stale_events_from_previous_session_are_not_forwarded(tmp_path):
    async def scenario():
        async with running(tmp_path, "stale") as (gw, _rec):
            ws = await uds_connect(gw)
            first = await hello_ready(ws)
            assert first["ev"] == "ready"  # 假 worker 在 ready 之前先吐了一条旧 final
            await ws.close()

    asyncio.run(scenario())


# ------------------------------------------------------------------ 要求 7：info

class FakeSystem:
    UUID = "11111111-2222-3333-4444-555555555555"

    def __init__(self, idle_ns=5_000_000_000, power="AC Power", fail=()):
        self.idle_ns, self.power, self.fail = idle_ns, power, set(fail)
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(argv)
        if argv[0] in self.fail:
            raise FileNotFoundError(argv[0])
        if argv[0] == "ioreg" and "IOHIDSystem" in argv:
            return f'    | |   "HIDIdleTime" = {self.idle_ns}\n'
        if argv[0] == "ioreg":
            return f'    "IOPlatformUUID" = "{self.UUID}"\n'
        if argv[0] == "pmset":
            return f"Now drawing from '{self.power}'\n -InternalBattery-0\t100%; charged\n"
        raise AssertionError(argv)


def test_info_matches_p1_and_leaks_nothing_raw(tmp_path):
    state = make_dir(tmp_path)
    (state / "node_id").write_text("node-abc\n")
    (state / "asr_state.json").write_text(json.dumps(
        {"model": "whisper-large-v3-turbo", "backend": "mlx", "rtf": 0.08, "translator": "ollama:qwen3.5:4b"}))
    system = FakeSystem(idle_ns=7_654_321_000)
    info = info_mod.NodeInfo(run=system, state_dir=state)

    async def scenario():
        async with running(tmp_path, info=info) as (gw, _rec):
            status, body = await http_get("/v1/info", port=gw.tcp_port, token=TOKEN)
            raw = await _raw_info(gw)
            ws = await uds_connect(gw)
            await hello_ready(ws)
            _, busy_body = await http_get("/v1/info", uds=gw.uds_path)
            await ws.close()
            return status, body, raw, busy_body

    status, body, raw, busy_body = asyncio.run(scenario())
    assert status == 200
    assert set(body) == {"v", "node_id", "hw_hash", "asr", "translator", "busy", "on_ac", "in_use", "worker"}
    assert body["v"] == 2 and body["node_id"] == "node-abc"
    assert body["hw_hash"] == hashlib.sha256(FakeSystem.UUID.encode()).hexdigest()[:16]
    assert body["asr"] == {"model": "whisper-large-v3-turbo", "backend": "mlx", "rtf": 0.08}
    assert body["translator"] == "ollama:qwen3.5:4b"
    assert body["busy"] is False and body["on_ac"] is True and body["in_use"] is True
    assert body["worker"] == "cold"
    assert busy_body["busy"] is True and busy_body["worker"] == "warm"
    # S8：原始空闲时长 / 原始 UUID 不出现在响应里
    assert "7654321" not in raw and "7.65" not in raw and FakeSystem.UUID not in raw


async def _raw_info(gw) -> str:
    r, w = await asyncio.open_connection("127.0.0.1", gw.tcp_port)
    w.write(f"GET /v1/info HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer {TOKEN}\r\n\r\n".encode())
    await w.drain()
    raw = (await r.read()).decode()
    w.close()
    return raw


def test_info_in_use_threshold_and_fail_conservative(tmp_path):
    ni = lambda **kw: info_mod.NodeInfo(run=FakeSystem(**kw), state_dir=tmp_path)  # noqa: E731
    assert ni(idle_ns=59_000_000_000).in_use() is True
    assert ni(idle_ns=61_000_000_000).in_use() is False
    assert ni(power="Battery Power").on_ac() is False
    assert ni(fail=["ioreg"]).in_use() is True  # 探测不出来 = 当作有人在用
    assert ni(fail=["pmset"]).on_ac() is False
    assert ni(fail=["ioreg"]).hw_hash() == ""


def test_info_defaults_when_state_files_missing_or_bad(tmp_path):
    info = info_mod.NodeInfo(run=FakeSystem(), state_dir=tmp_path)
    assert info.node_id() == ""
    asr, tr = info.asr_and_translator()
    assert asr["rtf"] is None and tr == "apple"
    (tmp_path / "asr_state.json").write_text("{not json")
    assert info.asr_and_translator()[0]["rtf"] is None
    (tmp_path / "asr_state.json").write_text(json.dumps({"rtf": "fast", "translator": "evil; rm -rf"}))
    asr, tr = info.asr_and_translator()
    assert asr["rtf"] is None and tr == "apple"


def test_info_hw_hash_is_cached(tmp_path):
    system = FakeSystem()
    info = info_mod.NodeInfo(run=system, state_dir=tmp_path)
    info.hw_hash()
    info.hw_hash()
    assert sum(1 for c in system.calls if "IOPlatformExpertDevice" in c) == 1


def test_parsers_on_realistic_output():
    assert info_mod.parse_hid_idle_seconds('"HIDIdleTime" = 2000000000\n"HIDIdleTime" = 9000000000') == 2.0
    assert info_mod.parse_on_ac("Now drawing from 'AC Power'\n") is True
    assert info_mod.parse_on_ac("Now drawing from 'Battery Power'\n") is False
    with pytest.raises(ValueError):
        info_mod.parse_on_ac("")
    with pytest.raises(ValueError):
        info_mod.parse_platform_uuid("nothing here")


# ------------------------------------------------------------------ 要求 8：S7

def test_logs_never_contain_transcripts_or_tokens(tmp_path, caplog, capfd):
    caplog.set_level(logging.DEBUG)  # 连 DEBUG 也不许漏（websockets 的 DEBUG 会逐帧打印文本）
    wrong = "WRONG-TOKEN-QQ77"

    async def scenario():
        async with running(tmp_path) as (gw, _rec):
            with pytest.raises(InvalidStatus):
                await tcp_connect(gw, wrong)
            await http_get("/v1/info", port=gw.tcp_port, token=wrong)
            ws = await tcp_connect(gw)
            await hello_ready(ws)
            await ws.send(b"\x01\x00" * 160)
            await ws.send(json.dumps({"type": "flush"}))
            texts = [json.loads(await asyncio.wait_for(ws.recv(), 8))["text"] for _ in range(2)]
            assert texts[0] == FEATURE  # 正文确实流经了 gateway，只是没进日志
            await ws.close()
            await until(lambda: not gw._session_active)

    asyncio.run(scenario())
    err = capfd.readouterr().err
    for needle in (FEATURE, TOKEN, wrong):
        assert needle not in caplog.text, needle
        assert needle not in err, needle
    assert "session_end" in caplog.text  # 日志本身是有内容的：记的是事件和字节数
