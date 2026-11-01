"""Groq whisper 云端识别客户端。

key 只从配置文件读，不读环境变量。日志只留 x_groq.id、音频时长、状态码、
原因和异常类型：requests 的异常文本和响应体可能带上 Authorization，所以这里
一律不 str() 传输层异常，也不读失败响应的正文。
"""
from __future__ import annotations

import io
import hashlib
import logging
import math
import os
import re
import sys
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np
import requests

from realtime_subtitle.node.segmenter import SAMPLE_RATE

# 任务卡指定 requests 的 timeout=(5, 10)：连接 5s、相邻两次读之间 10s。
# 这挡不住「每 2s 来一小块、整次拖到几十秒」。整次另用墙钟截止。
TIMEOUT = (5, 10)
REQUEST_DEADLINE_S = 10.0
GROQ_TRANSCRIPTIONS_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
MODEL = "whisper-large-v3"
RESPONSE_FORMAT = "verbose_json"

logger = logging.getLogger(__name__)


class _BearerAuth(requests.auth.AuthBase):
    """在 prepare 阶段写入 Bearer。

    Session.trust_env 保持默认 True 时，没传 auth 的请求会被 ~/.netrc / $NETRC
    的 Basic 盖掉手写的 Authorization。auth 对象在 __call__ 里设置头，netrc 就不会
    介入。repr/str 不能带 key：异常和日志有时会打印 auth。
    """

    def __init__(self, key: str):
        self._key = key

    def __call__(self, request: Any) -> Any:
        request.headers["Authorization"] = f"Bearer {self._key}"
        return request

    def __repr__(self) -> str:
        return "BearerAuth(redacted)"

    def __str__(self) -> str:
        return "BearerAuth(redacted)"


class CloudError(Exception):
    """不含 key、不含 Bearer 的失败。字段只给调用方接 routing_rules。"""

    def __init__(
        self,
        *,
        status: int | None,
        reason: str,
        exc_type: str,
        retry_after: int | None = None,
        remaining_requests: int | None = None,
        reset_requests_s: float | None = None,
        groq_id: str | None = None,
        audio_s: float = 0.0,
        duration_s: float | None = None,
        key_fingerprint: str | None = None,
        sent_at: float | None = None,
        secret: str = "",
    ):
        self.status = status
        self.reason = _scrub(reason, secret)
        self.exc_type = _scrub(exc_type, secret)
        self.retry_after = retry_after
        self.remaining_requests = remaining_requests
        self.reset_requests_s = reset_requests_s
        self.groq_id = _public_id(groq_id, secret)
        self.audio_s = audio_s
        self.key_fingerprint = key_fingerprint if key_fingerprint is not None else _key_fingerprint(secret)
        # 请求发出的墙钟秒，供额度账本判定迟到响应属于哪个周期。
        self.sent_at = sent_at
        # 计费时长：有响应 duration 用响应的，否则用送出去的音频长度。max(10, ·) 在 quota。
        self.duration_s = audio_s if duration_s is None else duration_s
        super().__init__(f"status={self.status} reason={self.reason} exc={self.exc_type}")

    def __repr__(self) -> str:
        return f"CloudError(status={self.status!r}, reason={self.reason!r}, exc_type={self.exc_type!r})"


class CloudCancelled(CloudError):
    """关开关之后：在飞请求的结果不能再当成识别结果。"""

    def __init__(
        self,
        *,
        audio_s: float = 0.0,
        groq_id: str | None = None,
        status: int | None = None,
        key_fingerprint: str | None = None,
        sent_at: float | None = None,
        secret: str = "",
    ):
        super().__init__(
            status=status,
            reason="cancelled",
            exc_type="CloudCancelled",
            groq_id=groq_id,
            audio_s=audio_s,
            key_fingerprint=key_fingerprint,
            sent_at=sent_at,
            secret=secret,
        )


@dataclass(frozen=True)
class MappedSegment:
    """Groq segments[].start/end 加上 a0 之后的客户端样本时钟。"""

    start: float
    end: float
    text: str


@dataclass(frozen=True)
class CloudTranscript:
    text: str
    segments: tuple[MappedSegment, ...]
    groq_id: str | None
    audio_s: float
    duration_s: float
    status: int
    remaining_requests: int | None
    reset_requests_s: float | None = None
    key_fingerprint: str | None = None
    sent_at: float | None = None


def config_dir() -> Path:
    """云端配置目录。APPDATA 只用来定位目录，不是密钥。"""
    if sys.platform == "win32":
        appdata = os.environ.get("APPDATA")
        if not appdata:
            raise CloudError(status=None, reason="appdata_missing", exc_type="CloudError")
        return Path(appdata) / "realtime_subtitle"
    return Path.home() / ".config" / "realtime_subtitle"


def groq_key_path() -> Path:
    return config_dir() / "groq.key"


def read_groq_key(path: Path | None = None) -> str | None:
    """读 key 文件的第一行。文件不存在或是空的就返回 None，不查环境变量。"""
    key_path = groq_key_path() if path is None else Path(path)
    try:
        text = key_path.read_text(encoding="utf-8-sig")
    except FileNotFoundError:
        return None
    except OSError as exc:
        # 异常文本不能带上文件内容；OSError 的 strerror 也不含 key，但保险起见不用它。
        raise CloudError(status=None, reason="key_unreadable", exc_type=type(exc).__name__) from None
    for line in text.splitlines():
        key = line.strip()
        if key:
            return key
    return None


def _key_fingerprint(key: str) -> str | None:
    """用于关联一次请求和后续换 key，不保留或输出 key 本身。"""
    if not key:
        return None
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:12]


class CloudClient:
    """一次一段的 Groq 转写。cancel() 关掉当前 session，之后到达的响应丢弃。"""

    def __init__(
        self,
        *,
        key_path: Path | None = None,
        session_factory: Callable[[], Any] | None = None,
    ):
        self._key_path = Path(key_path) if key_path is not None else None
        self._session_factory = session_factory or requests.Session
        self._lock = threading.Lock()
        self._generation = 0
        self._session = self._session_factory()

    def cancel(self) -> None:
        """关开关：作废这一代请求，并关掉它的连接。下一次 transcribe 用新 session。"""
        with self._lock:
            self._generation += 1
            old = self._session
            self._session = self._session_factory()
        _close_quietly(old)

    def transcribe(self, pcm: np.ndarray, *, a0: float, language: str) -> CloudTranscript:
        if isinstance(a0, bool) or not isinstance(a0, (int, float)) or not math.isfinite(float(a0)):
            raise TypeError("a0")
        if not isinstance(language, str) or not language.strip():
            raise TypeError("language")
        samples = _pcm_to_int16(pcm)
        audio_s = len(samples) / SAMPLE_RATE
        generation, session = self._snapshot()
        key = self._read_key()
        if not key:
            self._log(secret="", groq_id=None, audio_s=audio_s, status=None, reason="no_key", exc_type="CloudError")
            raise CloudError(status=None, reason="no_key", exc_type="CloudError", audio_s=audio_s) from None
        if self._generation_now() != generation:
            self._log(secret=key, groq_id=None, audio_s=audio_s, status=None, reason="cancelled", exc_type="CloudCancelled")
            raise CloudCancelled(audio_s=audio_s, secret=key) from None
        sent_at = time.time()
        wav = _wav_bytes(samples)
        outcome: dict[str, Any] = {}
        finished = threading.Event()

        def _post() -> None:
            try:
                try:
                    response = session.post(
                        GROQ_TRANSCRIPTIONS_URL,
                        files={"file": ("audio.wav", wav, "audio/wav")},
                        data={
                            "model": MODEL,
                            "language": language,
                            "response_format": RESPONSE_FORMAT,
                        },
                        auth=_BearerAuth(key),
                        timeout=TIMEOUT,
                        # 重定向会走 rebuild_auth，trust_env 下再次套上 netrc Basic。
                        allow_redirects=False,
                    )
                except Exception as exc:
                    outcome["error"] = exc
                    return
                if self._generation_now() != generation:
                    _close_quietly(response)
                    outcome["discarded"] = response
                    return
                outcome["response"] = response
            finally:
                finished.set()

        worker = threading.Thread(target=_post, name="groq-transcribe", daemon=True)
        worker.start()
        if not finished.wait(REQUEST_DEADLINE_S):
            # 先关掉连接，晚到的成功结果不能再当成识别结果。
            self.cancel()
            _close_quietly(outcome.get("response"))
            _close_quietly(outcome.get("discarded"))
            self._log(secret=key, groq_id=None, audio_s=audio_s, status=None, reason="timeout", exc_type="Timeout")
            raise CloudError(
                status=None,
                reason="timeout",
                exc_type="Timeout",
                audio_s=audio_s,
                sent_at=sent_at,
                secret=key,
            ) from None
        error = outcome.get("error")
        if error is not None:
            if isinstance(error, requests.Timeout):
                self._transport_failure(generation, audio_s, "timeout", error, key, sent_at)
            elif isinstance(error, requests.ConnectionError):
                self._transport_failure(generation, audio_s, "connection_error", error, key, sent_at)
            elif isinstance(error, requests.RequestException):
                self._transport_failure(generation, audio_s, "request_error", error, key, sent_at)
            raise error
        if "response" not in outcome:
            discarded = outcome.get("discarded")
            groq_id = _header(getattr(discarded, "headers", None), "x-groq-id")
            status = _status_code(discarded) if discarded is not None else None
            self._log(secret=key, groq_id=groq_id, audio_s=audio_s, status=status, reason="cancelled", exc_type="CloudCancelled")
            raise CloudCancelled(audio_s=audio_s, groq_id=groq_id, status=status, sent_at=sent_at, secret=key) from None
        response = outcome["response"]
        if self._generation_now() != generation:
            _close_quietly(response)
            groq_id = _header(getattr(response, "headers", None), "x-groq-id")
            status = _status_code(response)
            self._log(secret=key, groq_id=groq_id, audio_s=audio_s, status=status, reason="cancelled", exc_type="CloudCancelled")
            raise CloudCancelled(audio_s=audio_s, groq_id=groq_id, status=status, sent_at=sent_at, secret=key) from None
        try:
            return self._handle_response(response, a0=float(a0), audio_s=audio_s, secret=key, sent_at=sent_at)
        finally:
            _close_quietly(response)

    def _handle_response(
        self, response: Any, *, a0: float, audio_s: float, secret: str, sent_at: float | None = None,
    ) -> CloudTranscript:
        status = _status_code(response)
        headers = getattr(response, "headers", None)
        groq_id = _header(headers, "x-groq-id")
        remaining = _nonnegative_int(_header(headers, "x-ratelimit-remaining-requests"))
        retry_after = _nonnegative_int(_header(headers, "retry-after"))
        reset_requests_s = _reset_requests_seconds(_header(headers, "x-ratelimit-reset-requests"))
        if status != 200:
            reason = _http_reason(status)
            self._log(secret=secret, groq_id=groq_id, audio_s=audio_s, status=status, reason=reason, exc_type="HTTPStatus")
            raise CloudError(
                status=status,
                reason=reason,
                exc_type="HTTPStatus",
                retry_after=retry_after,
                remaining_requests=remaining,
                reset_requests_s=reset_requests_s,
                groq_id=groq_id,
                audio_s=audio_s,
                sent_at=sent_at,
                secret=secret,
            ) from None
        try:
            payload = response.json()
        except CloudError:
            raise
        except Exception as exc:
            # 不把解码失败的正文放进异常：失败页可能回显请求头。
            exc_type = type(exc).__name__
            self._log(secret=secret, groq_id=groq_id, audio_s=audio_s, status=status, reason="bad_response", exc_type=exc_type)
            raise CloudError(
                status=status,
                reason="bad_response",
                exc_type=exc_type,
                remaining_requests=remaining,
                reset_requests_s=reset_requests_s,
                groq_id=groq_id,
                audio_s=audio_s,
                sent_at=sent_at,
                secret=secret,
            ) from None
        if not isinstance(payload, dict):
            self._log(secret=secret, groq_id=groq_id, audio_s=audio_s, status=status, reason="bad_response", exc_type="CloudError")
            raise CloudError(
                status=status,
                reason="bad_response",
                exc_type="CloudError",
                remaining_requests=remaining,
                reset_requests_s=reset_requests_s,
                groq_id=groq_id,
                audio_s=audio_s,
                sent_at=sent_at,
                secret=secret,
            ) from None
        duration_s = _response_duration(payload, audio_s)
        text = payload.get("text")
        if not isinstance(text, str):
            text = ""
        segments = _map_segments(payload.get("segments"), a0)
        self._log(secret=secret, groq_id=groq_id, audio_s=audio_s, status=status, reason="ok", exc_type="-")
        return CloudTranscript(
            text=text,
            segments=segments,
            groq_id=_public_id(groq_id, secret),
            audio_s=audio_s,
            duration_s=duration_s,
            status=status,
            remaining_requests=remaining,
            reset_requests_s=reset_requests_s,
            key_fingerprint=_key_fingerprint(secret),
            sent_at=sent_at,
        )

    def _transport_failure(
        self,
        generation: int,
        audio_s: float,
        reason: str,
        exc: BaseException,
        secret: str,
        sent_at: float | None = None,
    ) -> None:
        exc_type = type(exc).__name__
        if self._generation_now() != generation:
            self._log(secret=secret, groq_id=None, audio_s=audio_s, status=None, reason="cancelled", exc_type=exc_type)
            raise CloudCancelled(audio_s=audio_s, sent_at=sent_at, secret=secret) from None
        self._log(secret=secret, groq_id=None, audio_s=audio_s, status=None, reason=reason, exc_type=exc_type)
        raise CloudError(
            status=None,
            reason=reason,
            exc_type=exc_type,
            audio_s=audio_s,
            sent_at=sent_at,
            secret=secret,
        ) from None

    def _read_key(self) -> str | None:
        return read_groq_key(self._key_path)

    def _snapshot(self) -> tuple[int, Any]:
        with self._lock:
            return self._generation, self._session

    def _generation_now(self) -> int:
        with self._lock:
            return self._generation

    def _log(
        self,
        *,
        secret: str,
        groq_id: str | None,
        audio_s: float,
        status: int | None,
        reason: str,
        exc_type: str,
    ) -> None:
        public_id = _public_id(groq_id, secret) or "-"
        message = " ".join((
            f"x_groq.id={public_id}",
            f"audio_s={audio_s:.3f}",
            f"status={'-' if status is None else status}",
            f"reason={_scrub(reason, secret)}",
            f"exc={_scrub(exc_type, secret)}",
        ))
        logger.info("%s", _scrub(message, secret))


def _pcm_to_int16(pcm: np.ndarray) -> np.ndarray:
    arr = np.asarray(pcm)
    if arr.ndim != 1:
        arr = arr.reshape(-1)
    if arr.dtype == np.int16:
        return np.ascontiguousarray(arr)
    if np.issubdtype(arr.dtype, np.floating):
        clipped = np.clip(arr, -1.0, 1.0)
        return np.ascontiguousarray((clipped * 32767.0).astype(np.int16))
    raise TypeError("pcm")


def _wav_bytes(samples: np.ndarray) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(samples.tobytes())
    return buf.getvalue()


def _map_segments(segments: Any, a0: float) -> tuple[MappedSegment, ...]:
    # WAV 的样本 0 就是 a0（cloud_segmenter 从语音起点切，不含前垫），所以直接加 a0。
    if not isinstance(segments, list):
        return ()
    mapped: list[MappedSegment] = []
    for seg in segments:
        if not isinstance(seg, dict) or "start" not in seg or "end" not in seg:
            continue
        try:
            start = a0 + float(seg["start"])
            end = a0 + float(seg["end"])
        except (TypeError, ValueError):
            continue
        if not math.isfinite(start) or not math.isfinite(end):
            continue
        text = seg.get("text")
        mapped.append(MappedSegment(start=start, end=end, text=text if isinstance(text, str) else ""))
    return tuple(mapped)


def _response_duration(payload: dict[str, Any], audio_s: float) -> float:
    raw = payload.get("duration")
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return audio_s
    if not math.isfinite(float(raw)) or float(raw) < 0:
        return audio_s
    return float(raw)


def _http_reason(status: int) -> str:
    if status == 401:
        return "unauthorized"
    if status == 403:
        return "forbidden"
    if status == 429:
        return "rate_limited"
    if 500 <= status <= 599:
        return "server_error"
    return "http_error"


def _header(headers: Any, name: str) -> str | None:
    if headers is None or not hasattr(headers, "items"):
        return None
    wanted = name.lower()
    for key, value in headers.items():
        if str(key).lower() == wanted and value is not None:
            return str(value)
    return None


def _nonnegative_int(value: str | None) -> int | None:
    if value is None:
        return None
    text = value.strip()
    if not text:
        return None
    try:
        number = int(text)
    except ValueError:
        try:
            parsed = float(text)
        except ValueError:
            return None
        if not math.isfinite(parsed) or not parsed.is_integer():
            return None
        number = int(parsed)
    if number < 0:
        return None
    return number


_RESET_DURATION_TOKEN = re.compile(r"(\d+(?:\.\d+)?)(ms|h|m|s)")


def _reset_requests_seconds(value: str | None) -> float | None:
    """解析 Groq 连续单位时长；未知格式不能被误认为额度恢复时间。"""
    text = value.strip() if value is not None else ""
    if not text:
        return None
    seconds = 0.0
    index = 0
    for match in _RESET_DURATION_TOKEN.finditer(text):
        if match.start() != index:
            return None
        number = float(match.group(1))
        multiplier = {"h": 3600.0, "m": 60.0, "s": 1.0, "ms": 0.001}[match.group(2)]
        seconds += number * multiplier
        if not math.isfinite(seconds):
            return None
        index = match.end()
    return seconds if index == len(text) else None


def _status_code(response: Any) -> int:
    raw = getattr(response, "status_code", 0)
    try:
        return int(raw)
    except (TypeError, ValueError):
        return 0


def _secret_in(value: str, secret: str) -> bool:
    # 短于 8 字符的替换会把普通日志字段擦掉；Groq key 比这长得多。
    return len(secret) >= 8 and secret in value


def _public_id(value: str | None, secret: str) -> str | None:
    if not value:
        return None
    if _secret_in(value, secret) or "Bearer" in value:
        return None
    return value


def _scrub(value: str, secret: str) -> str:
    text = value.replace(secret, "[redacted]") if _secret_in(value, secret) else value
    return text.replace("Bearer", "[redacted]")


def _close_quietly(obj: Any) -> None:
    close = getattr(obj, "close", None)
    if close is None:
        return
    try:
        close()
    except Exception:
        return
