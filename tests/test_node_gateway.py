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
    total = 0  # 收到的 PCM 字节数：worker 按它算样本时钟（F2）
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
                if mode == "gate":  # 慢 worker：门文件出现之前一个字节都不读
                    while not os.path.exists(rec + ".gate"):
                        time.sleep(0.02)
            elif t == "flush":
                emit({{"ev": "final", "id": 1, "a0": 0.0, "a1": 1.0,
                       "text": str(total) if mode == "gate" else "{FEATURE}"}})
                emit({{"ev": "translation", "id": 1, "text": "{FEATURE}-zh"}})
            elif t == "drain":
                emit({{"ev": "drained"}})
        else:
            n = int.from_bytes(b + stdin.read(3), "big")
            stdin.read(n)
            total += n
            record("pcm %d" % n)
            if mode.startswith("crash"):
                sys.exit(int(mode.partition(":")[2] or 3))
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
    kw.setdefault("allow_non_tailscale_for_tests", True)  # 测试在 127.0.0.1 上监听
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
    assert gw_mod.validate_host("100.64.0.1") == "100.64.0.1"
    assert gw_mod.validate_host("100.127.255.254") == "100.127.255.254"


@pytest.mark.parametrize("host", [
    "8.8.8.8", "192.168.1.20", "10.0.0.5", "127.0.0.1", "100.63.255.255", "100.128.0.1",
    "fd7a:115c:a1e0::1", " 100.64.0.1", "100.64.0.1 ", "100.64.0.1\n", "\t100.64.0.1",
])
def test_validate_host_rejects_non_tailscale_and_whitespace(host):
    with pytest.raises(ValueError):
        gw_mod.validate_host(host)


def test_non_tailscale_escape_hatch_is_explicit_and_still_rejects_wildcards():
    assert gw_mod.validate_host("127.0.0.1", allow_non_tailscale_for_tests=True) == "127.0.0.1"
    for bad in ("0.0.0.0", "::", " 127.0.0.1"):
        with pytest.raises(ValueError):
            gw_mod.validate_host(bad, allow_non_tailscale_for_tests=True)


def test_gateway_default_refuses_loopback_bind(tmp_path):
    """不带测试开关的 Gateway 不会在 127.0.0.1 上监听（取到非 Tailscale 地址只会退避重试）。"""
    delays: list[float] = []

    async def fake_sleep(d):
        delays.append(d)
        if len(delays) >= 2:
            await asyncio.Event().wait()

    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=make_dir(tmp_path), port=0,
                     resolve_host=lambda: "127.0.0.1", sleep=fake_sleep)
        await gw.start()
        await until(lambda: len(delays) >= 2, what="retries")
        assert not gw.tcp_ready.is_set() and gw.tcp_port is None
        await gw.stop()

    asyncio.run(scenario())


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
                     resolve_host=lambda: bad, sleep=fake_sleep,
                     allow_non_tailscale_for_tests=True)  # 开了测试开关也必须拒绝通配
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
                     resolve_host=lambda: "127.0.0.1", sleep=fake_sleep,
                     allow_non_tailscale_for_tests=True)
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


LONG_TOKEN = "k" * 40


def _token_file(tmp_path, text, mode=0o600):
    f = tmp_path / "token"
    f.write_text(text)
    f.chmod(mode)
    return f


def test_token_comes_from_file_or_stdin_never_argv(tmp_path):
    f = _token_file(tmp_path, f"  {LONG_TOKEN} \n")
    assert gw_mod.load_token(f) == LONG_TOKEN
    f = _token_file(tmp_path, "\n")
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
        gw = Gateway(token=TOKEN, uds_dir=uds_dir, port=0, resolve_host=lambda: "127.0.0.1",
                     allow_non_tailscale_for_tests=True, **kw)
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
        gw = Gateway(token=TOKEN, uds_dir=d, port=0, resolve_host=lambda: "127.0.0.1",
                     allow_non_tailscale_for_tests=True)
        await gw.start()
        assert stat.S_IMODE(os.lstat(d).st_mode) == 0o700
        assert stat.S_IMODE(os.lstat(gw.uds_path).st_mode) == 0o600
        await gw.stop()

    asyncio.run(scenario())


def test_uds_socket_path_symlink_and_stale(tmp_path):
    async def start(d):
        gw = Gateway(token=TOKEN, uds_dir=d, port=0, resolve_host=lambda: "127.0.0.1",
                     allow_non_tailscale_for_tests=True)
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
    ({"src": "DE"}, 1008),            # F1：与 worker 同一正则，大写不行
    ({"src": "de\n"}, 1008),          # F1：`$` 会放过结尾换行，必须 fullmatch
    ({"dst": "zh\n"}, 1008),
    ({"src": "de_DE"}, 1008),
    ({"src": "d"}, 1008),
    ({"src": "deutsch"}, 1008),
    ({"dst": "zh-"}, 1008),
    ({"dst": "zh-CNNNN"}, 1008),
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


def test_lang_regex_matches_worker_and_accepts_real_codes():
    for ok in ("de", "zh", "eng", "zh-CN", "pt-BR", "zh-Hans"):
        assert gw_mod._LANG_RE.fullmatch(ok), ok
    assert gw_mod._LANG_RE.pattern == r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$"  # TK-001 worker._LANG 原文


def test_bad_language_does_not_disturb_a_warm_worker(tmp_path):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.close()
            await until(lambda: not gw._session_active)
            pid = gw.worker.pid
            assert pid and gw.worker.state == "warm"
            for bad in ("DE", "de\n"):
                ws = await uds_connect(gw)
                await ws.send(json.dumps({**HELLO, "src": bad}))
                await asyncio.wait_for(ws.wait_closed(), 5)
                assert ws.close_code == 1008
                await until(lambda: not gw._session_active)
            # 保温中的 worker 没被碰：同一个进程、没有新 spawn、没收到第二个 hello
            assert gw.worker.pid == pid and _alive(pid)
            assert _starts(rec) == [pid] and _rec_lines(rec).count("hello") == 1
            ws = await uds_connect(gw)
            assert (await hello_ready(ws, {**HELLO, "src": "zh-CN", "dst": "de"}))["ev"] == "ready"
            assert gw.worker.pid == pid
            await ws.close()

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


def test_audio_queue_replaces_dropped_frames_with_gap_in_place():
    async def scenario():
        q = gw_mod.AudioQueue(3)
        q.put_pcm(b"a0")
        q.put_ctl({"type": "flush"})
        q.put_pcm(b"b0")
        q.put_pcm(b"c0")
        assert q.put_pcm(b"d0") == (1, 2)  # 丢最旧的 a0，原位换成 2 字节的 gap
        assert q.put_pcm(b"e0") == (1, 2)  # 丢 b0；gap 与前一个 gap 隔着 flush，不合并
        got = [await q.get() for _ in range(6)]
        assert got == [("gap", 2), ("ctl", {"type": "flush"}), ("gap", 2),
                       ("pcm", b"c0"), ("pcm", b"d0"), ("pcm", b"e0")]

    asyncio.run(scenario())


def test_audio_queue_gap_markers_merge_and_total_bytes_are_conserved():
    async def scenario():
        q = gw_mod.AudioQueue(2)
        sent = 0
        for i in range(500):
            frame = bytes(2 * (i % 7 + 1))
            sent += len(frame)
            q.put_pcm(frame)
        assert len(q) == 3  # 一个合并后的 gap + 2 帧：丢帧不会让队列变长
        got = []
        while len(q):
            got.append(await q.get())
        assert sum(len(p) if k == "pcm" else p for k, p in got) == sent  # 一个字节都不少
        assert [k for k, _ in got] == ["gap", "pcm", "pcm"]

    asyncio.run(scenario())


def test_audio_queue_control_messages_are_bounded_and_flushes_merge(caplog):
    async def scenario():
        q = gw_mod.AudioQueue(10)
        for _ in range(100_000):  # 海量连续 flush：合并成一条，队列不增长
            assert q.put_ctl({"type": "flush"}) is True
        assert len(q) == 1
        # R2-D3：夹着音频的 flush 无法合并，到上限后丢最旧的 flush（不再拒收），队列长度有界
        for _ in range(10_000):
            q.put_pcm(b"\x00\x00")
            assert q.put_ctl({"type": "flush"}) is True
        assert q._ctl <= gw_mod.CTL_QUEUE_MAX
        assert len(q) <= gw_mod.CTL_QUEUE_MAX + 10 + gw_mod.CTL_QUEUE_MAX + 1  # ctl + 音频上限 + gap
        assert q.flush_dropped > 9000
        # 取走后名额回收
        while len(q):
            await q.get()
        assert q.put_ctl({"type": "drain"}) is True

    asyncio.run(scenario())
    assert "control_queue_flush_dropped" in caplog.text
    # 告警按 1s 合并：上万次丢弃不会刷出上万行
    assert caplog.text.count("control_queue_flush_dropped") < 20


def test_audio_queue_flushes_separated_only_by_gap_merge():
    async def scenario():
        q = gw_mod.AudioQueue(1)
        q.put_pcm(b"aa")
        q.put_ctl({"type": "flush"})
        q.put_pcm(b"bb")        # 把 aa 挤成 gap
        q.put_pcm(b"cc")        # 把 bb 挤成 gap（与前一个 gap 隔着 flush，不合并）
        assert q.put_ctl({"type": "flush"}) is True
        # flush, gap, pcm(cc), flush：cc 是真实 PCM，不合并
        assert [k for k, _ in q._items] == ["gap", "ctl", "gap", "pcm", "ctl"]
        # 在 gap 后面的新 flush 之前没有真实 PCM 的情况：flush, gap, flush
        q3 = gw_mod.AudioQueue(1)
        q3.put_ctl({"type": "flush"})
        q3.put_pcm(b"aa")
        q3.put_pcm(b"bb")       # aa -> gap
        # 队列：flush, gap, pcm(bb) —— 取走 bb 之外的路径：直接构造 flush, gap
        q3._items.pop()
        q3._pcm -= 1
        assert q3.put_ctl({"type": "flush"}) is True
        assert [k for k, _ in q3._items] == ["gap", "ctl"]  # 旧 flush 被换到 gap 之后
        assert q3._ctl == 1

    asyncio.run(scenario())


def test_audio_queue_never_drops_drain_and_rejects_when_only_drains_overflow(monkeypatch):
    monkeypatch.setattr(gw_mod, "CTL_QUEUE_MAX", 6)

    async def scenario():
        q = gw_mod.AudioQueue(10)
        assert q.put_ctl({"type": "hello"}) is True
        for _ in range(5):
            q.put_pcm(b"\x00\x00")
            assert q.put_ctl({"type": "drain"}) is True
        assert q._ctl == 6
        assert q.put_ctl({"type": "drain"}) is False      # 全是 drain/hello：没有 flush 可丢
        assert q.put_ctl({"type": "flush"}) is False
        # 队列里夹着 flush 时：丢 flush 让位，但 drain 一条不少
        q = gw_mod.AudioQueue(10)
        for _ in range(3):
            q.put_pcm(b"\x00\x00")
            q.put_ctl({"type": "drain"})
        for _ in range(100):
            q.put_pcm(b"\x00\x00")
            assert q.put_ctl({"type": "flush"}) is True
        drains = [1 for k, p in q._items if k == "ctl" and p["type"] == "drain"]
        assert len(drains) == 3
        assert q.put_ctl({"type": "drain"}) is True       # 还能顶掉 flush 入队
        assert q.put_ctl({"type": "drain"}) is True
        assert q.put_ctl({"type": "drain"}) is True       # 此时 6 条全是 drain
        assert q.put_ctl({"type": "drain"}) is False

    asyncio.run(scenario())


def test_flush_flood_with_stalled_worker_keeps_connection_and_bounded_queue(tmp_path, monkeypatch, caplog):
    """S-F1 在 R2-D3 之后的新语义：flush/pcm 交错洪水不再断开合规客户端，
    而是丢最旧的 flush + 告警，队列长度有界。"""
    made: list = []

    class Spy(gw_mod.AudioQueue):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            made.append(self)

    monkeypatch.setattr(gw_mod, "AudioQueue", Spy)
    monkeypatch.setattr(gw_mod, "CTL_QUEUE_MAX", 8)

    async def scenario():
        async with running(tmp_path, "stall") as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            frame = b"\x01\x00" * 1600
            flush = json.dumps({"type": "flush"})
            for _ in range(2000):  # worker 不读 stdin：管道塞满后 flush 只能留在队列里
                await ws.send(frame)
                await ws.send(flush)
            q = made[-1]
            await until(lambda: q.flush_dropped > 0, what="flush dropped")
            assert ws.close_code is None  # 没被断开
            assert q._ctl <= 8 and len(q) <= 8 + gw_mod.AUDIO_QUEUE_MAX_FRAMES + 8 + 1
            await ws.close()
            await until(lambda: not gw._session_active)

    asyncio.run(scenario())
    assert "control_queue_flush_dropped" in caplog.text
    assert "control_queue_overflow" not in caplog.text


def test_drain_flood_closes_connection_with_bounded_queue(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gw_mod, "CTL_QUEUE_MAX", 8)

    async def scenario():
        async with running(tmp_path, "stall") as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            frame = b"\x01\x00" * 1600
            drain = json.dumps({"type": "drain"})
            try:
                for _ in range(2000):
                    await ws.send(frame)
                    await ws.send(drain)
            except Exception:  # noqa: BLE001 - 服务端先关了连接
                pass
            await asyncio.wait_for(ws.wait_closed(), 8)
            assert ws.close_code == 1008
            await until(lambda: not gw._session_active)

    asyncio.run(scenario())
    assert "control_queue_overflow" in caplog.text


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
        async with running(tmp_path, "crash:9") as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.send(b"\x01\x00" * 100)  # 假 worker 收到第一帧就 exit(9)
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
    assert "worker_exit code=9" in caplog.text


@pytest.mark.parametrize("rc,code", [
    (2, "protocol_error"), (3, "engine_load_failed"), (4, "internal_error"),
    (1, "worker_crashed"), (9, "worker_crashed"), (0, "worker_crashed"),  # 会话中自行退出，含 0
])
def test_worker_exit_code_maps_to_status_code(tmp_path, caplog, rc, code):
    async def scenario():
        async with running(tmp_path, f"crash:{rc}") as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.send(b"\x01\x00" * 100)
            ev = json.loads(await asyncio.wait_for(ws.recv(), 8))
            assert ev["ev"] == "status" and ev["code"] == code and ev["text"]
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1011
            await until(lambda: not gw._session_active)

    asyncio.run(scenario())
    assert f"session_worker_exit rc={rc} status={code}" in caplog.text
    assert FEATURE not in caplog.text


def test_worker_spawn_failure_is_reported(tmp_path):
    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=make_dir(tmp_path), port=0,
                     worker_argv=["/nonexistent/worker-binary"], resolve_host=lambda: "127.0.0.1",
                     allow_non_tailscale_for_tests=True)
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


# ------------------------------------------------------------------ S-F6：token 文件权限与长度

@pytest.mark.parametrize("mode", [0o640, 0o604, 0o660, 0o644, 0o666, 0o602])
def test_token_file_with_group_or_other_bits_is_refused(tmp_path, mode):
    f = _token_file(tmp_path, LONG_TOKEN, mode)
    with pytest.raises(ValueError) as ei:
        gw_mod.load_token(f)
    assert LONG_TOKEN not in str(ei.value)


@pytest.mark.parametrize("mode", [0o600, 0o400])
def test_token_file_owner_only_is_accepted(tmp_path, mode):
    assert gw_mod.load_token(_token_file(tmp_path, LONG_TOKEN, mode)) == LONG_TOKEN


def test_short_token_is_refused_and_never_echoed(tmp_path):
    short = "s" * 31
    f = _token_file(tmp_path, f" {short}\n")  # 去空白后 31 < 32
    with pytest.raises(ValueError) as ei:
        gw_mod.load_token(f)
    assert short not in str(ei.value)
    assert gw_mod.check_token("x" * 32) == "x" * 32
    with pytest.raises(ValueError):
        gw_mod.check_token(" " * 40)


def test_main_refuses_bad_tokens_without_leaking_them(tmp_path, monkeypatch):
    import io

    async def fake_run(token):  # 不真起服务，只看 token 走到了这里
        seen.append(token)

    seen: list[str] = []
    monkeypatch.setattr(gw_mod, "run_gateway", fake_run)
    secret = "short-secret-token"
    monkeypatch.setattr(sys, "stdin", io.StringIO(secret + "\n"))
    with pytest.raises(SystemExit) as ei:
        gw_mod.main(["--token-stdin"])
    assert secret not in str(ei.value) and not seen
    loose = _token_file(tmp_path, LONG_TOKEN, 0o644)
    with pytest.raises(SystemExit) as ei:
        gw_mod.main(["--token-file", str(loose)])
    assert LONG_TOKEN not in str(ei.value) and not seen
    monkeypatch.setattr(sys, "stdin", io.StringIO(f"  {LONG_TOKEN}  \n"))
    gw_mod.main(["--token-stdin"])
    assert seen == [LONG_TOKEN]
    gw_mod.main(["--token-file", str(_token_file(tmp_path, LONG_TOKEN + "\n"))])
    assert seen == [LONG_TOKEN, LONG_TOKEN]


# ------------------------------------------------------------------ S-F2：重复 Authorization 头

def test_duplicate_authorization_headers_are_401(tmp_path):
    async def raw(gw, headers: str) -> int:
        r, w = await asyncio.open_connection("127.0.0.1", gw.tcp_port)
        w.write(f"GET /v1/info HTTP/1.1\r\nHost: x\r\nConnection: close\r\n{headers}\r\n".encode())
        await w.drain()
        data = await asyncio.wait_for(r.read(), 5)
        w.close()
        return int(data.split(b" ", 2)[1])

    async def scenario():
        async with running(tmp_path) as (gw, rec):
            good = f"Authorization: Bearer {TOKEN}\r\n"
            assert await raw(gw, good) == 200
            assert await raw(gw, good + good) == 401  # 两个都对也不行
            assert await raw(gw, good + "Authorization: Bearer nope\r\n") == 401
            assert await raw(gw, "Authorization: Bearer nope\r\n" + good) == 401
            assert await raw(gw, "") == 401
            assert _starts(rec) == []

    asyncio.run(scenario())


# ------------------------------------------------------------------ S-F4：深嵌套 JSON

DEEP = "[" * 60000  # < 64KB，不会先被 max_size 拦下；json.loads 会抛 RecursionError


def test_deeply_nested_json_in_hello_closes_1003_with_log(tmp_path, caplog):
    async def scenario():
        async with running(tmp_path) as (gw, rec):
            ws = await uds_connect(gw)
            await ws.send(DEEP)
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1003
            assert _starts(rec) == []

    asyncio.run(scenario())
    assert "bad_control" in caplog.text and "RecursionError" in caplog.text


def test_deeply_nested_json_mid_session_closes_1003_with_log(tmp_path, caplog):
    async def scenario():
        async with running(tmp_path) as (gw, _rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            await ws.send(DEEP)
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1003  # 不是 1000：不能被吞成正常关闭
            await until(lambda: not gw._session_active)

    asyncio.run(scenario())
    assert "bad_control" in caplog.text and "RecursionError" in caplog.text


# ------------------------------------------------------------------ F2：丢帧后样本时钟不漂移

def test_dropped_audio_is_padded_so_worker_clock_matches_client(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(gw_mod, "AUDIO_QUEUE_MAX_FRAMES", 5)
    frame_bytes = 3200  # 100ms
    n_frames = 300

    async def scenario():
        async with running(tmp_path, "gate") as (gw, rec):
            ws = await uds_connect(gw)
            await hello_ready(ws)
            sent = 0
            for i in range(n_frames):  # 慢 worker 一个字节都不读：队列只有 5 帧，大量丢帧
                await ws.send(bytes([i % 100 + 1, 0]) * (frame_bytes // 2))
                sent += frame_bytes
            await until(lambda: "audio_queue_drop" in caplog.text, what="drops happened")
            Path(str(rec) + ".gate").write_text("go")  # 放 worker 读
            await ws.send(json.dumps({"type": "flush"}))
            statuses, final = [], None
            while final is None:
                ev = json.loads(await asyncio.wait_for(ws.recv(), 15))
                if ev["ev"] == "status":
                    statuses.append(ev)
                elif ev["ev"] == "final":
                    final = ev
            await ws.close()
            return sent, final, statuses

    sent, final, statuses = asyncio.run(scenario())
    assert int(final["text"]) == sent  # worker 收到的总字节数 == 客户端发出的总字节数
    audio_dropped = [e for e in statuses if e["code"] == "audio_dropped"]
    assert audio_dropped and all(e["text"] for e in audio_dropped)
    assert len(audio_dropped) < 20  # 按 1s 合并，不是每个被丢的帧一条
    assert len({e["text"] for e in audio_dropped}) == 1  # 固定短语，不带内容


def test_audio_dropped_status_is_coalesced_per_second(tmp_path, monkeypatch):
    monkeypatch.setattr(gw_mod, "AUDIO_QUEUE_MAX_FRAMES", 1)

    async def scenario():
        gw = Gateway(token=TOKEN, uds_dir=make_dir(tmp_path), port=0)
        sess = gw_mod._Session(gw, ws=None, hello=HELLO, transport="uds")
        sess.ready = True
        for _ in range(1000):
            sess._note_drop(1, 3200)
        return [sess.out_q.get_nowait() for _ in range(sess.out_q.qsize())]

    evs = asyncio.run(scenario())
    assert evs == [{"ev": "status", "code": "audio_dropped", "text": gw_mod.AUDIO_DROPPED_TEXT}]


# ------------------------------------------------------------------ F6：真 TK-001 worker 联调

# 子进程里跑真 worker，只把三个重依赖（Whisper / Silero VAD / 翻译）换成假的：
# 握手、长度前缀帧、分段、事件编号、退出码全是 TK-001 的真代码。
REAL_WORKER_CHILD = textwrap.dedent(
    '''
    import os, sys
    sys.path.insert(0, sys.argv[1])
    import numpy as np
    from realtime_subtitle.node import engines, segmenter, worker

    class FakeAsr(engines.AsrEngine):
        def load(self):
            import time
            time.sleep(float(os.environ.get("FAKE_LOAD_S", "0")))
        def transcribe(self, audio, language):
            return [engines.Utterance(0.1, 0.5, "Hallo Welt.")]
        def info(self):
            return {"model": "fake", "backend": "fake", "rtf": None}

    class FakeTranslator(engines.Translator):
        name = "fake"
        def translate(self, text, src, dst):
            return "你好世界。"

    class FakeVad:
        def __call__(self, w):
            return 1.0 if float(np.sqrt((w.astype(np.float32) ** 2).mean())) > 0.02 else 0.0
        def reset(self):
            pass

    engines.WhisperEngine = FakeAsr
    segmenter.SileroVad = FakeVad
    worker.select_translator = lambda s, d: FakeTranslator()
    worker.main()
    '''
)


def _tone(seconds: float) -> bytes:
    np = pytest.importorskip("numpy")
    t = np.arange(int(seconds * 16000)) / 16000
    return (0.3 * np.sin(2 * np.pi * 440 * t) * 32767).astype("<i2").tobytes()


def _silence(seconds: float) -> bytes:
    return bytes(int(seconds * 16000) * 2)


@contextlib.asynccontextmanager
async def running_real_worker(tmp_path: Path, monkeypatch, load_s: float = 0.0):
    worker_mod = pytest.importorskip("realtime_subtitle.node.worker")
    pytest.importorskip("realtime_subtitle.node.segmenter")
    pytest.importorskip("realtime_subtitle.node.engines")
    child = tmp_path / "real_worker_child.py"
    child.write_text(REAL_WORKER_CHILD)
    pkg_root = Path(worker_mod.__file__).resolve().parents[2]  # 真 worker 所在的包根，原样给子进程
    monkeypatch.setenv("FAKE_LOAD_S", str(load_s))
    gw = Gateway(
        token=TOKEN, uds_dir=make_dir(tmp_path), port=0, resolve_host=lambda: "127.0.0.1",
        allow_non_tailscale_for_tests=True,
        worker_argv=[sys.executable, str(child), str(pkg_root)],
    )
    await gw.start()
    try:
        yield gw
    finally:
        await gw.stop()


async def _recv_until(ws, stop, timeout: float = 15.0) -> list[dict]:
    evs: list[dict] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ev = json.loads(await asyncio.wait_for(ws.recv(), max(0.1, deadline - time.monotonic())))
        evs.append(ev)
        if stop(ev):
            return evs
    raise AssertionError(f"timeout; got {evs}")


def test_real_worker_session_hello_audio_flush_final_and_reuse(tmp_path, monkeypatch):
    async def scenario():
        async with running_real_worker(tmp_path, monkeypatch) as gw:
            ws = await uds_connect(gw)
            await ws.send(json.dumps(HELLO))
            audio = _tone(1.0) + _silence(0.3)
            for i in range(0, len(audio), 3200):
                await ws.send(audio[i:i + 3200])
            await ws.send(json.dumps({"type": "flush"}))
            evs = await _recv_until(ws, lambda e: e["ev"] == "translation")
            await ws.close()
            await until(lambda: not gw._session_active)
            pid = gw.worker.pid
            assert pid and gw.worker.state == "warm"

            # 非法 src：gateway 在 spawn 之前就拒绝，保温中的真 worker 不受影响
            for bad in ("DE", "de\n"):
                ws = await uds_connect(gw)
                await ws.send(json.dumps({**HELLO, "src": bad}))
                await asyncio.wait_for(ws.wait_closed(), 5)
                assert ws.close_code == 1008
                await until(lambda: not gw._session_active)
                assert gw.worker.pid == pid and _alive(pid)

            ws = await uds_connect(gw)  # 复用同一个 worker：cold=False、事件 id 重新从 1 起
            await ws.send(json.dumps(HELLO))
            audio = _tone(1.0) + _silence(0.3)
            for i in range(0, len(audio), 3200):
                await ws.send(audio[i:i + 3200])
            await ws.send(json.dumps({"type": "drain"}))
            evs2 = await _recv_until(ws, lambda e: e["ev"] == "drained")
            await ws.close()
            assert gw.worker.pid == pid
            return evs, evs2

    evs, evs2 = asyncio.run(scenario())
    assert evs[0]["ev"] == "ready" and evs[0]["cold"] is True
    final = next(e for e in evs if e["ev"] == "final")
    assert final["text"] == "Hallo Welt." and final["id"] == 1
    assert next(e for e in evs if e["ev"] == "translation")["text"] == "你好世界。"
    assert evs2[0]["ev"] == "ready" and evs2[0]["cold"] is False
    assert next(e for e in evs2 if e["ev"] == "final")["id"] == 1


def test_real_worker_bad_language_never_reaches_worker(tmp_path, monkeypatch):
    """冷状态下发非法 src：不 spawn（worker 没有被拉起，更不会 bad_hello 退出）。"""
    async def scenario():
        async with running_real_worker(tmp_path, monkeypatch) as gw:
            ws = await uds_connect(gw)
            await ws.send(json.dumps({**HELLO, "src": "DE"}))
            await asyncio.wait_for(ws.wait_closed(), 5)
            assert ws.close_code == 1008
            assert gw.worker.pid is None and gw.worker.state == "cold"

    asyncio.run(scenario())


def test_real_worker_time_axis_survives_dropped_audio(tmp_path, monkeypatch):
    """F2 复现：队列只有 5 帧 + 模型冷加载 1.5s，客户端 8s 静音后才有 1s 音调。
    修复前 worker 只收到末尾几帧，音调被记在 1s 附近；补零之后应在 8~9s。"""
    monkeypatch.setattr(gw_mod, "AUDIO_QUEUE_MAX_FRAMES", 5)

    async def scenario():
        async with running_real_worker(tmp_path, monkeypatch, load_s=1.5) as gw:
            ws = await uds_connect(gw)
            await ws.send(json.dumps(HELLO))
            audio = _silence(8.0) + _tone(1.0) + _silence(0.3)
            for i in range(0, len(audio), 3200):
                await ws.send(audio[i:i + 3200])
            await ws.send(json.dumps({"type": "drain"}))
            evs = await _recv_until(ws, lambda e: e["ev"] == "drained", timeout=30)
            await ws.close()
            return evs

    evs = asyncio.run(scenario())
    assert any(e["ev"] == "status" and e["code"] == "audio_dropped" for e in evs)  # 确实丢过帧
    final = next(e for e in evs if e["ev"] == "final")
    # 音调占客户端时钟的 [8.0, 9.0]；worker 的时间轴必须落在它附近（修复前整体偏早 8s）
    assert 7.5 <= final["a0"] and abs(final["a1"] - 9.0) < 0.5, final


def test_real_worker_cold_load_flush_per_half_second_not_disconnected(tmp_path, monkeypatch):
    """R2-D3：冷加载期间客户端每 0.5s 音频发一次 flush，不得被断开（旧上限下第 72 条被 1008）。"""
    monkeypatch.setattr(gw_mod, "AUDIO_QUEUE_MAX_FRAMES", 3)
    monkeypatch.setattr(gw_mod, "CTL_QUEUE_MAX", 8)

    async def scenario():
        async with running_real_worker(tmp_path, monkeypatch, load_s=1.5) as gw:
            ws = await uds_connect(gw)
            await ws.send(json.dumps(HELLO))
            half = _silence(0.5)
            for _ in range(40):  # 20s 音频，每 0.5s 一次 flush，远超 CTL_QUEUE_MAX
                await ws.send(half)
                await ws.send(json.dumps({"type": "flush"}))
            await ws.send(json.dumps({"type": "drain"}))
            evs = await _recv_until(ws, lambda e: e["ev"] == "drained", timeout=30)
            assert ws.close_code is None
            await ws.close()
            return evs

    evs = asyncio.run(scenario())
    assert evs[0]["ev"] == "ready"


def test_real_worker_cold_load_drops_are_reported_after_ready(tmp_path, monkeypatch):
    """R2-D2：冷加载期间丢帧，客户端收到的第一个事件必须是 ready，随后才是 audio_dropped。"""
    monkeypatch.setattr(gw_mod, "AUDIO_QUEUE_MAX_FRAMES", 3)

    async def scenario():
        async with running_real_worker(tmp_path, monkeypatch, load_s=1.0) as gw:
            ws = await uds_connect(gw)
            await ws.send(json.dumps(HELLO))
            for _ in range(30):
                await ws.send(_silence(0.1))
            await ws.send(json.dumps({"type": "drain"}))
            evs = await _recv_until(ws, lambda e: e["ev"] == "drained", timeout=30)
            await ws.close()
            return evs

    evs = asyncio.run(scenario())
    assert evs[0]["ev"] == "ready"
    drops = [i for i, e in enumerate(evs) if e["ev"] == "status" and e["code"] == "audio_dropped"]
    assert drops and drops[0] == 1  # ready 之后合并发一条


def test_real_worker_disconnect_during_cold_load_then_new_session_gets_one_own_ready(tmp_path, monkeypatch):
    """R2-D1：会话 1 在冷加载中断开、会话 2 随即连上 → 会话 2 恰好一个 ready，且是自己的（cold=False）。"""
    async def scenario():
        async with running_real_worker(tmp_path, monkeypatch, load_s=1.0) as gw:
            ws1 = await uds_connect(gw)
            await ws1.send(json.dumps(HELLO))
            await until(lambda: gw.worker._hellos_written >= 1, what="hello 1 written")
            await ws1.close()
            await until(lambda: not gw._session_active)
            assert gw.worker.state == "warm"

            ws2 = await uds_connect(gw)
            await ws2.send(json.dumps(HELLO))
            audio = _tone(1.0) + _silence(0.3)
            for i in range(0, len(audio), 3200):
                await ws2.send(audio[i:i + 3200])
            await ws2.send(json.dumps({"type": "drain"}))
            evs = await _recv_until(ws2, lambda e: e["ev"] == "drained", timeout=30)
            await ws2.close()
            return evs

    evs = asyncio.run(scenario())
    readies = [e for e in evs if e["ev"] == "ready"]
    assert len(readies) == 1 and evs[0]["ev"] == "ready"
    assert readies[0]["cold"] is False  # 会话 1 的 ready 是 cold=True，不能漏给会话 2
    final = next(e for e in evs if e["ev"] == "final")
    assert final["text"] == "Hallo Welt." and final["id"] == 1
