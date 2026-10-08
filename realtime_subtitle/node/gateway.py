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
# 控制消息（flush/drain/hello）也要有上限（S4）：它们不占音频名额，不设限的话
# 客户端连发 flush 就能把队列撑到内存耗尽。连续的 flush 先合并，仍超限就断开。
CTL_QUEUE_MAX = 64
# 补零 PCM 单次写入的块大小：与客户端单帧上限同量级，惰性生成，不在队列里囤积
_GAP_CHUNK_BYTES = 64 * 1024
AUDIO_DROPPED_TEXT = "音频积压，部分音频已丢弃"
MIN_TOKEN_CHARS = 32
TAILSCALE_CGNAT = ipaddress.ip_network("100.64.0.0/10")

# --- worker 生命周期（架构选择 1）---
WORKER_KEEPALIVE_S = 120.0
WORKER_KILL_GRACE_S = 3.0
WORKER_ARGV = [sys.executable, "-m", "realtime_subtitle.node.worker"]
# worker 的下行事件是 JSON 行；final 再长也不到这个数，超了说明它坏了
_WORKER_LINE_LIMIT = 1024 * 1024

# --- 绑定重试（S2）---
BIND_BACKOFF_FIRST_S = 1.0
BIND_BACKOFF_MAX_S = 60.0

# 与 TK-001 worker 的 hello 校验逐字一致（worker.py 的 _LANG），而且必须 fullmatch：
# `$` 会放过结尾换行。gateway 比 worker 宽的话，worker 收到 hello 后会 bad_hello 退出，
# 把保温中的 worker 白白杀掉。
_LANG_RE = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
# worker 退出码（P3）→ 给客户端的 status.code；其它非 0 与会话中的 0 一律 worker_crashed
_EXIT_STATUS = {2: "protocol_error", 3: "engine_load_failed", 4: "internal_error"}
_STATUS_TEXT = {
    "worker_crashed": "识别进程异常退出",
    "protocol_error": "识别进程协议错误",
    "engine_load_failed": "识别引擎加载失败",
    "internal_error": "识别进程内部错误",
}
_CLIENT_EVENTS = frozenset({"ready", "final", "translation", "status", "drained"})


class GatewayStartError(RuntimeError):
    """启动前置条件不满足（UDS 目录权限、socket 路径被 symlink 等）；起不来是看得见的。"""


# --------------------------------------------------------------------------
# 地址与 token
# --------------------------------------------------------------------------

def validate_host(host: str, *, allow_non_tailscale_for_tests: bool = False) -> str:
    """监听地址必须是 Tailscale 的 IPv4（100.64.0.0/10），且不带任何空白（S2）。

    只拒通配不够：`tailscale ip -4` 的输出若被污染，任意单播地址（8.8.8.8、LAN IP）
    都会被绑上去。`allow_non_tailscale_for_tests` 仅供测试在 127.0.0.1 上起监听，
    生产路径（Gateway 默认）永远不开；开了也照样拒绝通配/组播。
    """
    raw = host or ""
    if raw != raw.strip():
        raise ValueError("监听地址不能带空白")
    try:
        addr = ipaddress.ip_address(raw)
    except ValueError:
        raise ValueError("监听地址必须是明确的 IP（Tailscale IP），不能为空或主机名") from None
    if addr.is_unspecified or addr.is_multicast:
        raise ValueError("监听地址不允许通配/组播地址；节点只绑定到 Tailscale IP")
    if not allow_non_tailscale_for_tests and not (
        isinstance(addr, ipaddress.IPv4Address) and addr in TAILSCALE_CGNAT
    ):
        raise ValueError("监听地址必须落在 Tailscale 地址段 100.64.0.0/10")
    return str(addr)


def resolve_tailscale_ipv4(run: node_info.Runner = node_info.run_command) -> str:
    """每次启动现取 Tailscale IP（S2）：plist 里不写死，IP 变了重启即可跟上。"""
    out = run(["tailscale", "ip", "-4"])
    lines = out.strip().splitlines()
    if not lines:
        raise ValueError("tailscale_no_ipv4")
    return lines[0].strip()


def check_token(token: str) -> str:
    """去空白后校验长度（S5）。错误信息里绝不带 token 本身。"""
    token = token.strip()
    if not token:
        raise ValueError("token 为空：节点必须显式配置 bearer token")
    if len(token) < MIN_TOKEN_CHARS:
        raise ValueError(f"token 太短：至少 {MIN_TOKEN_CHARS} 个字符")
    return token


def load_token(path: str | Path) -> str:
    """读 token 文件；权限含 group/other 位就拒绝（S5 要求 0600）。"""
    with open(path, encoding="utf-8") as f:
        # 用 fstat 而不是先 stat 再 open：检查与读取是同一个文件
        mode = os.fstat(f.fileno()).st_mode
        if mode & 0o077:
            raise ValueError(f"token 文件权限过宽（{stat.S_IMODE(mode):04o}），需要 0600")
        return check_token(f.read())


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

    只有音频帧计入音频上限；满了丢**最旧**的音频帧——字幕要的是最新的内容，
    积压的陈旧音频就算识别出来也已经没用。

    ☠️ 丢帧不能让 worker 的样本时钟前移：worker 按「收到的样本数」算 a0/a1，
    少收一帧，之后所有段的时间戳就整体偏早，客户端按自己的样本时钟替换字幕行
    时会替换到错的行。所以被丢的帧**原位换成一个 `gap` 标记**（只记字节数，
    相邻的合并），喂 worker 时再惰性展开成等长的零 PCM。位置不变，flush 与音频的
    相对顺序不变；队列里囤的只是几个整数，不是零。

    控制消息（hello/flush/drain）有个数上限（`CTL_QUEUE_MAX`）：紧挨着的 flush
    （中间至多隔着 gap，没有真实 PCM）合并成一条；仍然超限就丢**最旧的一条 flush**
    并告警（flush 只是「尽快出结果」的提示，丢了不影响正确性），drain 永不丢——
    只有超限部分全是 drain/hello 时才返回 False 让调用方断开。内存仍然有界。
    """

    def __init__(self, max_frames: int, max_ctl: int | None = None) -> None:
        self._max = max_frames
        self._max_ctl = max_ctl
        self._items: deque[tuple[str, Any]] = deque()
        self._pcm = 0
        self._ctl = 0
        self._wake = asyncio.Event()
        self.flush_dropped = 0
        self._flush_drop_pending = 0
        self._flush_drop_logged_at = float("-inf")

    def __len__(self) -> int:
        return len(self._items)

    def put_pcm(self, data: bytes) -> tuple[int, int]:
        """返回本次丢掉的 (帧数, 字节数)。"""
        dropped = (0, 0)
        if self._pcm >= self._max:
            for i, (kind, payload) in enumerate(self._items):
                if kind == "pcm":
                    self._pcm -= 1
                    self._items[i] = ("gap", len(payload))
                    self._merge_gap(i)
                    dropped = (1, len(payload))
                    break
        self._items.append(("pcm", data))
        self._pcm += 1
        self._wake.set()
        return dropped

    def _merge_gap(self, i: int) -> None:
        """把 i 位置的 gap 与左右相邻的 gap 合并，队列里的 gap 个数因此不超过 ctl 个数 + 1。"""
        if i + 1 < len(self._items) and self._items[i + 1][0] == "gap":
            self._items[i] = ("gap", self._items[i][1] + self._items[i + 1][1])
            del self._items[i + 1]
        if i > 0 and self._items[i - 1][0] == "gap":
            self._items[i - 1] = ("gap", self._items[i - 1][1] + self._items[i][1])
            del self._items[i]

    def put_ctl(self, msg: dict[str, Any]) -> bool:
        """入队一条控制消息。返回 False 表示超限且无 flush 可丢（调用方应断开连接）。"""
        if msg.get("type") == "flush":
            # 从队尾往前跳过 gap（没有真实音频）：碰到 flush 就把它换成这条新的
            # （新 flush 在 gap 之后，语义上覆盖旧的），不增加个数
            for i in range(len(self._items) - 1, -1, -1):
                kind, payload = self._items[i]
                if kind == "gap":
                    continue
                if kind == "ctl" and payload.get("type") == "flush":
                    del self._items[i]
                    self._ctl -= 1
                    self._merge_around(i)
                break
        limit = CTL_QUEUE_MAX if self._max_ctl is None else self._max_ctl
        if self._ctl >= limit and not self._drop_oldest_flush():
            return False
        self._items.append(("ctl", msg))
        self._ctl += 1
        self._wake.set()
        return True

    def _merge_around(self, i: int) -> None:
        """删掉 i 位置的元素后，若左右都是 gap 则合并（保持 gap 个数有界）。"""
        if 0 < i < len(self._items) and self._items[i - 1][0] == "gap" and self._items[i][0] == "gap":
            self._items[i - 1] = ("gap", self._items[i - 1][1] + self._items[i][1])
            del self._items[i]

    def _drop_oldest_flush(self) -> bool:
        for i, (kind, payload) in enumerate(self._items):
            if kind == "ctl" and payload.get("type") == "flush":
                del self._items[i]
                self._ctl -= 1
                self._merge_around(i)
                self.flush_dropped += 1
                self._flush_drop_pending += 1
                now = time.monotonic()
                if now - self._flush_drop_logged_at >= _DROP_LOG_INTERVAL_S:
                    log.warning("control_queue_flush_dropped count=%d limit=%d",
                                self._flush_drop_pending, CTL_QUEUE_MAX)
                    self._flush_drop_pending = 0
                    self._flush_drop_logged_at = now
                return True
        return False

    async def get(self) -> tuple[str, Any]:
        while not self._items:
            self._wake.clear()
            await self._wake.wait()
        kind, payload = self._items.popleft()
        if kind == "pcm":
            self._pcm -= 1
        elif kind == "ctl":
            self._ctl -= 1
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
        # 保温 worker 跨会话：按 hello/ready 一一对应的序号过滤事件（见 _deliver）。
        # 三个计数都随 worker 进程重置。
        self._hellos_written = 0
        self._readies_seen = 0
        self._sink_hello_no: int | None = None

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
            self._sink_hello_no = None  # 本会话的 hello 还没写出，之前的事件都不属于它
            return reused

    def detach(self) -> None:
        """会话结束：worker 继续保温，到期再回收。"""
        self._sink = None
        self._sink_hello_no = None
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
            if payload.get("type") == "hello":
                # 与 write 之间没有 await：记账与「已写出」同步，取消也不会错位
                self._hellos_written += 1
                self._sink_hello_no = self._hellos_written
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
        self._hellos_written = 0
        self._readies_seen = 0
        self._sink_hello_no = None
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
        except (ValueError, RecursionError):
            log.warning("worker_bad_line bytes=%d", len(line))
            return
        if not isinstance(ev, dict) or ev.get("ev") not in _CLIENT_EVENTS:
            log.warning("worker_unknown_event bytes=%d", len(line))
            return
        # worker 对每条 hello 恰好回一个 ready，且按序处理。已写出但尚未收到 ready 的
        # hello 对应的 ready 及其间事件属于更早的会话（它在冷加载中断开了），丢弃；
        # 只有本会话那次 hello 的 ready 到达之后的事件才交给本会话。
        if ev.get("ev") == "ready":
            self._readies_seen += 1
        no = self._sink_hello_no
        if no is None or self._readies_seen < no:
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
        self._drop_logged_at = float("-inf")
        self._drop_notified_at = float("-inf")
        self._dropped_before_ready = False

    # ---- worker 回调（都在事件循环线程里，不需要 threadsafe）----

    def on_worker_event(self, ev: dict[str, Any]) -> None:
        # WorkerManager 已按 hello/ready 序号过滤，交到这里的第一个事件就是本会话的
        # ready；这里再兜一道：ready 之前的非 ready 事件不外发。
        if not self.ready:
            if ev.get("ev") != "ready":
                return
            self.ready = True
            self.out_q.put_nowait(ev)
            if self._dropped_before_ready:
                # 冷加载期间的丢帧只记了账：ready 送达后合并通知一次
                self._drop_notified_at = time.monotonic()
                self._emit_audio_dropped()
            return
        self.out_q.put_nowait(ev)

    def _emit_audio_dropped(self) -> None:
        self.out_q.put_nowait({"ev": "status", "code": "audio_dropped", "text": AUDIO_DROPPED_TEXT})

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
            code = _EXIT_STATUS.get(crash_rc, "worker_crashed")
            log.error("session_worker_exit rc=%d status=%s", crash_rc, code)
            await self._fail(code, flush_pending=True)

    async def _fail(self, code: str, *, flush_pending: bool = False) -> None:
        """告诉客户端出了什么事再关；先把 worker 崩溃前已经产出的事件发完。"""
        with contextlib.suppress(ConnectionClosed):
            if flush_pending:
                while not self.out_q.empty():
                    await self.ws.send(_dump(self.out_q.get_nowait()))
            await self.ws.send(_dump({"ev": "status", "code": code, "text": _STATUS_TEXT.get(code, _STATUS_TEXT["worker_crashed"])}))
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
                if kind == "gap":
                    # 被丢弃的音频换成等长静音，worker 的样本时钟才与客户端一致
                    left = payload
                    while left > 0:
                        n = min(left, _GAP_CHUNK_BYTES)
                        await self.gw.worker.send("pcm", bytes(n))
                        left -= n
                else:
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
        if not self.ready:
            self._dropped_before_ready = True  # ready 前不给客户端发任何东西，只记账
            return
        if now - self._drop_notified_at >= _DROP_LOG_INTERVAL_S:
            # 告诉客户端：这段时间的识别结果可能缺内容（已用静音补齐时间轴）。按 1s 合并
            self._drop_notified_at = now
            self._emit_audio_dropped()

    async def _on_text(self, text: str) -> bool:
        try:
            msg = json.loads(text)
        except (ValueError, RecursionError) as e:
            log.warning("bad_control bytes=%d reason=not_json err=%s", len(text), type(e).__name__)
            await self._safe_close(1003, "bad json")
            return False
        typ = msg.get("type") if isinstance(msg, dict) else None
        if typ in ("flush", "drain"):
            if not self.aq.put_ctl({"type": typ}):
                log.warning("control_queue_overflow limit=%d", CTL_QUEUE_MAX)
                await self._safe_close(1008, "too many control messages")
                return False
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
        allow_non_tailscale_for_tests: bool = False,
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
        # ☠️ 仅测试用：允许监听 127.0.0.1 等非 Tailscale 地址。生产入口（run_gateway）不传。
        self._allow_non_tailscale = allow_non_tailscale_for_tests
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
                host = validate_host(
                    await loop.run_in_executor(None, self._resolve_host),
                    allow_non_tailscale_for_tests=self._allow_non_tailscale,
                )
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
        # 重复的 Authorization 头一律不认（get() 遇到多个会抛 MultipleValuesError → 500）
        values = headers.get_all("Authorization")
        if len(values) != 1:
            return False
        got = values[0] or ""
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
        except (ValueError, RecursionError) as e:
            log.warning("bad_control bytes=%d reason=not_json err=%s phase=hello", len(raw), type(e).__name__)
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
        if not (isinstance(src, str) and isinstance(dst, str) and _LANG_RE.fullmatch(src) and _LANG_RE.fullmatch(dst)):
            log.warning("hello_rejected reason=bad_language")
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
    try:
        token = load_token(args.token_file) if args.token_file else check_token(sys.stdin.readline())
    except (ValueError, OSError) as e:
        # 只报原因，不带 token；OSError 的文本里只有路径
        raise SystemExit(f"token 不可用：{e}") from None
    logging.basicConfig(
        stream=sys.stderr, level=logging.INFO, format="%(asctime)s [node.gateway] %(levelname)s %(message)s"
    )
    asyncio.run(run_gateway(token))


if __name__ == "__main__":
    main()
