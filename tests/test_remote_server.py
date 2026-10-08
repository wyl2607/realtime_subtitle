import asyncio
import json

import numpy as np
import pytest

import realtime_subtitle.config as config
from realtime_subtitle.remote import server as remote_server


class _FakeTranslator:
    def __init__(self):
        self.on_display = None
        self.on_pair = None
        self.on_draft = None
        self.on_status = None
        self.on_language_applied = None
        self.switches = []
        self.enqueued = []
        self.flushes = 0
        self.cleared = 0
        self.shutdowns = 0

    def request_switch_language(self, src, source="manual", target=None):
        self.switches.append((src, source, target))

    def enqueue_audio(self, audio_data, capture_time):
        self.enqueued.append((audio_data.copy(), capture_time))

    def request_flush(self):
        self.flushes += 1

    def clear_context(self):
        self.cleared += 1

    def shutdown(self):
        self.shutdowns += 1

    def emit_pair(self, src="Hallo.", dst="你好。"):
        self.on_pair(src, dst, 0)

    def emit_display(self, committed="live ", unstable="tail"):
        self.on_display(committed, unstable)


def _websockets():
    return pytest.importorskip("websockets")


async def _connect(websockets, uri, token):
    headers = {"Authorization": f"Bearer {token}"}
    try:
        return await websockets.connect(uri, additional_headers=headers)
    except TypeError:
        return await websockets.connect(uri, extra_headers=headers)


async def _start(fake):
    srv = remote_server.RemoteSubtitleServer("127.0.0.1", 0, "secret", fake)
    await srv.start()
    return srv, f"ws://127.0.0.1:{srv.port}/v1"


async def _hello(ws):
    await ws.send(json.dumps({
        "type": "hello",
        "v": 1,
        "src": "de",
        "dst": "zh",
        "sample_rate": 16000,
        "format": "s16le",
    }))
    return json.loads(await ws.recv())


def _status_code(exc):
    response = getattr(exc, "response", None)
    return (
        getattr(response, "status_code", None)
        or getattr(response, "status", None)
        or getattr(exc, "status_code", None)
        or getattr(exc, "status", None)
    )


def test_bad_token_rejects_handshake():
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            with pytest.raises(Exception) as got:
                await _connect(websockets, uri, "wrong")
            assert _status_code(got.value) == 401
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_second_client_gets_1013_busy():
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            first = await _connect(websockets, uri, "secret")
            try:
                assert (await _hello(first))["ev"] == "ready"
                second = await _connect(websockets, uri, "secret")
                try:
                    with pytest.raises(websockets.exceptions.ConnectionClosed) as got:
                        await second.recv()
                    assert got.value.code == 1013
                    assert got.value.reason == "busy"
                finally:
                    await second.close()
            finally:
                await first.close()
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_hello_receives_ready_and_sets_language_pair():
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            ws = await _connect(websockets, uri, "secret")
            try:
                ready = await _hello(ws)
                assert ready == {"ev": "ready", "t": 0.0}
                assert fake.switches == [("de", "manual", "zh")]
            finally:
                await ws.close()
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_binary_frames_are_chunked_and_converted(monkeypatch):
    websockets = _websockets()
    monkeypatch.setattr(config, "CHUNK_SUBMIT_SECONDS", 4 / 16000)

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            ws = await _connect(websockets, uri, "secret")
            try:
                await _hello(ws)
                pcm = np.array([0, 32767, -32768, 16384], dtype="<i2")
                await ws.send(pcm.tobytes())
                deadline = asyncio.get_running_loop().time() + 1
                while not fake.enqueued and asyncio.get_running_loop().time() < deadline:
                    await asyncio.sleep(0.01)
                assert len(fake.enqueued) == 1
                audio, capture_time = fake.enqueued[0]
                np.testing.assert_allclose(
                    audio,
                    np.array([0.0, 32767 / 32768, -1.0, 0.5], dtype=np.float32),
                )
                assert capture_time > 0
            finally:
                await ws.close()
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_pair_emits_final_then_translation_with_incrementing_id():
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            ws = await _connect(websockets, uri, "secret")
            try:
                await _hello(ws)
                fake.emit_pair("Hallo.", "你好。")
                first = json.loads(await ws.recv())
                second = json.loads(await ws.recv())
                assert first["ev"] == "final"
                assert first["id"] == 1
                assert first["text"] == "Hallo."
                assert second["ev"] == "translation"
                assert second["id"] == 1
                assert second["text"] == "你好。"
            finally:
                await ws.close()
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_display_emits_volatile():
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            ws = await _connect(websockets, uri, "secret")
            try:
                await _hello(ws)
                fake.emit_display("原文", "尾巴")
                msg = json.loads(await ws.recv())
                assert msg["ev"] == "volatile"
                assert msg["text"] == "原文尾巴"
            finally:
                await ws.close()
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_disconnect_clears_context():
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            ws = await _connect(websockets, uri, "secret")
            await _hello(ws)
            await ws.close()
            deadline = asyncio.get_running_loop().time() + 1
            while fake.cleared == 0 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert fake.cleared == 1
        finally:
            await srv.stop()

    asyncio.run(scenario())


def test_wildcard_host_is_rejected():
    with pytest.raises(ValueError, match="通配地址"):
        remote_server.validate_host("0.0.0.0")
    with pytest.raises(SystemExit):
        remote_server.parse_args(["--host", "0.0.0.0", "--token-file", "token.txt"])


def test_session_drives_idle_flush_like_desktop_timer():
    # 桌面版靠 app 的 500ms QTimer 调 request_flush 冲出尾句；服务端没有 Qt，必须自己按时调
    websockets = _websockets()

    async def scenario():
        fake = _FakeTranslator()
        srv, uri = await _start(fake)
        try:
            ws = await _connect(websockets, uri, "secret")
            try:
                assert (await _hello(ws))["ev"] == "ready"
                await asyncio.sleep(1.3)
                assert fake.flushes >= 2
            finally:
                await ws.close()
        finally:
            await srv.stop()

    asyncio.run(scenario())
