"""节点常驻网关（RFC P1/P2/P3 + S2/S3/S4/S5/S7）。

    python -m realtime_subtitle.node.gateway --token-file <路径>   # 或 --token-stdin

gateway 只做四件事：监听（Tailscale IP:8791 与本机 UDS）、鉴权、卡资源上限、
按需拉起/回收 worker 子进程。识别和翻译全在 worker 里，所以这里**绝不能
import numpy / mlx / torch / faster_whisper**——常驻内存 <50MB 是靠这条守住的，
`test_gateway_does_not_import_heavy_modules` 盯着。

生命周期：无会话 = 无 worker；合法 hello 到达才 spawn（半截握手/垃圾连接不该
把 2GB 的模型拉起来）；会话结束 worker 保温 `WORKER_KEEPALIVE_S`，到期
SIGTERM，`WORKER_KILL_GRACE_S` 后 SIGKILL。进程一退，模型显存/内存随之释放。

日志（S7）：只记事件类型、时长、字节数、帧数、错误码/类名。转录/译文正文、
token、hello 里的任何字段值都不进日志。websockets 自己的 DEBUG 日志会逐帧打印
文本内容，所以给它单独一个钉在 WARNING 的 logger，不受根 logger 级别影响。

时长/上限都是模块常量，函数体里每次现读，测试里 monkeypatch 调短即可。
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import stat
import struct
import sys
import time
from collections import deque
from collections.abc import Callable
from http import HTTPStatus
from pathlib import Path
from typing import Any

from websockets.asyncio.server import serve, unix_serve
from websockets.exceptions import ConnectionClosed

from realtime_subtitle.node import info as node_info

log = logging.getLogger("realtime_subtitle.node.gateway")
_ws_log = logging.getLogger("realtime_subtitle.node.gateway.ws")
_ws_log.setLevel(logging.WARNING)

PORT = 8791
UDS_FILENAME = "gw.sock"
INFO_PATH = "/v1/info"
SESSION_PATH = "/v2/session"

# --- 资源上限（P2 / S4）---
MAX_MESSAGE_BYTES = 64 * 1024
HELLO_TIMEOUT_S = 5.0
SESSION_MAX_S = 4 * 3600.0
PING_INTERVAL_S = 10.0
PING_TIMEOUT_S = 10.0
# ping 超时后 websockets 还要等对端回 close 帧才算断开，默认等 10s。对端已经「死」了，
# 等它回话没意义，而 RFC 验收要求 kill/断网后 30s 内判定断开（10+10 之后只剩 10s 余量）。
CLOSE_TIMEOUT_S = 2.0
SUPPORTED_SAMPLE_RATE = 16000
SUPPORTED_FORMAT = "s16le"
# 每帧约 100ms：200 帧 ≈ 20s 缓冲，够盖住冷启动加载模型的那一段，又不会无限吃内存
AUDIO_QUEUE_MAX_FRAMES = 200
_DROP_LOG_INTERVAL_S = 1.0

# --- worker 生命周期（架构选择 1）---
WORKER_KEEPALIVE_S = 120.0
WORKER_KILL_GRACE_S = 3.0
WORKER_ARGV = [sys.executable, "-m", "realtime_subtitle.node.worker"]
# worker 的下行事件是 JSON 行；final 再长也不到这个数，超了说明它坏了
_WORKER_LINE_LIMIT = 1024 * 1024

# --- 绑定重试（S2）---
BIND_BACKOFF_FIRST_S = 1.0
BIND_BACKOFF_MAX_S = 60.0

_LANG_RE = re.compile(r"^[A-Za-z0-9_-]{1,16}$")
_CLIENT_EVENTS = frozenset({"ready", "final", "translation", "status", "drained"})


class GatewayStartError(RuntimeError):
    """启动前置条件不满足（UDS 目录权限、socket 路径被 symlink 等）；起不来是看得见的。"""


# --------------------------------------------------------------------------
# 地址与 token
# --------------------------------------------------------------------------

def validate_host(host: str) -> str:
    """只允许明确的单播 IP。通配地址（含 `0:0:0:0:0:0:0:0` 之类的变体）一律拒绝。"""
    try:
        addr = ipaddress.ip_address((host or "").strip())
    except ValueError:
        raise ValueError("监听地址必须是明确的 IP（Tailscale IP），不能为空或主机名") from None
    if addr.is_unspecified or addr.is_multicast:
        raise ValueError("监听地址不允许通配/组播地址；节点只绑定到 Tailscale IP")
    return str(addr)


def resolve_tailscale_ipv4(run: node_info.Runner = node_info.run_command) -> str:
    """每次启动现取 Tailscale IP（S2）：plist 里不写死，IP 变了重启即可跟上。"""
    out = run(["tailscale", "ip", "-4"])
    lines = out.strip().splitlines()
    if not lines:
        raise ValueError("tailscale_no_ipv4")
    return lines[0].strip()


def load_token(path: str | Path) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token 文件为空：节点必须显式配置 bearer token")
    return token


# --------------------------------------------------------------------------
# UDS 对端 uid（S3）
# --------------------------------------------------------------------------

def default_peer_uid(sock: socket.socket) -> int:
    """取 UDS 对端的有效 uid。取不到就抛异常，调用方按拒绝处理（fail closed）。"""
    if sys.platform.startswith("linux"):
        creds = sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, struct.calcsize("3i"))
        _pid, uid, _gid = struct.unpack("3i", creds)
        return uid
    if sys.platform == "darwin":
        # 待 Mac 验证：getpeereid(3) 在 libc 里就是读 LOCAL_PEERCRED 这个 sockopt
        # （SOL_LOCAL=0, LOCAL_PEERCRED=1），返回 struct xucred
        # {u_int cr_version; uid_t cr_uid; short cr_ngroups; gid_t cr_groups[16]} = 76 字节。
        buf = sock.getsockopt(0, 0x001, 76)
        _version, uid = struct.unpack_from("II", buf)
        return uid
    raise OSError("peer_uid_unsupported_platform")


# --------------------------------------------------------------------------
# 送往 worker 的有界队列（S4）
# --------------------------------------------------------------------------

class AudioQueue:
    """音频帧与控制消息共用一条队列以保序（flush 必须排在它之前的音频之后）。

    只有音频帧计入上限；满了丢**最旧**的音频帧——字幕要的是最新的内容，
    积压的陈旧音频就算识别出来也已经没用。控制消息（hello/flush/drain）永不丢。
    """

    def __init__(self, max_frames: int) -> None:
        self._max = max_frames
        self._items: deque[tuple[str, Any]] = deque()
        self._pcm = 0
        self._wake = asyncio.Event()

    def put_pcm(self, data: bytes) -> tuple[int, int]:
        """返回本次丢掉的 (帧数, 字节数)。"""
        dropped = (0, 0)
        if self._pcm >= self._max:
            for i, (kind, payload) in enumerate(self._items):
                if kind == "pcm":
                    del self._items[i]
                    self._pcm -= 1
                    dropped = (1, len(payload))
                    break
        self._items.append(("pcm", data))
        self._pcm += 1
        self._wake.set()
        return dropped

    def put_ctl(self, msg: dict[str, Any]) -> None:
        self._items.append(("ctl", msg))
        self._wake.set()

    async def get(self) -> tuple[str, Any]:
        while not self._items:
            self._wake.clear()
            await self._wake.wait()
        kind, payload = self._items.popleft()
        if kind == "pcm":
            self._pcm -= 1
        return kind, payload


# --------------------------------------------------------------------------
# worker 子进程管理
# --------------------------------------------------------------------------

class WorkerManager:
    def __init__(self, argv: list[str]) -> None:
        self._argv = list(argv)
        self._proc: asyncio.subprocess.Process | None = None
        self._reader: asyncio.Task | None = None
        self._sink: Any = None
        self._lock = asyncio.Lock()
        # 每次 attach/detach 递增。保温到期的回收任务只认自己那一代，
        # 这样新会话来了不必去 cancel 一个可能正在 SIGTERM 中途的任务。
        self._idle_gen = 0
        self._idle_task: asyncio.Task | None = None
        self._reaping: asyncio.subprocess.Process | None = None

    @property
    def state(self) -> str:
        return "warm" if self._proc is not None and self._proc.returncode is None else "cold"

    @property
    def pid(self) -> int | None:
        return self._proc.pid if self._proc is not None and self._proc.returncode is None else None

    async def attach(self, sink: Any) -> bool:
        """绑定当前会话；必要时 spawn。返回 True 表示复用了保温中的 worker。"""
        async with self._lock:
            self._idle_gen += 1
            reused = self.state == "warm"
            if not reused:
                await self._spawn()
            self._sink = sink
            return reused

    def detach(self) -> None:
        """会话结束：worker 继续保温，到期再回收。"""
        self._sink = None
        self._idle_gen += 1
        self._idle_task = asyncio.create_task(self._reap_later(self._idle_gen))

    async def shutdown(self) -> None:
        self._idle_gen += 1
        if self._idle_task is not None:
            self._idle_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._idle_task
        async with self._lock:
            await self._terminate_locked("shutdown")

    async def send(self, kind: str, payload: Any) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise BrokenPipeError("worker_not_running")
        if kind == "pcm":
            data = len(payload).to_bytes(4, "big") + payload
        else:
            data = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
        # 一次 write 把整帧塞进缓冲：drain 中途被取消也不会留下半帧
        proc.stdin.write(data)
        await proc.stdin.drain()

    # ---- 内部 ----

    async def _spawn(self) -> None:
        t0 = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *self._argv,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=None,  # worker 日志自己遵守 S7，直接并入 launchd 捕获的 stderr
            limit=_WORKER_LINE_LIMIT,
        )
        self._proc = proc
        self._reader = asyncio.create_task(self._read_events(proc))
        log.info("worker_spawned spawn_s=%.3f", time.monotonic() - t0)

    async def _read_events(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            while True:
                line = await proc.stdout.readline()
                if not line:
                    break
                self._deliver(line)
        except ValueError:
            # 单行超过上限：worker 坏了，掐掉它，走下面统一的退出处理
            log.error("worker_line_too_long limit_bytes=%d", _WORKER_LINE_LIMIT)
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
        rc = await proc.wait()
        if self._proc is proc:
            self._proc = None
        if self._reaping is proc:
            return  # 我们自己回收的，不是崩溃
        if rc != 0:
            log.error("worker_exit code=%d", rc)
        else:
            log.warning("worker_exit code=0 unexpected=1")
        sink = self._sink
        if sink is not None:
            sink.on_worker_exit(rc)

    def _deliver(self, line: bytes) -> None:
        try:
            ev = json.loads(line)
        except ValueError:
            log.warning("worker_bad_line bytes=%d", len(line))
            return
        if not isinstance(ev, dict) or ev.get("ev") not in _CLIENT_EVENTS:
            log.warning("worker_unknown_event bytes=%d", len(line))
            return
        sink = self._sink
        if sink is not None:
            sink.on_worker_event(ev)

    async def _reap_later(self, gen: int) -> None:
        await asyncio.sleep(WORKER_KEEPALIVE_S)
        async with self._lock:
            if gen != self._idle_gen or self._sink is not None:
                return
            await self._terminate_locked("idle")

    async def _terminate_locked(self, reason: str) -> None:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return
        t0 = time.monotonic()
        self._reaping = proc
        how = "term"
        with contextlib.suppress(ProcessLookupError):
            proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), WORKER_KILL_GRACE_S)
        except asyncio.TimeoutError:
            how = "kill"
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await self._reader
        log.info("worker_reaped reason=%s how=%s dur_s=%.3f", reason, how, time.monotonic() - t0)


# --------------------------------------------------------------------------
# 单个会话
# --------------------------------------------------------------------------

def _dump(ev: dict[str, Any]) -> str:
    return json.dumps(ev, ensure_ascii=False, separators=(",", ":"))


class _Session:
    def __init__(self, gw: Gateway, ws: Any, hello: dict[str, Any], transport: str) -> None:
        self.gw = gw
        self.ws = ws
        self.hello = hello
        self.transport = transport
        self.ready = False
        self.out_q: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.aq = AudioQueue(AUDIO_QUEUE_MAX_FRAMES)
        self.exit_fut: asyncio.Future[int] = asyncio.get_running_loop().create_future()
        self.audio_bytes = 0
        self.events_out = 0
        self.dropped_frames = 0
        self.dropped_bytes = 0
        self._drop_pending = [0, 0]
        self._drop_logged_at = 0.0

    # ---- worker 回调（都在事件循环线程里，不需要 threadsafe）----

    def on_worker_event(self, ev: dict[str, Any]) -> None:
        # worker 按顺序处理：新会话的 `ready` 一定排在上一会话遗留事件之后。
        # 在见到 ready 之前收到的全是旧会话的尾巴，丢掉，免得串到新客户端。
        if not self.ready:
            if ev.get("ev") != "ready":
                return
            self.ready = True
        self.out_q.put_nowait(ev)

    def on_worker_exit(self, rc: int) -> None:
        if not self.exit_fut.done():
            self.exit_fut.set_result(rc)

    # ---- 主流程 ----

    async def run(self) -> None:
        t0 = time.monotonic()
        log.info("session_start transport=%s", self.transport)
        try:
            try:
                reused = await self.gw.worker.attach(self)
            except OSError as e:
                log.error("worker_spawn_failed err=%s", type(e).__name__)
                await self._fail("worker_crashed")
                return
            log.info("session_attached worker_reused=%d", int(reused))
            try:
                await self._serve()
            finally:
                self.gw.worker.detach()
        finally:
            log.info(
                "session_end transport=%s dur_s=%.1f audio_bytes=%d events_out=%d dropped_frames=%d dropped_bytes=%d",
                self.transport, time.monotonic() - t0, self.audio_bytes, self.events_out,
                self.dropped_frames, self.dropped_bytes,
            )

    async def _serve(self) -> None:
        # 重建 hello 而不是原样转发：只放行校验过的字段，客户端塞的多余内容到不了 worker
        self.aq.put_ctl({
            "type": "hello", "v": 2,
            "src": self.hello["src"], "dst": self.hello["dst"],
            "sample_rate": SUPPORTED_SAMPLE_RATE, "format": SUPPORTED_FORMAT,
        })
        pump_in = asyncio.create_task(self._pump_in())
        pump_out = asyncio.create_task(self._pump_out())
        feeder = asyncio.create_task(self._feed_worker())
        limit = asyncio.create_task(asyncio.sleep(SESSION_MAX_S))
        crashed = asyncio.ensure_future(self.exit_fut)
        tasks = [pump_in, pump_out, feeder, limit, crashed]
        crash_rc: int | None = None
        try:
            done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            if feeder in done and crashed not in done and feeder.result() is False:
                # 写 worker 管道失败多半是它刚死：给退出码一点时间落地，好区分崩溃与别的
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(asyncio.shield(crashed), 3.0)
                if not crashed.done():
                    self.on_worker_exit(-1)
                done = {crashed}
            if crashed in done:
                crash_rc = crashed.result()
            elif limit in done:
                log.info("session_limit_reached")
                await self._safe_close(1000, "session limit")
        finally:
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        if crash_rc is not None:
            await self._fail("worker_crashed", flush_pending=True)

    async def _fail(self, code: str, *, flush_pending: bool = False) -> None:
        """告诉客户端出了什么事再关；先把 worker 崩溃前已经产出的事件发完。"""
        with contextlib.suppress(ConnectionClosed):
            if flush_pending:
                while not self.out_q.empty():
                    await self.ws.send(_dump(self.out_q.get_nowait()))
            await self.ws.send(_dump({"ev": "status", "code": code, "text": "识别进程异常退出"}))
        await self._safe_close(1011, code)

    async def _safe_close(self, code: int, reason: str) -> None:
        with contextlib.suppress(Exception):
            await self.ws.close(code, reason)

    async def _pump_out(self) -> None:
        try:
            while True:
                ev = await self.out_q.get()
                await self.ws.send(_dump(ev))
                self.events_out += 1
        except ConnectionClosed:
            return

    async def _feed_worker(self) -> bool:
        """把队列里的东西按序写进 worker 管道。管道坏了返回 False。"""
        try:
            while True:
                kind, payload = await self.aq.get()
                await self.gw.worker.send(kind, payload)
        except (BrokenPipeError, ConnectionResetError, OSError):
            return False

    async def _pump_in(self) -> None:
        try:
            async for msg in self.ws:
                if isinstance(msg, str):
                    if not await self._on_text(msg):
                        return
                elif not await self._on_binary(msg):
                    return
        except ConnectionClosed:
            return

    async def _on_binary(self, data: bytes) -> bool:
        if len(data) % 2:
            await self._safe_close(1003, "frame not s16le")
            return False
        if not data:
            return True
        self.audio_bytes += len(data)
        frames, nbytes = self.aq.put_pcm(bytes(data))
        if frames:
            self._note_drop(frames, nbytes)
        return True

    def _note_drop(self, frames: int, nbytes: int) -> None:
        self.dropped_frames += frames
        self.dropped_bytes += nbytes
        self._drop_pending[0] += frames
        self._drop_pending[1] += nbytes
        now = time.monotonic()
        # 积压时每帧都在丢，逐条告警会把日志刷爆：按间隔合并成一行
        if now - self._drop_logged_at >= _DROP_LOG_INTERVAL_S:
            log.warning("audio_queue_drop frames=%d bytes=%d", *self._drop_pending)
            self._drop_pending = [0, 0]
            self._drop_logged_at = now

    async def _on_text(self, text: str) -> bool:
        try:
            msg = json.loads(text)
        except ValueError:
            log.warning("bad_control bytes=%d reason=not_json", len(text))
            await self._safe_close(1003, "bad json")
            return False
        typ = msg.get("type") if isinstance(msg, dict) else None
        if typ in ("flush", "drain"):
            self.aq.put_ctl({"type": typ})
            return True
        if typ == "hello":
            await self._safe_close(1008, "duplicate hello")
        else:
            await self._safe_close(1003, "bad message")
        return False


# --------------------------------------------------------------------------
# Gateway
# --------------------------------------------------------------------------

class Gateway:
    def __init__(
        self,
        *,
        token: str,
        uds_dir: Path | None = None,
        port: int = PORT,
        worker_argv: list[str] | None = None,
        resolve_host: Callable[[], str] | None = None,
        peer_uid: Callable[[socket.socket], int] = default_peer_uid,
        uid: int | None = None,
        info: node_info.NodeInfo | None = None,
        sleep: Callable[[float], Any] = asyncio.sleep,
    ) -> None:
        if not token:
            raise ValueError("token 不能为空")
        self._token = token
        self._uds_dir = Path(uds_dir) if uds_dir is not None else node_info.STATE_DIR
        self._port = port
        self._resolve_host = resolve_host or resolve_tailscale_ipv4
        self._peer_uid = peer_uid
        self._uid = os.getuid() if uid is None else uid
        self._info = info or node_info.NodeInfo(state_dir=self._uds_dir)
        self._sleep = sleep
        self.worker = WorkerManager(worker_argv if worker_argv is not None else WORKER_ARGV)
        self._session_active = False
        self._uds_server = None
        self._tcp_server = None
        self._tcp_task: asyncio.Task | None = None
        self.tcp_ready = asyncio.Event()
        self.tcp_host: str | None = None
        self.tcp_port: int | None = None

    @property
    def uds_path(self) -> Path:
        return self._uds_dir / UDS_FILENAME

    # ---- 启停 ----

    async def start(self) -> None:
        self._prepare_uds()  # 不满足就直接抛：起不来是看得见的，被悄悄放行才危险
        common = dict(
            max_size=MAX_MESSAGE_BYTES,
            ping_interval=PING_INTERVAL_S,
            ping_timeout=PING_TIMEOUT_S,
            close_timeout=CLOSE_TIMEOUT_S,
            compression=None,  # PCM 压不动，白耗 CPU
            logger=_ws_log,
        )
        self._common = common
        self._uds_server = await unix_serve(
            self._handle_uds, str(self.uds_path),
            process_request=self._process_request_uds, **common,
        )
        # 目录 0700 已经挡住了别人，socket 自身再收紧到 0600 是第二层
        os.chmod(self.uds_path, 0o600)
        self._tcp_task = asyncio.create_task(self._tcp_loop())

    async def stop(self) -> None:
        if self._tcp_task is not None:
            self._tcp_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._tcp_task
        for srv in (self._tcp_server, self._uds_server):
            if srv is not None:
                srv.close()
                await srv.wait_closed()
        await self.worker.shutdown()
        with contextlib.suppress(OSError):
            os.unlink(self.uds_path)

    # ---- UDS 准备（S3）----

    def _prepare_uds(self) -> None:
        d = self._uds_dir
        if not d.exists():
            d.mkdir(mode=0o700, parents=True)
        st = os.lstat(d)  # lstat：目录本身是 symlink 也拒绝
        if not stat.S_ISDIR(st.st_mode):
            raise GatewayStartError("uds_dir_not_a_directory")
        if st.st_uid != self._uid:
            raise GatewayStartError("uds_dir_wrong_owner")
        if stat.S_IMODE(st.st_mode) != 0o700:
            raise GatewayStartError("uds_dir_mode_not_0700")
        try:
            st = os.lstat(self.uds_path)
        except FileNotFoundError:
            return
        if stat.S_ISLNK(st.st_mode):
            raise GatewayStartError("uds_path_is_symlink")
        if not stat.S_ISSOCK(st.st_mode):
            raise GatewayStartError("uds_path_not_a_socket")
        os.unlink(self.uds_path)  # 上次崩溃留下的残留 socket

    # ---- TCP 绑定（S2）----

    async def _tcp_loop(self) -> None:
        loop = asyncio.get_running_loop()
        delay = BIND_BACKOFF_FIRST_S
        attempt = 0
        while True:
            try:
                # tailscale 命令会阻塞，放线程池，别卡住已经在服务的 UDS
                host = validate_host(await loop.run_in_executor(None, self._resolve_host))
                self._tcp_server = await serve(
                    self._handle_tcp, host, self._port,
                    process_request=self._process_request_tcp, **self._common,
                )
            except Exception as e:  # noqa: BLE001 - 取不到地址/端口被占/Tailscale 还没起，都等一会再试
                attempt += 1
                log.warning("tcp_bind_failed attempt=%d err=%s retry_in_s=%.0f", attempt, type(e).__name__, delay)
                await self._sleep(delay)
                delay = min(delay * 2, BIND_BACKOFF_MAX_S)
                continue
            sock = self._tcp_server.sockets[0]
            self.tcp_host = host
            self.tcp_port = sock.getsockname()[1]
            log.info("tcp_listening attempt=%d", attempt + 1)
            self.tcp_ready.set()
            return

    # ---- HTTP 层：鉴权 / 路由 ----

    def _authorized(self, headers: Any) -> bool:
        got = headers.get("Authorization", "") or ""
        expected = f"Bearer {self._token}"
        # 常量时间比较；先转 bytes，str 里有非 ASCII 时 compare_digest 会抛 TypeError
        return hmac.compare_digest(got.encode("utf-8"), expected.encode("utf-8"))

    async def _process_request_tcp(self, connection: Any, request: Any):
        if not self._authorized(request.headers):
            log.warning("auth_failed transport=tcp")
            resp = connection.respond(HTTPStatus.UNAUTHORIZED, "unauthorized\n")
            resp.headers["WWW-Authenticate"] = "Bearer"
            return resp
        return await self._route(connection, request)

    async def _process_request_uds(self, connection: Any, request: Any):
        # 每个连接都验：文件权限只是第一层，对端 uid 才是 S3 要的第二层
        try:
            sock = connection.transport.get_extra_info("socket")
            ok = self._peer_uid(sock) == self._uid
        except Exception as e:  # noqa: BLE001 - 取不到对端身份就当不是自己人
            log.warning("peer_uid_unavailable err=%s", type(e).__name__)
            ok = False
        if not ok:
            log.warning("peer_uid_rejected transport=uds")
            return connection.respond(HTTPStatus.FORBIDDEN, "forbidden\n")
        return await self._route(connection, request)

    async def _route(self, connection: Any, request: Any):
        path = request.path.split("?", 1)[0]
        if path == SESSION_PATH:
            return None  # 交给 websockets 完成升级
        if path == INFO_PATH:
            loop = asyncio.get_running_loop()
            body = await loop.run_in_executor(
                None, lambda: self._info.collect(busy=self._session_active, worker=self.worker.state)
            )
            resp = connection.respond(HTTPStatus.OK, json.dumps(body, separators=(",", ":")) + "\n")
            resp.headers["Content-Type"] = "application/json"
            return resp
        return connection.respond(HTTPStatus.NOT_FOUND, "not found\n")

    # ---- WebSocket 会话 ----

    async def _handle_tcp(self, ws: Any) -> None:
        await self._handle(ws, "tcp")

    async def _handle_uds(self, ws: Any) -> None:
        await self._handle(ws, "uds")

    async def _handle(self, ws: Any, transport: str) -> None:
        # 判断与置位之间没有 await，单线程事件循环下是原子的
        if self._session_active:
            log.info("session_busy transport=%s", transport)
            await ws.close(1013, "busy")
            return
        self._session_active = True
        try:
            hello = await self._read_hello(ws)
            if hello is not None:
                await _Session(self, ws, hello, transport).run()
        finally:
            self._session_active = False

    async def _read_hello(self, ws: Any) -> dict[str, Any] | None:
        try:
            raw = await asyncio.wait_for(ws.recv(), HELLO_TIMEOUT_S)
        except asyncio.TimeoutError:
            log.warning("hello_timeout")
            await ws.close(1008, "hello timeout")
            return None
        except ConnectionClosed:
            return None
        if isinstance(raw, bytes):
            await ws.close(1008, "hello required")
            return None
        try:
            msg = json.loads(raw)
        except ValueError:
            await ws.close(1003, "bad json")
            return None
        if not isinstance(msg, dict) or msg.get("type") != "hello":
            await ws.close(1008, "hello required")
            return None
        if msg.get("v") != 2:
            await ws.close(1008, "bad version")
            return None
        # sample_rate/format 只接受一种（S4）：worker 也只认这一种，别让不匹配的音频进管道
        if msg.get("sample_rate") != SUPPORTED_SAMPLE_RATE or msg.get("format") != SUPPORTED_FORMAT:
            log.warning("hello_rejected reason=unsupported_audio")
            await ws.close(1003, "unsupported audio")
            return None
        src, dst = msg.get("src"), msg.get("dst")
        if not (isinstance(src, str) and isinstance(dst, str) and _LANG_RE.match(src) and _LANG_RE.match(dst)):
            await ws.close(1008, "bad language")
            return None
        return msg


# --------------------------------------------------------------------------
# 入口
# --------------------------------------------------------------------------

async def run_gateway(token: str) -> None:
    gw = Gateway(token=token)
    await gw.start()
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    log.info("gateway_started")
    try:
        await stop_event.wait()
    finally:
        await gw.stop()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Realtime Subtitle node gateway")
    # token 只能从文件或 stdin 来：命令行参数会出现在 ps 里（S5）
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--token-file", help="节点 token 文件（权限 0600）")
    g.add_argument("--token-stdin", action="store_true", help="从 stdin 读一行 token")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    token = load_token(args.token_file) if args.token_file else sys.stdin.readline().strip()
    if not token:
        raise SystemExit("token 为空")
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s [node.gateway] %(levelname)s %(message)s"
    )
    asyncio.run(run_gateway(token))


if __name__ == "__main__":
    main()
