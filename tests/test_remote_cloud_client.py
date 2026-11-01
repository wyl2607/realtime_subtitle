"""云端客户端：假 session，不连 Groq。假 key 只存在于这个文件。"""
import ast
import base64
import io
import logging
import os
import re
import sys
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
import requests

from realtime_subtitle.remote.cloud_client import (
    GROQ_TRANSCRIPTIONS_URL,
    CloudCancelled,
    CloudClient,
    CloudError,
    _reset_requests_seconds,
    config_dir,
    groq_key_path,
    read_groq_key,
)
from realtime_subtitle.remote.routing_rules import RoutingState

FAKE_KEY = "gsk_test_DO_NOT_LEAK_7f3a9c"
ENV_KEY = "gsk_env_LEAK_should_not_send"
REMOTE = Path(__file__).resolve().parents[1] / "realtime_subtitle" / "remote"


class FakeResponse:
    def __init__(self, status, headers, payload, reason="Nope"):
        self.status_code = status
        self.headers = headers
        self.payload = payload
        # 故意放进 reason：实现要是把响应原因抄进日志，测试会红。
        self.reason = FAKE_KEY + " Bearer"
        self.closed = False
        self._reason_unused = reason

    def json(self):
        if self.status_code != 200:
            raise AssertionError("error body must not be read")
        return self.payload

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response = response
        self.exc = exc
        self.calls = []
        self.closed = False

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc is not None:
            raise self.exc
        return self.response

    def close(self):
        self.closed = True


def _pcm(seconds=1.0):
    n = int(seconds * 16000)
    return np.arange(n, dtype=np.int16)


def _client(tmp_path, session, key=FAKE_KEY):
    key_path = tmp_path / "groq.key"
    if key is not None:
        key_path.write_text(key + "\n", encoding="utf-8")
    return CloudClient(key_path=key_path, session_factory=lambda: session)


def _assert_clean(caplog, exc):
    blob = caplog.text + str(exc) + repr(exc)
    assert FAKE_KEY not in blob
    assert ENV_KEY not in blob
    assert "Bearer" not in blob


def test_cloud_error_exposes_only_a_key_fingerprint(tmp_path):
    key = "gsk_test_fingerprint_only_7d91a4"
    error = CloudError(status=401, reason="unauthorized", exc_type="HTTPStatus", secret=key)
    assert error.key_fingerprint is not None
    assert len(error.key_fingerprint) == 12
    assert key not in str(error)
    assert key not in repr(error)


def _sent_authorization(kwargs):
    """AuthBase 在 prepare 时写入 Bearer。repr/str 不得含 key。"""
    assert kwargs["allow_redirects"] is False
    auth = kwargs["auth"]
    prepared = requests.PreparedRequest()
    prepared.prepare(
        method="POST",
        url=GROQ_TRANSCRIPTIONS_URL,
        headers=kwargs.get("headers"),
        auth=auth,
    )
    header = prepared.headers["Authorization"]
    token = header.split(" ", 1)[1]
    assert token not in repr(auth)
    assert token not in str(auth)
    assert not header.startswith("Basic")
    return header


def test_post_is_verbose_json_wav_and_maps_segments_onto_a0(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    pcm = _pcm(1.0)
    payload = {
        "text": "TRANSCRIPT_SENTINEL",
        "duration": 2.5,
        "segments": [
            {"start": 0.25, "end": 0.5, "text": " hallo"},
            {"start": 0.5, "end": 1.0, "text": " welt"},
        ],
    }
    session = FakeSession(FakeResponse(
        200,
        {
            "x-groq-id": "req_abc",
            "x-ratelimit-remaining-requests": "1990",
            "x-ratelimit-reset-requests": "2m59.56s",
        },
        payload,
    ))
    client = _client(tmp_path, session)
    result = client.transcribe(pcm, a0=10.0, language="de")
    assert result.text == "TRANSCRIPT_SENTINEL"
    assert result.audio_s == 1.0
    assert result.duration_s == 2.5
    assert result.remaining_requests == 1990
    assert result.reset_requests_s == 179.56
    assert result.groq_id == "req_abc"
    assert isinstance(result.key_fingerprint, str) and len(result.key_fingerprint) == 12
    assert isinstance(result.sent_at, float)
    assert [(seg.start, seg.end, seg.text) for seg in result.segments] == [
        (10.25, 10.5, " hallo"),
        (10.5, 11.0, " welt"),
    ]
    url, kwargs = session.calls[0]
    assert url == GROQ_TRANSCRIPTIONS_URL
    assert kwargs["timeout"] == (5, 10)
    assert kwargs.get("verify", True) is True
    assert "proxies" not in kwargs
    assert kwargs["data"] == {
        "model": "whisper-large-v3",
        "language": "de",
        "response_format": "verbose_json",
    }
    assert _sent_authorization(kwargs) == f"Bearer {FAKE_KEY}"
    name, wav, content_type = kwargs["files"]["file"]
    assert name == "audio.wav"
    assert content_type == "audio/wav"
    with wave.open(io.BytesIO(wav), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getsampwidth() == 2
        assert handle.getframerate() == 16000
        assert handle.readframes(handle.getnframes()) == pcm.tobytes()
    assert "TRANSCRIPT_SENTINEL" not in caplog.text
    assert "x_groq.id=req_abc" in caplog.text
    assert "audio_s=1.000" in caplog.text
    assert "status=200" in caplog.text
    assert FAKE_KEY not in caplog.text
    assert "Bearer" not in caplog.text


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2m59.56s", 179.56),
        ("7.66s", 7.66),
        ("1h2m3s", 3723.0),
        ("0.5s", 0.5),
        ("120ms", 0.12),
        (" abc", None),
        (None, None),
        ("", None),
    ],
)
def test_reset_requests_header_parses_only_complete_groq_durations(raw, expected):
    assert _reset_requests_seconds(raw) == expected


def test_float_pcm_is_encoded_as_s16le(tmp_path):
    session = FakeSession(FakeResponse(200, {"x-groq-id": "req_f"}, {"text": "", "segments": []}))
    client = _client(tmp_path, session)
    client.transcribe(np.array([0.0, 1.0, -1.0], dtype=np.float32), a0=0.0, language="de")
    wav = session.calls[0][1]["files"]["file"][1]
    with wave.open(io.BytesIO(wav), "rb") as handle:
        frames = np.frombuffer(handle.readframes(handle.getnframes()), dtype="<i2")
    assert frames.tolist() == [0, 32767, -32767]


def test_env_key_is_ignored_when_file_is_missing(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("GROQ_API_KEY", ENV_KEY)
    session = FakeSession()
    client = _client(tmp_path, session, key=None)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.1), a0=0.0, language="de")
    assert caught.value.reason == "no_key"
    assert session.calls == []
    _assert_clean(caplog, caught.value)


def test_env_key_does_not_override_file(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", ENV_KEY)
    session = FakeSession(FakeResponse(200, {}, {"text": "ok", "segments": []}))
    client = _client(tmp_path, session)
    client.transcribe(_pcm(0.1), a0=0.0, language="de")
    assert _sent_authorization(session.calls[0][1]) == f"Bearer {FAKE_KEY}"
    assert ENV_KEY not in _sent_authorization(session.calls[0][1])


def test_rewritten_key_file_is_used_on_the_next_request(tmp_path):
    session = FakeSession(FakeResponse(200, {}, {"text": "", "segments": []}))
    key_path = tmp_path / "groq.key"
    key_path.write_text(FAKE_KEY + "\n", encoding="utf-8")
    client = CloudClient(key_path=key_path, session_factory=lambda: session)
    client.transcribe(_pcm(0.1), a0=0.0, language="de")
    key_path.write_text("gsk_test_SECOND_KEY_abcdef\n", encoding="utf-8")
    client.transcribe(_pcm(0.1), a0=0.0, language="de")
    assert _sent_authorization(session.calls[0][1]) == f"Bearer {FAKE_KEY}"
    assert _sent_authorization(session.calls[1][1]) == "Bearer gsk_test_SECOND_KEY_abcdef"


@pytest.mark.parametrize(
    ("status", "reason"),
    [
        (401, "unauthorized"),
        (403, "forbidden"),
        (429, "rate_limited"),
        (500, "server_error"),
        (503, "server_error"),
    ],
)
def test_http_errors_do_not_leak_the_key(tmp_path, caplog, status, reason):
    caplog.set_level(logging.DEBUG)
    headers = {
        "x-groq-id": "req_err",
        "retry-after": "12",
        "x-ratelimit-remaining-requests": "10",
        "x-ratelimit-reset-requests": "120ms",
    }
    session = FakeSession(FakeResponse(status, headers, {"error": FAKE_KEY + " Bearer"}))
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=1.0, language="de")
    err = caught.value
    assert err.status == status
    assert err.reason == reason
    assert err.exc_type == "HTTPStatus"
    assert err.remaining_requests == 10
    assert err.retry_after == 12
    assert err.reset_requests_s == 0.12
    assert isinstance(err.key_fingerprint, str) and len(err.key_fingerprint) == 12
    assert isinstance(err.sent_at, float)
    assert f"status={status}" in caplog.text
    assert f"reason={reason}" in caplog.text
    _assert_clean(caplog, err)


def test_429_without_retry_after_lets_the_state_machine_use_60s(tmp_path):
    session = FakeSession(FakeResponse(429, {"x-groq-id": "req_429"}, {}))
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    assert caught.value.retry_after is None
    state = RoutingState(has_key=True)
    state.apply({"type": "http_status", "status": caught.value.status, "retry_after": caught.value.retry_after})
    assert state.cloud_state == "cooldown_rate_limited"
    assert state.cooldown_seconds == 60


def test_401_and_429_headers_feed_routing_rules(tmp_path):
    session = FakeSession(FakeResponse(401, {"retry-after": "9"}, {}))
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    state = RoutingState(has_key=True)
    state.apply({"type": "http_status", "status": caught.value.status, "retry_after": caught.value.retry_after})
    assert state.cloud_state == "disabled_invalid_key"
    assert state.action == "mark_key_invalid"

    session.response = FakeResponse(429, {"retry-after": "12"}, {})
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    state = RoutingState(has_key=True)
    state.apply({"type": "http_status", "status": caught.value.status, "retry_after": caught.value.retry_after})
    assert state.cooldown_seconds == 12
    assert state.action == "rate_limited"


def test_echoed_key_in_groq_id_is_dropped(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)
    session = FakeSession(FakeResponse(401, {"x-groq-id": FAKE_KEY + " Bearer"}, {}))
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    assert caught.value.groq_id is None
    _assert_clean(caplog, caught.value)


@pytest.mark.parametrize(
    ("exc", "reason", "exc_type"),
    [
        (requests.exceptions.ReadTimeout(FAKE_KEY + " Bearer"), "timeout", "ReadTimeout"),
        (requests.exceptions.ConnectTimeout(FAKE_KEY + " Bearer"), "timeout", "ConnectTimeout"),
        (requests.exceptions.ConnectionError(FAKE_KEY + " Bearer"), "connection_error", "ConnectionError"),
    ],
)
def test_transport_errors_do_not_leak_the_key(tmp_path, caplog, exc, reason, exc_type):
    caplog.set_level(logging.DEBUG)
    session = FakeSession(exc=exc)
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    assert caught.value.status is None
    assert caught.value.reason == reason
    assert caught.value.exc_type == exc_type
    assert isinstance(caught.value.sent_at, float)
    assert f"reason={reason}" in caplog.text
    assert f"exc={exc_type}" in caplog.text
    _assert_clean(caplog, caught.value)


def test_timeout_is_a_segment_failure_for_the_state_machine(tmp_path):
    session = FakeSession(exc=requests.exceptions.ReadTimeout("slow"))
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    assert caught.value.status is None
    state = RoutingState(has_key=True)
    state.apply({"type": "segment_failure"})
    assert state.consecutive_failures == 1
    assert state.cloud_state == "available"


def test_bad_json_does_not_include_the_body(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    class BadSession(FakeSession):
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return self

        def json(self):
            raise ValueError(FAKE_KEY + " Bearer in body")

        def close(self):
            self.closed = True

    session = BadSession()
    session.status_code = 200
    session.headers = {"x-groq-id": "req_bad", "x-ratelimit-reset-requests": "7.66s"}
    client = _client(tmp_path, session)
    with pytest.raises(CloudError) as caught:
        client.transcribe(_pcm(0.2), a0=0.0, language="de")
    assert caught.value.reason == "bad_response"
    assert caught.value.exc_type == "ValueError"
    assert caught.value.reset_requests_s == 7.66
    _assert_clean(caplog, caught.value)


def test_cancel_discards_a_response_that_already_arrived(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    class CancelOnPost(FakeSession):
        def __init__(self):
            super().__init__()
            self.client = None

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            self.client.cancel()
            return FakeResponse(
                200,
                {"x-groq-id": "req_late"},
                {"text": "SECRET_TRANSCRIPT", "segments": [{"start": 0.0, "end": 0.2, "text": "SECRET_TRANSCRIPT"}]},
            )

    session = CancelOnPost()
    client = _client(tmp_path, session)
    session.client = client
    with pytest.raises(CloudCancelled) as caught:
        client.transcribe(_pcm(0.2), a0=3.0, language="de")
    assert caught.value.reason == "cancelled"
    assert "SECRET_TRANSCRIPT" not in caplog.text
    _assert_clean(caplog, caught.value)


def test_cancel_aborts_inflight_and_the_next_request_can_proceed(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    class BlockingSession:
        def __init__(self):
            self.started = threading.Event()
            self.closed = threading.Event()
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            self.started.set()
            if not self.closed.wait(timeout=3):
                raise AssertionError("cancel did not close the session")
            raise requests.ConnectionError(FAKE_KEY + " Bearer")

        def close(self):
            self.closed.set()

    class OkSession:
        def __init__(self):
            self.calls = []

        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            return FakeResponse(
                200,
                {"x-groq-id": "req_ok"},
                {"text": "after", "duration": 0.2, "segments": [{"start": 0.0, "end": 0.2, "text": "after"}]},
            )

        def close(self):
            pass

    made = []

    def factory():
        session = BlockingSession() if not made else OkSession()
        made.append(session)
        return session

    key_path = tmp_path / "groq.key"
    key_path.write_text(FAKE_KEY + "\n", encoding="utf-8")
    client = CloudClient(key_path=key_path, session_factory=factory)
    box = {}

    def run():
        try:
            box["result"] = client.transcribe(_pcm(0.2), a0=0.0, language="de")
        except Exception as exc:
            box["exc"] = exc

    thread = threading.Thread(target=run)
    thread.start()
    assert made[0].started.wait(2)
    client.cancel()
    thread.join(3)
    assert not thread.is_alive()
    assert isinstance(box.get("exc"), CloudCancelled)
    assert "result" not in box
    _assert_clean(caplog, box["exc"])

    result = client.transcribe(_pcm(0.2), a0=4.0, language="de")
    assert result.text == "after"
    assert result.segments[0].start == 4.0
    assert result.segments[0].end == 4.2
    assert len(made[1].calls) == 1


def test_key_path_posix_and_windows(monkeypatch, tmp_path):
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GROQ_API_KEY", ENV_KEY)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))  # Windows 上 Path.home() 读的是它
    assert groq_key_path() == tmp_path / ".config" / "realtime_subtitle" / "groq.key"
    assert config_dir() == tmp_path / ".config" / "realtime_subtitle"
    assert read_groq_key(tmp_path / "missing.key") is None

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("APPDATA", str(tmp_path / "AppData" / "Roaming"))
    assert groq_key_path() == tmp_path / "AppData" / "Roaming" / "realtime_subtitle" / "groq.key"

    monkeypatch.delenv("APPDATA", raising=False)
    with pytest.raises(CloudError) as caught:
        groq_key_path()
    assert caught.value.reason == "appdata_missing"
    assert ENV_KEY not in str(caught.value)
    assert "Bearer" not in repr(caught.value)


def test_read_groq_key_uses_only_the_first_line(tmp_path):
    path = tmp_path / "groq.key"
    path.write_text("\n  " + FAKE_KEY + "  \nignored\n", encoding="utf-8")
    assert read_groq_key(path) == FAKE_KEY
    path.write_text("   \n", encoding="utf-8")
    assert read_groq_key(path) is None


def test_netrc_cannot_override_bearer(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    netrc_path = tmp_path / "netrc"
    netrc_path.write_text("default login x password y\n", encoding="utf-8")
    os.chmod(netrc_path, 0o600)
    monkeypatch.setenv("NETRC", str(netrc_path))
    captured = {}

    class CaptureSession(requests.Session):
        def send(self, request, **kwargs):
            captured["headers"] = dict(request.headers)
            captured["allow_redirects"] = kwargs.get("allow_redirects", True)
            captured["trust_env"] = self.trust_env
            response = requests.Response()
            response.status_code = 200
            response.url = GROQ_TRANSCRIPTIONS_URL
            response.encoding = "utf-8"
            response.headers["x-groq-id"] = "req_netrc"
            response._content = b'{"text":"ok","duration":0.1,"segments":[]}'
            return response

    session = CaptureSession()
    assert session.trust_env is True
    client = _client(tmp_path, session)
    result = client.transcribe(_pcm(0.1), a0=0.0, language="de")
    basic = "Basic " + base64.b64encode(b"x:y").decode("ascii")
    assert captured["trust_env"] is True
    assert captured["allow_redirects"] is False
    assert captured["headers"]["Authorization"] == f"Bearer {FAKE_KEY}"
    assert captured["headers"]["Authorization"] != basic
    assert result.text == "ok"
    assert FAKE_KEY not in caplog.text
    assert "Bearer" not in caplog.text


def test_wall_clock_deadline_aborts_a_slow_stream(tmp_path, caplog):
    caplog.set_level(logging.DEBUG)

    class SlowChunkSession:
        def __init__(self):
            self.closed = threading.Event()
            self.succeeded = threading.Event()
            self.calls = []
            self.chunks = 0
            self.worker = None

        def post(self, url, **kwargs):
            self.worker = threading.current_thread()
            self.calls.append((url, kwargs))
            # 每 2s 一块，共 30s。相邻间隔小于读取超时，整次仍必须在墙钟 10s 失败。
            for _ in range(15):
                if self.closed.is_set():
                    raise requests.ConnectionError("closed")
                time.sleep(2)
                self.chunks += 1
            self.succeeded.set()
            return FakeResponse(200, {"x-groq-id": "req_late"}, {"text": "LATE_OK", "segments": []})

        def close(self):
            self.closed.set()

    session = SlowChunkSession()
    client = _client(tmp_path, session)
    started = time.monotonic()
    try:
        with pytest.raises(CloudError) as caught:
            client.transcribe(_pcm(0.2), a0=0.0, language="de")
        elapsed = time.monotonic() - started
        assert 8.5 <= elapsed <= 11.5
        err = caught.value
        assert not isinstance(err, CloudCancelled)
        assert err.reason == "timeout"
        assert err.status is None
        assert err.exc_type == "Timeout"
        assert "reason=timeout" in caplog.text
        assert "exc=Timeout" in caplog.text
        _assert_clean(caplog, err)
        assert session.calls[0][1]["timeout"] == (5, 10)
        assert session.calls[0][1]["allow_redirects"] is False
        assert session.closed.is_set()
    finally:
        session.close()
        if session.worker is not None:
            session.worker.join(5)
    assert session.worker is not None
    assert not session.worker.is_alive()
    assert not session.succeeded.is_set()
    assert 1 <= session.chunks < 15


def test_remote_source_never_disables_tls_or_reads_a_key_from_the_environment():
    assert list(REMOTE.glob("*.py")), "remote package is missing"
    for path in sorted(REMOTE.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        assert not re.search(r"verify\s*=\s*False", text), path.name
        assert "GROQ_API_KEY" not in text, path.name
        if path.name == "cloud_client.py":
            # 云端按 RFC 走系统/环境代理；节点客户端恰好相反、必须 trust_env=False（不走代理），所以只约束云端
            assert not re.search(r"trust_env\s*=\s*False", text), path.name
        tree = ast.parse(text)
        for name in _env_names(tree):
            assert name == "APPDATA", f"{path.name} reads env {name}"


def _env_names(tree):
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "get" and _is_os_environ(node.func.value):
                names.append(_literal_arg(node))
            elif node.func.attr == "getenv" and _is_os(node.func.value):
                names.append(_literal_arg(node))
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value):
            names.append(_literal_slice(node))
    return names


def _is_os(node):
    return isinstance(node, ast.Name) and node.id == "os"


def _is_os_environ(node):
    return isinstance(node, ast.Attribute) and node.attr == "environ" and _is_os(node.value)


def _literal_arg(node):
    if not node.args or not isinstance(node.args[0], ast.Constant) or not isinstance(node.args[0].value, str):
        return "<dynamic>"
    return node.args[0].value


def _literal_slice(node):
    sl = node.slice
    if isinstance(sl, ast.Constant) and isinstance(sl.value, str):
        return sl.value
    return "<dynamic>"
