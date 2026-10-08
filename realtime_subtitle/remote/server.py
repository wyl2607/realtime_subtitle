"""把现有识别/翻译流水线包成最小 WebSocket 服务。

这个模块故意只做远程字幕 PoC：鉴权、单会话、PCM 分块和回调转发。
模型生命周期仍交给 WhisperQueueTranslator，避免远程模式另起一套识别逻辑。
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import signal
import time
from http import HTTPStatus
from pathlib import Path
from typing import Any

import numpy as np

import realtime_subtitle.config as config

SUPPORTED_PATH = "/v1"
SUPPORTED_FORMAT = "s16le"
SUPPORTED_SAMPLE_RATE = 16000


def load_token(path: str | Path) -> str:
    token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise ValueError("token 文件为空：远程服务必须显式配置 bearer token")
    return token


def validate_host(host: str) -> str:
    if host in {"0.0.0.0", "::"}:
        raise ValueError(
            "--host 不允许使用通配地址 0.0.0.0/::；远程识别只允许绑定到明确的内网/Tailscale IP"
        )
    if not host:
        raise ValueError("--host 必填：请显式传入 Mac mini 的 Tailscale IP")
    return host


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Realtime Subtitle remote recognition server")
    parser.add_argument("--host", required=True, help="Mac mini 的 Tailscale IP；禁止 0.0.0.0/::")
    parser.add_argument("--port", type=int, default=8790)
    parser.add_argument("--token-file", required=True)
    args = parser.parse_args(argv)
    try:
        args.host = validate_host(args.host)
    except ValueError as exc:
        parser.error(str(exc))
    return args


def build_translator():
    """服务端模式强制关闭文本存档，再加载现有 translator。

    为什么在内存里改 config 而不是改 config.py：远程 PoC 的隐私边界更窄，
    但本地桌面应用仍保留原来的默认存档行为。
    """
    config.SAVE_TRANSCRIPT = False
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    return WhisperQueueTranslator()


class RemoteSubtitleServer:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        translator: Any,
    ) -> None:
        self.host = validate_host(host)
        self.port = int(port)
        self.token = token
        self.translator = translator
        self._server = None
        self._active = False
        self._active_lock = asyncio.Lock()

    async def start(self) -> None:
        try:
            from websockets.legacy.server import serve
        except ImportError as exc:  # pragma: no cover - 由缺依赖环境触发
            raise RuntimeError(
                "缺少 websockets；请先安装 realtime_subtitle/remote/requirements.txt"
            ) from exc

        self._server = await serve(
            self._handle_ws,
            self.host,
            self.port,
            process_request=self._process_request,
        )
        sockets = getattr(self._server, "sockets", None) or []
        if sockets:
            self.port = sockets[0].getsockname()[1]

    async def stop(self, *, shutdown_translator: bool = False) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        if shutdown_translator:
            shutdown = getattr(self.translator, "shutdown", None)
            if shutdown is not None:
                shutdown()

    async def wait_closed(self) -> None:
        if self._server is not None:
            await self._server.wait_closed()

    def _process_request(self, path, headers):
        if path != SUPPORTED_PATH:
            return (
                HTTPStatus.NOT_FOUND,
                [("Content-Type", "text/plain; charset=utf-8")],
                b"not found\n",
            )
        got = headers.get("Authorization", "")
        expected = f"Bearer {self.token}"
        if not hmac.compare_digest(got, expected):
            return (
                HTTPStatus.UNAUTHORIZED,
                [("Content-Type", "text/plain; charset=utf-8")],
                b"unauthorized\n",
            )
        return None

    async def _handle_ws(self, websocket, _path) -> None:
        async with self._active_lock:
            if self._active:
                await websocket.close(code=1013, reason="busy")
                return
            self._active = True

        session = _RemoteSession(self.translator, websocket)
        try:
            await session.run()
        finally:
            try:
                session.detach_callbacks()
            finally:
                session.clear_context()
                async with self._active_lock:
                    self._active = False


class _RemoteSession:
    def __init__(self, translator: Any, websocket: Any) -> None:
        self.translator = translator
        self.websocket = websocket
        self.loop = asyncio.get_running_loop()
        self.outbox: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.started_at = 0.0
        self._pair_id = 0
        self._pcm = bytearray()
        self._chunk_samples = 0
        self._ready = False
        self._callbacks_attached = False

    async def run(self) -> None:
        sender = asyncio.create_task(self._send_loop())
        ticker = asyncio.create_task(self._flush_loop())
        try:
            async for message in self.websocket:
                if isinstance(message, str):
                    await self._handle_text(message)
                else:
                    await self._handle_binary(message)
        finally:
            for task in (sender, ticker):
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def _flush_loop(self) -> None:
        # ☠️ 桌面版的"说完一段、没有新音频就把尾句冲出去"靠 app 的 500ms QTimer
        # (_flush_check → request_flush) 驱动，translator 自己不会触发。服务端没有 Qt，
        # 不补这个定时器的话，说话人一停最后一句就一直挂着，直到下一段音频进来。
        # 判断"忙时不插队 / 多久算空闲"仍在 translator 里，这里只负责按时敲门。
        while True:
            await asyncio.sleep(0.5)
            if self._ready:
                flush = getattr(self.translator, "request_flush", None)
                if flush is not None:
                    flush()

    async def _send_loop(self) -> None:
        while True:
            payload = await self.outbox.get()
            await self.websocket.send(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            )

    async def _handle_text(self, message: str) -> None:
        try:
            payload = json.loads(message)
        except json.JSONDecodeError:
            await self.websocket.close(code=1003, reason="bad json")
            return

        typ = payload.get("type")
        if typ == "hello":
            await self._handle_hello(payload)
        elif typ == "flush":
            flush = getattr(self.translator, "request_flush", None)
            if flush is not None:
                flush()
        else:
            await self.websocket.close(code=1003, reason="bad message")

    async def _handle_hello(self, payload: dict[str, Any]) -> None:
        if self._ready:
            await self.websocket.close(code=1008, reason="duplicate hello")
            return
        if payload.get("v") != 1:
            await self.websocket.close(code=1008, reason="bad version")
            return
        if payload.get("format") != SUPPORTED_FORMAT or payload.get("sample_rate") != SUPPORTED_SAMPLE_RATE:
            await self.websocket.close(code=1003, reason="unsupported audio")
            return

        src = str(payload.get("src") or "")
        dst = str(payload.get("dst") or "")
        if not src or not dst:
            await self.websocket.close(code=1008, reason="bad language")
            return

        self.started_at = time.monotonic()
        self._chunk_samples = max(
            1,
            int(round(SUPPORTED_SAMPLE_RATE * float(getattr(config, "CHUNK_SUBMIT_SECONDS", 0.5)))),
        )
        self._attach_callbacks()
        # 语言切换必须走 translator 的单一入口；即使当前语言相同，也由 ASR 线程判定。
        self.translator.request_switch_language(src, source="manual", target=dst)
        self._ready = True
        self._enqueue({"ev": "ready", "t": 0.0})

    async def _handle_binary(self, message: bytes) -> None:
        if not self._ready:
            await self.websocket.close(code=1008, reason="hello required")
            return
        self._pcm.extend(message)
        bytes_per_chunk = self._chunk_samples * 2
        while len(self._pcm) >= bytes_per_chunk:
            raw = bytes(self._pcm[:bytes_per_chunk])
            del self._pcm[:bytes_per_chunk]
            audio = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
            self.translator.enqueue_audio(audio, time.time())

    def _attach_callbacks(self) -> None:
        self.translator.on_display = self._on_display
        self.translator.on_pair = self._on_pair
        self.translator.on_draft = self._on_draft
        self.translator.on_status = self._on_status
        self.translator.on_language_applied = self._on_language_applied
        self._callbacks_attached = True

    def detach_callbacks(self) -> None:
        if not self._callbacks_attached:
            return
        for name in (
            "on_display",
            "on_pair",
            "on_draft",
            "on_status",
            "on_language_applied",
        ):
            if getattr(self.translator, name, None) in {
                self._on_display,
                self._on_pair,
                self._on_draft,
                self._on_status,
                self._on_language_applied,
            }:
                setattr(self.translator, name, None)
        self._callbacks_attached = False

    def clear_context(self) -> None:
        clear = getattr(self.translator, "clear_context", None)
        if clear is None:
            return
        executor = getattr(self.translator, "_asr_executor", None)
        if executor is None:
            clear()
            return
        try:
            # 真实 translator 的 clear_context 原本跑在 ASR 线程；断开时也排进同一条队列，
            # 避免和正在处理的一批音频并发重置 processor。
            executor.submit(clear)
        except RuntimeError:
            clear()

    def _elapsed(self) -> float:
        if not self.started_at:
            return 0.0
        return max(0.0, time.monotonic() - self.started_at)

    def _enqueue(self, payload: dict[str, Any]) -> None:
        self.loop.call_soon_threadsafe(self.outbox.put_nowait, payload)

    def _on_display(self, committed_live: str, unstable: str = "") -> None:
        self._enqueue({
            "ev": "volatile",
            "t": self._elapsed(),
            "text": f"{committed_live or ''}{unstable or ''}",
        })

    def _on_pair(self, source_text: str, target_text: str, *_rev) -> None:
        self._pair_id += 1
        pair_id = self._pair_id
        t = self._elapsed()
        self._enqueue({"ev": "final", "t": t, "id": pair_id, "text": source_text or ""})
        self._enqueue({"ev": "translation", "t": t, "id": pair_id, "text": target_text or ""})

    def _on_draft(self, text: str, *_rev) -> None:
        self._enqueue({"ev": "draft", "t": self._elapsed(), "text": text or ""})

    def _on_status(self, text: str) -> None:
        self._enqueue({"ev": "status", "t": self._elapsed(), "text": text or ""})

    def _on_language_applied(self, *_args) -> None:
        # 语言应用事件只用于本地 UI 拒收旧代数；远端协议不需要额外事件。
        return


async def run_server(host: str, port: int, token_file: str | Path) -> None:
    token = load_token(token_file)
    translator = build_translator()
    server = RemoteSubtitleServer(host, port, token, translator)
    await server.start()
    print(f"✅ 远程识别服务已启动: ws://{host}:{server.port}{SUPPORTED_PATH}")

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop_event.set)
    try:
        await stop_event.wait()
    finally:
        await server.stop(shutdown_translator=True)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    asyncio.run(run_server(args.host, args.port, args.token_file))


if __name__ == "__main__":
    main()
