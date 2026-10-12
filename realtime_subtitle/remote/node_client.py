from __future__ import annotations

import asyncio
import ipaddress
import json
import logging
import os
import socket
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

import websockets
from websockets.exceptions import ConnectionClosed

logger = logging.getLogger("realtime_subtitle.remote.node_client")
# S3 / CLAUDE.md: 日志严格脱敏，绝不打印 token、音频正文、转录正文
logger.setLevel(logging.WARNING)


TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")
FRAME_BYTES = 3200  # 100ms at 16kHz mono s16le
PRE_READY_AUDIO_SECONDS = 15
READY_BUFFER_MAX_BYTES = PRE_READY_AUDIO_SECONDS * 16000 * 2  # 480000 bytes
MAX_PAD_GAP_SECONDS = 5.0
READY_TIMEOUT_SECONDS = 15.0
CLOSE_TIMEOUT_SECONDS = 2.0


class NodeClientError(Exception):
    """节点客户端基础异常。"""


class BadURLError(NodeClientError):
    """节点地址不可用或安全校验失败（非 100.64.0.0/10 地址）。"""


class TokenUnavailableError(NodeClientError):
    """Token 文件不存在或不可读。"""


class TokenPermissionsError(NodeClientError):
    """Token 文件权限不符合 0600 要求。"""


class UnauthorizedError(NodeClientError):
    """节点鉴权失败（401）。"""


class NodeIDMismatchError(NodeClientError):
    """node_id 不一致。"""


class SelfNodeError(NodeClientError):
    """探测到的 node_id 就是本机。路由必须跳过该节点，不得建会话。"""


class NodeBusyError(NodeClientError):
    """节点忙（close 1013）。"""


class HandshakeTimeoutError(NodeClientError):
    """节点握手等待 ready 超时。"""


class ProtocolError(NodeClientError):
    """协议错误或数据格式异常。"""


class ConnectionClosedError(NodeClientError):
    """连接异常断开。"""


class PingTimeoutError(NodeClientError):
    """节点心跳超时。"""


class AudioGapError(NodeClientError):
    """音频断档过长，干净结束会话待重开（非节点故障）。"""


@dataclass
class NodeConfig:
    id: str
    node_id: str
    url: str
    token_file: str

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeConfig:
        return cls(
            id=data["id"],
            node_id=data.get("node_id", ""),
            url=data["url"],
            token_file=data.get("token_file", ""),
        )


@dataclass
class NodeASRInfo:
    model: str | None = None
    backend: str | None = None
    rtf: float | None = None


@dataclass
class NodeInfo:
    v: int
    node_id: str
    hw_hash: str
    asr: NodeASRInfo
    translator: str
    busy: bool
    on_ac: bool
    in_use: bool
    worker: str
    translator_ok: bool = True
    # 节点自报的机器状态（2026-10-12）：旧节点没有这两个字段，None 不罚分
    mem_pressure: str | None = None
    swap_used_gb: float | None = None

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> NodeInfo:
        asr_data = data.get("asr") or {}
        return cls(
            v=int(data.get("v", 0)),
            node_id=str(data.get("node_id", "")),
            hw_hash=str(data.get("hw_hash", "")),
            asr=NodeASRInfo(
                model=asr_data.get("model"),
                backend=asr_data.get("backend"),
                rtf=float(asr_data["rtf"]) if asr_data.get("rtf") is not None else None,
            ),
            translator=str(data.get("translator", "")),
            busy=bool(data.get("busy", False)),
            on_ac=bool(data.get("on_ac", False)),
            in_use=bool(data.get("in_use", False)),
            worker=str(data.get("worker", "")),
            translator_ok=bool(data.get("translator_ok", True)),
            mem_pressure=data.get("mem_pressure") if isinstance(data.get("mem_pressure"), str) else None,
            swap_used_gb=(float(data["swap_used_gb"])
                          if isinstance(data.get("swap_used_gb"), int | float)
                          and not isinstance(data.get("swap_used_gb"), bool) else None),
        )


@dataclass
class NodeSessionRef:
    node_id: str
    generation: int


@dataclass
class NodeCallbacks:
    on_ready: Callable[[str, int, NodeInfo], None] = lambda *_: None
    on_final: Callable[[str, int, int, str, float | None, float | None], None] = lambda *_: None
    on_translation: Callable[[str, int, int, str], None] = lambda *_: None
    on_status: Callable[[str], None] = lambda *_: None
    on_status_event: Callable[[str, int, dict[str, Any]], None] = lambda *_: None
    on_translate_failed: Callable[[str, int, int], None] = lambda *_: None
    on_closed: Callable[[str, int, Exception | None], None] = lambda *_: None


def is_tailscale_ip(ip_str: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_str)
        return isinstance(ip, ipaddress.IPv4Address) and ip in TAILSCALE_NETWORK
    except ValueError:
        return False


def validate_and_resolve_node_host(
    host: str,
    port: int = 8791,
    getaddrinfo_fn: Callable[..., Any] = socket.getaddrinfo,
) -> list[str]:
    """
    连接前把 URL 主机解析出的所有地址都校验在 100.64.0.0/10 内，否则拒连。
    测试要求：公网 IP、主机名解析到公网、混合解析都要拒绝。
    """
    try:
        ip = ipaddress.ip_address(host)
        if not (isinstance(ip, ipaddress.IPv4Address) and ip in TAILSCALE_NETWORK):
            raise BadURLError(f"IP address {host} is not in Tailscale range 100.64.0.0/10")
        return [str(ip)]
    except ValueError:
        pass

    try:
        infos = getaddrinfo_fn(host, port, proto=socket.IPPROTO_TCP)
    except Exception as e:
        raise BadURLError(f"Cannot resolve host {host}: {e}") from e

    if not infos:
        raise BadURLError(f"No address resolved for host {host}")

    resolved_ips: list[str] = []
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        if ip_str not in resolved_ips:
            resolved_ips.append(ip_str)

    for ip_str in resolved_ips:
        if not is_tailscale_ip(ip_str):
            raise BadURLError(
                f"Resolved address {ip_str} for host {host} is not in Tailscale range 100.64.0.0/10"
            )

    return resolved_ips


def read_token_file(path: str | Path) -> str:
    """
    token 只从文件读。文件必须存在，且在 POSIX 系统上权限必须是 0600。
    错误信息和异常中绝不泄露 token。
    """
    expanded = Path(path).expanduser()
    if not expanded.exists() or not expanded.is_file():
        raise TokenUnavailableError(f"Token file {path} not found or not a file")

    if os.name == "posix":
        mode = expanded.stat().st_mode
        if mode & 0o777 != 0o600:
            raise TokenPermissionsError(
                f"Token file {path} permissions must be 0600 (got {oct(mode & 0o777)})"
            )

    try:
        token = expanded.read_text(encoding="utf-8").strip()
    except Exception as e:
        raise TokenUnavailableError(f"Failed to read token file {path}") from e

    if not token:
        raise TokenUnavailableError(f"Token file {path} is empty")

    return token


def _first_ipv4_literal(resolved: list[str]) -> str:
    """校验结果里的第一个 IPv4 字面量。没有则失败，禁止退回主机名。"""
    for ip_str in resolved:
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if isinstance(ip, ipaddress.IPv4Address):
            return str(ip)
    raise BadURLError("Validated address list has no IPv4 literal")


def _split_and_pin_ipv4(
    url: str,
    getaddrinfo_fn: Callable[..., Any],
    *,
    empty_message: str,
) -> tuple[urllib.parse.SplitResult, str]:
    parsed = urllib.parse.urlsplit(url)
    if not parsed.hostname:
        raise BadURLError(empty_message)
    resolved = validate_and_resolve_node_host(
        parsed.hostname,
        parsed.port or 8791,
        getaddrinfo_fn=getaddrinfo_fn,
    )
    return parsed, _first_ipv4_literal(resolved)


def _pinned_info_url(parsed: urllib.parse.SplitResult, ip: str) -> str:
    port = parsed.port or 8791
    scheme = "https" if parsed.scheme in {"wss", "https"} else "http"
    return urllib.parse.urlunsplit((scheme, f"{ip}:{port}", "/v1/info", "", ""))


def _pinned_session_url(parsed: urllib.parse.SplitResult, ip: str) -> str:
    port = parsed.port or 8791
    scheme = parsed.scheme if parsed.scheme in {"ws", "wss"} else "ws"
    path = parsed.path if parsed.path not in {"", "/"} else "/v2/session"
    return urllib.parse.urlunsplit((scheme, f"{ip}:{port}", path, parsed.query, ""))


def _read_self_node_id() -> str | None:
    # 延迟导入：router 在模块加载时依赖本文件，读 node_id 的实现只留在 router。
    from realtime_subtitle.remote.router import read_self_node_id

    return read_self_node_id()


def prepare(
    config: NodeConfig,
    timeout: float = 5.0,
    getaddrinfo_fn: Callable[..., Any] = socket.getaddrinfo,
) -> tuple[str, NodeInfo]:
    """
    准备节点会话：校验 URL、把主机钉成已校验的 IPv4 字面量，再请求 /v1/info。
    Session.trust_env = False，不读 HTTP(S)_PROXY / ALL_PROXY；不跟随重定向。
    探测到的 node_id 与本机相同则抛 SelfNodeError，调用方应跳过自己。
    """
    parsed, ip = _split_and_pin_ipv4(
        config.url,
        getaddrinfo_fn,
        empty_message=f"Invalid node URL: {config.url}",
    )
    token = read_token_file(config.token_file)
    info_url = _pinned_info_url(parsed, ip)

    import requests

    headers = {"Authorization": f"Bearer {token}"}
    with requests.Session() as session:
        session.trust_env = False
        try:
            resp = session.get(
                info_url,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
        except Exception as e:
            raise ConnectionClosedError(f"Failed to connect to {config.id}: {type(e).__name__}") from e

        if resp.status_code == 401:
            raise UnauthorizedError(f"Node {config.id} unauthorized (401)")
        if not (200 <= resp.status_code < 300):
            raise ConnectionClosedError(f"Node {config.id} info returned status {resp.status_code}")

        try:
            data = resp.json()
        except Exception as e:
            raise ProtocolError(f"Invalid JSON from /v1/info: {e}") from e

    if data.get("v") != 2:
        raise ProtocolError(f"Unsupported protocol version: {data.get('v')}")

    probed_node_id = str(data.get("node_id", ""))
    self_node_id = _read_self_node_id()
    if self_node_id and probed_node_id == self_node_id:
        raise SelfNodeError(f"Node {config.id} is this machine; skip")
    if probed_node_id != config.node_id:
        raise NodeIDMismatchError(
            f"Node ID mismatch: expected {config.node_id}, got {probed_node_id}"
        )

    return token, NodeInfo.from_dict(data)


@dataclass
class WireAudio:
    start_seconds: float
    data: bytes

    @property
    def samples(self) -> int:
        return len(self.data) // 2


class ReadyAudioBuffer:
    """
    ready 到达前的有界音频缓冲。超限丢弃最早帧并计数。
    """

    def __init__(self, max_bytes: int = READY_BUFFER_MAX_BYTES) -> None:
        self.max_bytes = max_bytes
        self.chunks: list[WireAudio] = []
        self.byte_count: int = 0
        self.dropped_chunks_count: int = 0
        self.dropped_samples_count: int = 0

    @property
    def is_empty(self) -> bool:
        return len(self.chunks) == 0

    def append(self, audio: WireAudio) -> None:
        if self.max_bytes <= 0 or not audio.data:
            return
        next_chunk = audio
        if len(next_chunk.data) > self.max_bytes:
            keep_bytes = self.max_bytes - (self.max_bytes % 2)
            if keep_bytes <= 0:
                return
            dropped_samples = (len(next_chunk.data) - keep_bytes) // 2
            next_chunk = WireAudio(
                start_seconds=next_chunk.start_seconds + dropped_samples / 16000.0,
                data=next_chunk.data[-keep_bytes:],
            )
            self.dropped_samples_count += dropped_samples

        while self.byte_count + len(next_chunk.data) > self.max_bytes and self.chunks:
            dropped = self.chunks.pop(0)
            self.byte_count -= len(dropped.data)
            self.dropped_chunks_count += 1
            self.dropped_samples_count += dropped.samples

        if len(next_chunk.data) <= self.max_bytes:
            self.chunks.append(next_chunk)
            self.byte_count += len(next_chunk.data)

    def drain_all(self) -> list[WireAudio]:
        drained = self.chunks
        self.chunks = []
        self.byte_count = 0
        return drained

    def remove_all(self) -> None:
        self.chunks.clear()
        self.byte_count = 0


def gap_pad_samples(offset: float, sent_samples: int, audio_start: float, tolerance: float = 0.1) -> int:
    gap = audio_start - (offset + sent_samples / 16000.0)
    if gap <= tolerance:
        return 0
    return int(round(gap * 16000.0))


def bounded_gap_pad_samples(
    offset: float,
    sent_samples: int,
    audio_start: float,
    tolerance: float = 0.1,
    max_gap: float = MAX_PAD_GAP_SECONDS,
) -> int | None:
    gap = audio_start - (offset + sent_samples / 16000.0)
    if gap > max_gap:
        return None
    return gap_pad_samples(offset, sent_samples, audio_start, tolerance)


def _extract_close_code(e: ConnectionClosed) -> int | None:
    rcvd = getattr(e, "rcvd", None)
    if rcvd is not None and getattr(rcvd, "code", None) is not None:
        return int(rcvd.code)
    code = getattr(e, "code", None)
    return int(code) if code is not None else None


class NodeClient:
    """
    协议 v2 节点 WebSocket 客户端。
    - token 只从文件读
    - 连接前强制校验 URL 解析地址在 100.64.0.0/10 内
    - 显式不走代理（proxy=None）
    - ready 前音频进有界缓冲（超限丢最早并计数）
    - 严格区分 1013 busy 与普通断线
    """

    def __init__(
        self,
        config: NodeConfig,
        token: str,
        info: NodeInfo,
        source_locale_id: str = "de-DE",
        target_language_id: str = "zh-Hans",
        offset_seconds: float = 0.0,
        session_generation: int = 1,
        callbacks: NodeCallbacks | None = None,
        getaddrinfo_fn: Callable[..., Any] = socket.getaddrinfo,
    ) -> None:
        self.config = config
        self.token = token
        self.info = info
        self.source_locale_id = source_locale_id
        self.target_language_id = target_language_id
        self.offset_seconds = offset_seconds
        self.session_generation = session_generation
        self.callbacks = callbacks or NodeCallbacks()
        self.getaddrinfo_fn = getaddrinfo_fn

        self.ready_buffer = ReadyAudioBuffer(max_bytes=READY_BUFFER_MAX_BYTES)
        self._ws: Any = None
        self._is_ready = False
        self._is_closed = False
        self._closed_notified = False
        self._clock_started = False
        self._sent_samples = 0
        self._receive_task: asyncio.Task[None] | None = None
        self._drained_event = asyncio.Event()

    async def connect(self, ready_timeout: float = READY_TIMEOUT_SECONDS) -> None:
        parsed, ip = _split_and_pin_ipv4(
            self.config.url,
            self.getaddrinfo_fn,
            empty_message=f"Invalid URL: {self.config.url}",
        )
        # 用已校验的 IPv4 字面量建连，避免 websockets 再次解析主机名。
        session_url = _pinned_session_url(parsed, ip)

        headers = {"Authorization": f"Bearer {self.token}"}
        try:
            self._ws = await websockets.connect(
                session_url,
                additional_headers=headers,
                proxy=None,  # 显式不走代理
                ping_interval=10.0,
                ping_timeout=10.0,
                close_timeout=CLOSE_TIMEOUT_SECONDS,
            )
        except ConnectionClosed as e:
            code = _extract_close_code(e)
            if code == 1013:
                raise NodeBusyError("Node is busy (1013)") from e
            raise ConnectionClosedError(f"Connection closed during connect: {code}") from e
        except Exception as e:
            raise self._classify_error(e)

        try:
            hello = {
                "type": "hello",
                "v": 2,
                "src": self.source_locale_id.split("-")[0].lower(),
                "dst": self.target_language_id.split("-")[0].lower(),
                "sample_rate": 16000,
                "format": "s16le",
            }
            await self._ws.send(json.dumps(hello, sort_keys=True))
            await self._await_ready(timeout=ready_timeout)
            await self._flush_ready_buffer()
            self.callbacks.on_ready(self.config.id, self.session_generation, self.info)
            self._receive_task = asyncio.create_task(self._receive_loop())
        except Exception as e:
            self.ready_buffer.remove_all()
            classified = self._classify_error(e)
            await self.close()
            raise classified

    async def send(self, audio_data: bytes, start_seconds: float) -> None:
        """发送音频数据（16k mono s16le）。ready 前进有界缓冲；ready 后分帧发送并补零。"""
        if self._is_closed:
            raise ConnectionClosedError("Node client is closed")
        if not audio_data:
            return

        wire = WireAudio(start_seconds=start_seconds, data=audio_data)
        if not self._is_ready:
            if not self._clock_started:
                self._clock_started = True
                self.offset_seconds = start_seconds
            self.ready_buffer.append(wire)
            return

        await self._send_wire_audio(wire)

    async def flush(self) -> None:
        if self._ws and not self._is_closed:
            try:
                await self._ws.send(json.dumps({"type": "flush"}))
            except Exception:
                pass

    async def drain(self, timeout: float = 3.0) -> None:
        if not self._ws or self._is_closed:
            return
        self._drained_event.clear()
        try:
            await self._ws.send(json.dumps({"type": "drain"}))
            await asyncio.wait_for(self._drained_event.wait(), timeout=timeout)
        except Exception:
            pass

    async def close(self) -> None:
        if self._is_closed:
            return
        self._is_closed = True
        self.ready_buffer.remove_all()
        if self._receive_task and not self._receive_task.done():
            self._receive_task.cancel()
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass

    async def _send_wire_audio(self, audio: WireAudio) -> None:
        if not self._clock_started:
            self._clock_started = True
            self.offset_seconds = audio.start_seconds
            pad = 0
        else:
            pad = bounded_gap_pad_samples(
                offset=self.offset_seconds,
                sent_samples=self._sent_samples,
                audio_start=audio.start_seconds,
            )

        if pad is None:
            raise AudioGapError("Audio gap exceeded 5s")

        if pad > 0:
            remaining = pad * 2
            while remaining > 0:
                n = min(FRAME_BYTES, remaining)
                await self._ws.send(b"\x00" * n)
                remaining -= n
            self._sent_samples += pad

        pending = audio.data
        while pending:
            n = min(FRAME_BYTES, len(pending))
            await self._ws.send(pending[:n])
            pending = pending[n:]
        self._sent_samples += audio.samples

    async def _flush_ready_buffer(self) -> None:
        while not self.ready_buffer.is_empty:
            batch = self.ready_buffer.drain_all()
            for chunk in batch:
                await self._send_wire_audio(chunk)
        self._is_ready = True

    async def _await_ready(self, timeout: float) -> None:
        async def wait_msg() -> None:
            while True:
                msg = await self._ws.recv()
                if isinstance(msg, str):
                    try:
                        data = json.loads(msg)
                        if data.get("ev") == "ready":
                            return
                    except Exception:
                        pass

        try:
            await asyncio.wait_for(wait_msg(), timeout=timeout)
        except asyncio.TimeoutError as e:
            raise HandshakeTimeoutError(f"Ready handshake timed out after {timeout}s") from e

    async def _receive_loop(self) -> None:
        close_error: Exception | None = None
        try:
            while not self._is_closed:
                msg = await self._ws.recv()
                if isinstance(msg, str):
                    self._handle_event_text(msg)
        except asyncio.CancelledError:
            pass
        except ConnectionClosed as e:
            code = _extract_close_code(e)
            if code == 1013:
                close_error = NodeBusyError("Node is busy (1013)")
            elif code != 1000:
                close_error = ConnectionClosedError(f"Connection closed with code {code}")
        except Exception as e:
            close_error = self._classify_error(e)
        finally:
            self._notify_closed(close_error)

    def _handle_event_text(self, text: str) -> None:
        try:
            event = json.loads(text)
        except Exception:
            return

        ev = event.get("ev")
        if ev == "final":
            final_id = event.get("id")
            body = event.get("text", "")
            a0 = event.get("a0")
            a1 = event.get("a1")
            t0 = (a0 + self.offset_seconds) if a0 is not None else None
            t1 = (a1 + self.offset_seconds) if a1 is not None else None
            if final_id is not None:
                self.callbacks.on_final(self.config.id, self.session_generation, int(final_id), body, t0, t1)
        elif ev == "translation":
            tx_id = event.get("id")
            body = event.get("text", "")
            if tx_id is not None:
                self.callbacks.on_translation(self.config.id, self.session_generation, int(tx_id), body)
        elif ev == "status":
            code = event.get("code", "")
            if code == "translate_failed":
                self.callbacks.on_status_event(self.config.id, self.session_generation, event)
                sid = event.get("id")
                if sid is not None:
                    try:
                        self.callbacks.on_translate_failed(
                            self.config.id, self.session_generation, int(sid)
                        )
                    except (TypeError, ValueError):
                        pass
                return
            self.callbacks.on_status(f"节点 {self.config.id}：{code}")
        elif ev == "drained":
            self._drained_event.set()

    def _notify_closed(self, error: Exception | None) -> None:
        if not self._closed_notified:
            self._closed_notified = True
            self.callbacks.on_closed(self.config.id, self.session_generation, error)

    def _classify_error(self, error: Exception) -> NodeClientError:
        if isinstance(error, NodeClientError):
            return error
        if isinstance(error, ConnectionClosed):
            code = _extract_close_code(error)
            if code == 1013:
                return NodeBusyError("Node busy (1013)")
            return ConnectionClosedError(f"Connection closed with code {code}")
        return ConnectionClosedError(str(error))
