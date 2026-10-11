from __future__ import annotations

import asyncio
import json
import os
import socket
import urllib.parse
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import websockets
from websockets.asyncio.server import ServerConnection

from realtime_subtitle.remote.node_client import (
    AudioGapError,
    BadURLError,
    NodeBusyError,
    NodeCallbacks,
    NodeClient,
    NodeConfig,
    NodeIDMismatchError,
    NodeInfo,
    ProtocolError,
    ReadyAudioBuffer,
    SelfNodeError,
    TokenPermissionsError,
    TokenUnavailableError,
    UnauthorizedError,
    WireAudio,
    bounded_gap_pad_samples,
    gap_pad_samples,
    prepare,
    read_token_file,
    validate_and_resolve_node_host,
)


# --- 1. 地址范围校验测试（安全硬规则） ---


def test_ip_validation_tailscale_literal():
    ips = validate_and_resolve_node_host("100.64.0.1")
    assert ips == ["100.64.0.1"]

    ips2 = validate_and_resolve_node_host("100.127.255.254")
    assert ips2 == ["100.127.255.254"]


def test_ip_validation_public_ip_rejected():
    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("8.8.8.8")

    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("1.1.1.1")


def test_ip_validation_non_tailscale_private_ip_rejected():
    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("192.168.1.100")

    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("127.0.0.1")

    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("10.0.0.1")


def test_ip_validation_hostname_resolving_to_tailscale():
    def mock_getaddrinfo(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.1.2", port))]

    ips = validate_and_resolve_node_host("node.example.com", getaddrinfo_fn=mock_getaddrinfo)
    assert ips == ["100.64.1.2"]


def test_ip_validation_hostname_resolving_to_public_rejected():
    def mock_getaddrinfo(host, port, proto=0):
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", port))]

    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("public.example.com", getaddrinfo_fn=mock_getaddrinfo)


def test_ip_validation_mixed_resolution_rejected():
    def mock_getaddrinfo(host, port, proto=0):
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.1.5", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("198.51.100.1", port)),
        ]

    with pytest.raises(BadURLError, match="Tailscale"):
        validate_and_resolve_node_host("mixed.example.com", getaddrinfo_fn=mock_getaddrinfo)


def test_ip_validation_unresolvable_host_rejected():
    def mock_getaddrinfo(host, port, proto=0):
        raise socket.gaierror("Name or service not known")

    with pytest.raises(BadURLError, match="Cannot resolve"):
        validate_and_resolve_node_host("notfound.example.com", getaddrinfo_fn=mock_getaddrinfo)


# --- 2. Token 文件权限与读取测试 ---


def test_read_token_file_success(tmp_path: Path):
    token_file = tmp_path / "token"
    token_file.write_text("secret_token_1234567890\n", encoding="utf-8")
    if os.name == "posix":
        token_file.chmod(0o600)

    token = read_token_file(token_file)
    assert token == "secret_token_1234567890"


def test_read_token_file_permissions_enforced_on_posix(tmp_path: Path):
    if os.name != "posix":
        pytest.skip("POSIX permissions test only applies on POSIX")

    token_file = tmp_path / "token"
    token_file.write_text("secret_token_1234567890\n", encoding="utf-8")
    token_file.chmod(0o644)

    with pytest.raises(TokenPermissionsError) as exc_info:
        read_token_file(token_file)
    assert "secret_token" not in str(exc_info.value)


def test_read_token_file_missing_or_empty(tmp_path: Path):
    missing_file = tmp_path / "nonexistent"
    with pytest.raises(TokenUnavailableError):
        read_token_file(missing_file)

    empty_file = tmp_path / "empty"
    empty_file.write_text("  \n", encoding="utf-8")
    if os.name == "posix":
        empty_file.chmod(0o600)
    with pytest.raises(TokenUnavailableError):
        read_token_file(empty_file)


# --- 3. prepare (/v1/info) 测试 ---


def _info_payload(node_id: str = "target_node", version: int = 2) -> dict:
    return {
        "v": version,
        "node_id": node_id,
        "hw_hash": "abc",
        "asr": {"model": "whisper-large-v3-turbo", "rtf": 0.05},
        "translator": "apple",
        "busy": False,
        "on_ac": True,
        "in_use": False,
        "worker": "warm",
    }


def _capture_session_request(response: MagicMock):
    captured: dict = {}

    def fake_request(self, method, url, **kwargs):
        captured["trust_env"] = self.trust_env
        captured["method"] = method
        captured["url"] = url
        captured["kwargs"] = kwargs
        return response

    return fake_request, captured


def _write_token(tmp_path: Path) -> Path:
    token_file = tmp_path / "token"
    token_file.write_text("test_token\n", encoding="utf-8")
    if os.name == "posix":
        token_file.chmod(0o600)
    return token_file


def test_prepare_success(tmp_path: Path):
    token_file = _write_token(tmp_path)
    config = NodeConfig(id="node1", node_id="target_node", url="ws://100.64.0.10:8791", token_file=str(token_file))

    fake_response = MagicMock()
    fake_response.status_code = 200
    fake_response.json.return_value = _info_payload()

    fake_request, captured = _capture_session_request(fake_response)
    with patch("requests.Session.request", fake_request):
        token, info = prepare(config)
        assert token == "test_token"
        assert info.node_id == "target_node"
        assert info.asr.model == "whisper-large-v3-turbo"
        # trust_env=False：不读环境代理；且不跟随重定向
        assert captured["trust_env"] is False
        assert captured["kwargs"]["allow_redirects"] is False
        assert urllib.parse.urlsplit(captured["url"]).hostname == "100.64.0.10"


def test_prepare_pins_resolved_ipv4_literal(tmp_path: Path):
    token_file = _write_token(tmp_path)
    config = NodeConfig(
        id="node1",
        node_id="target_node",
        url="ws://node.example.ts:8791/v2/session",
        token_file=str(token_file),
    )

    def fake_getaddrinfo(host, port, proto=0):
        assert host == "node.example.ts"
        return [
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.4.4", port)),
            (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.4.5", port)),
        ]

    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = _info_payload()
    fake_request, captured = _capture_session_request(fake_response)
    with patch("requests.Session.request", fake_request):
        prepare(config, getaddrinfo_fn=fake_getaddrinfo)

    parsed = urllib.parse.urlsplit(captured["url"])
    assert parsed.scheme == "http"
    assert parsed.hostname == "100.64.4.4"
    assert parsed.port == 8791
    assert parsed.path == "/v1/info"
    assert "node.example.ts" not in captured["url"]
    assert captured["kwargs"]["allow_redirects"] is False


def test_prepare_ignores_proxy_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    for key in ("ALL_PROXY", "HTTPS_PROXY", "HTTP_PROXY", "all_proxy", "https_proxy", "http_proxy"):
        monkeypatch.setenv(key, "http://127.0.0.1:9")

    token_file = _write_token(tmp_path)
    config = NodeConfig(id="node1", node_id="target_node", url="ws://100.64.0.10:8791", token_file=str(token_file))
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = _info_payload()
    fake_request, captured = _capture_session_request(fake_response)
    with patch("requests.Session.request", fake_request):
        prepare(config)

    assert os.environ["ALL_PROXY"] == "http://127.0.0.1:9"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:9"
    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:9"
    assert captured["method"] == "GET"
    assert captured["trust_env"] is False
    assert captured["kwargs"].get("proxies") in (None, {})


def test_prepare_rejects_unauthorized_and_mismatch(tmp_path: Path):
    token_file = _write_token(tmp_path)
    config = NodeConfig(id="node1", node_id="target_node", url="ws://100.64.0.10:8791", token_file=str(token_file))

    # 401
    resp_401 = MagicMock(status_code=401)
    fake_request, _captured = _capture_session_request(resp_401)
    with patch("requests.Session.request", fake_request):
        with pytest.raises(UnauthorizedError):
            prepare(config)

    # node_id mismatch
    resp_mismatch = MagicMock(status_code=200)
    resp_mismatch.json.return_value = {"v": 2, "node_id": "other_node"}
    fake_request, _captured = _capture_session_request(resp_mismatch)
    with patch("requests.Session.request", fake_request):
        with pytest.raises(NodeIDMismatchError):
            prepare(config)

    # v != 2
    resp_v1 = MagicMock(status_code=200)
    resp_v1.json.return_value = {"v": 1, "node_id": "target_node"}
    fake_request, _captured = _capture_session_request(resp_v1)
    with patch("requests.Session.request", fake_request):
        with pytest.raises(ProtocolError):
            prepare(config)


def test_prepare_rejects_self_node_id(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import realtime_subtitle.node.info as node_info

    state = tmp_path / "state"
    state.mkdir()
    (state / "node_id").write_text("local_box\n", encoding="utf-8")
    monkeypatch.setattr(node_info, "STATE_DIR", state)

    token_file = _write_token(tmp_path)
    config = NodeConfig(id="node1", node_id="expected_other", url="ws://100.64.0.10:8791", token_file=str(token_file))
    fake_response = MagicMock(status_code=200)
    fake_response.json.return_value = _info_payload(node_id="local_box")
    fake_request, _captured = _capture_session_request(fake_response)
    with patch("requests.Session.request", fake_request):
        with pytest.raises(SelfNodeError):
            prepare(config)


# --- 4. ReadyAudioBuffer 测试 ---


def test_ready_audio_buffer_fifo_and_trim():
    one_sec_bytes = 16000 * 2  # 32000 字节
    buffer = ReadyAudioBuffer(max_bytes=one_sec_bytes * 2)

    c1 = WireAudio(start_seconds=0.0, data=b"\x01" * one_sec_bytes)
    c2 = WireAudio(start_seconds=1.0, data=b"\x02" * one_sec_bytes)
    c3 = WireAudio(start_seconds=2.0, data=b"\x03" * one_sec_bytes)

    buffer.append(c1)
    buffer.append(c2)
    assert buffer.byte_count == one_sec_bytes * 2
    assert buffer.dropped_chunks_count == 0

    # 超限丢最早
    buffer.append(c3)
    assert buffer.byte_count == one_sec_bytes * 2
    assert buffer.dropped_chunks_count == 1
    assert buffer.dropped_samples_count == 16000

    drained = buffer.drain_all()
    assert len(drained) == 2
    assert drained[0].start_seconds == 1.0
    assert drained[1].start_seconds == 2.0
    assert buffer.is_empty


# --- 5. 补零算法测试（与 Swift 对齐） ---


def test_gap_pad_samples_calculation():
    # 连续不补零
    assert gap_pad_samples(offset=100.0, sent_samples=16000, audio_start=101.0) == 0
    # 容差内不补零
    assert gap_pad_samples(offset=100.0, sent_samples=16000, audio_start=101.05) == 0
    # 丢 6s 帧算 96000 样本
    assert gap_pad_samples(offset=100.0, sent_samples=16000, audio_start=107.0) == 96000
    # 阈值内（3s）补零
    assert bounded_gap_pad_samples(offset=100.0, sent_samples=16000, audio_start=104.0) == 48000
    # 恰好 5s 仍补零
    assert bounded_gap_pad_samples(offset=100.0, sent_samples=16000, audio_start=106.0) == 80000
    # 超过 5s 返回 None（断档过长）
    assert bounded_gap_pad_samples(offset=100.0, sent_samples=16000, audio_start=107.0) is None
    # 验证 AudioGapError 类型
    assert issubclass(AudioGapError, Exception)


# --- 6. 本地桩 WebSocket 服务器全流程测试 ---


def test_node_client_flow_with_mock_server(tmp_path: Path):
    async def _run():
        token_file = tmp_path / "token"
        token_file.write_text("token123\n", encoding="utf-8")
        if os.name == "posix":
            token_file.chmod(0o600)

        ready_sent = asyncio.Event()

        async def mock_handler(websocket: ServerConnection):
            # 1. 验证首帧为 hello
            hello_raw = await websocket.recv()
            hello = json.loads(hello_raw)
            assert hello["type"] == "hello"
            assert hello["v"] == 2

            # 2. 回复 ready
            ready_msg = {"ev": "ready", "cold": False, "load_s": 0.1, "engine": {"translator": "apple"}}
            await websocket.send(json.dumps(ready_msg))
            ready_sent.set()

            # 3. 接收并回复事件
            while True:
                try:
                    msg = await websocket.recv()
                    if isinstance(msg, str):
                        ev = json.loads(msg)
                        if ev.get("type") == "drain":
                            await websocket.send(json.dumps({"ev": "drained"}))
                    elif isinstance(msg, bytes):
                        await websocket.send(json.dumps({
                            "ev": "final", "id": 1, "a0": 0.0, "a1": 1.0, "text": "Hallo"
                        }))
                        await websocket.send(json.dumps({
                            "ev": "translation", "id": 1, "text": "你好"
                        }))
                except websockets.exceptions.ConnectionClosed:
                    break

        server = await websockets.serve(mock_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        config = NodeConfig(id="node1", node_id="n1", url=f"ws://fake-node.ts:{port}/v2/session", token_file=str(token_file))
        info = NodeInfo(
            v=2, node_id="n1", hw_hash="h",
            asr=NodeInfo.from_dict({}).asr, translator="apple",
            busy=False, on_ac=True, in_use=False, worker="warm"
        )

        finals: list[tuple] = []
        translations: list[tuple] = []
        final_ready = asyncio.Event()
        translation_ready = asyncio.Event()

        def on_final(node_id, gen, fid, text, t0, t1):
            finals.append((node_id, gen, fid, text, t0, t1))
            final_ready.set()

        def on_translation(node_id, gen, fid, text):
            translations.append((node_id, gen, fid, text))
            translation_ready.set()

        callbacks = NodeCallbacks(on_final=on_final, on_translation=on_translation)

        def mock_addrinfo(host, p, proto=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.5", p))]

        client = NodeClient(
            config=config,
            token="token123",
            info=info,
            callbacks=callbacks,
            getaddrinfo_fn=mock_addrinfo,
        )

        real_ws_connect = websockets.connect

        async def patched_connect(uri, **kwargs):
            assert kwargs.get("proxy") is None
            pinned = urllib.parse.urlsplit(uri)
            assert pinned.hostname == "100.64.0.5"
            assert pinned.scheme == "ws"
            assert pinned.path == "/v2/session"
            assert "fake-node.ts" not in uri
            local_uri = f"ws://127.0.0.1:{port}/v2/session"
            return await real_ws_connect(local_uri, **kwargs)

        with patch("websockets.connect", side_effect=patched_connect):
            await client.send(b"\x01\x00" * 1600, start_seconds=10.0)
            assert not client.ready_buffer.is_empty

            await client.connect()
            assert client._is_ready
            assert client.ready_buffer.is_empty

            await client.send(b"\x02\x00" * 1600, start_seconds=10.1)
            await client.drain(timeout=2.0)

            await asyncio.wait_for(final_ready.wait(), timeout=2.0)
            await asyncio.wait_for(translation_ready.wait(), timeout=2.0)
            assert len(finals) >= 1
            assert finals[0][3] == "Hallo"
            assert finals[0][4] == 10.0

            assert len(translations) >= 1
            assert translations[0][3] == "你好"

            await client.close()

        server.close()
        await server.wait_closed()

    asyncio.run(_run())


def test_node_client_distinguishes_busy_1013(tmp_path: Path):
    async def _run():
        token_file = tmp_path / "token"
        token_file.write_text("token123\n", encoding="utf-8")
        if os.name == "posix":
            token_file.chmod(0o600)

        async def busy_handler(websocket: ServerConnection):
            await websocket.close(1013, "busy")

        server = await websockets.serve(busy_handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]

        config = NodeConfig(id="node1", node_id="n1", url=f"ws://fake-node.ts:{port}/v2/session", token_file=str(token_file))
        info = NodeInfo(v=2, node_id="n1", hw_hash="h", asr=NodeInfo.from_dict({}).asr, translator="apple", busy=False, on_ac=True, in_use=False, worker="warm")

        def mock_addrinfo(host, p, proto=0):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("100.64.0.5", p))]

        client = NodeClient(config=config, token="token123", info=info, getaddrinfo_fn=mock_addrinfo)

        real_ws_connect = websockets.connect

        async def patched_connect(uri, **kwargs):
            assert kwargs.get("proxy") is None
            pinned = urllib.parse.urlsplit(uri)
            assert pinned.hostname == "100.64.0.5"
            assert "fake-node.ts" not in uri
            local_uri = f"ws://127.0.0.1:{port}/v2/session"
            return await real_ws_connect(local_uri, **kwargs)

        with patch("websockets.connect", side_effect=patched_connect):
            with pytest.raises(NodeBusyError):
                await client.connect(ready_timeout=1.0)

        server.close()
        await server.wait_closed()

    asyncio.run(_run())
