#!/usr/bin/env python3
"""TK-001：向 Groq 打一轮超额请求，留下一份打过码的 429 响应头。

key 只读 ~/.config/rslite/groq.key。任何写出的文本里，key 都被换成 ***。
本脚本不改 groq-429.md。--self-check 不读 key、不访问网络。
"""

from __future__ import annotations

import argparse
import io
import json
import re
import secrets
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import wave
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from pathlib import Path

URL = "https://api.groq.com/openai/v1/audio/transcriptions"
KEY_PATH = Path.home() / ".config" / "rslite" / "groq.key"
N_REQUESTS = 24
N_WORKERS = 12
TIMEOUT_S = 30.0
# 产品超时是 10s。这里要等得及把 429 头收回来，短超时会把限流响应当成连接失败。
AUDIO_S = 1.0
SAMPLE_RATE = 16000
# 本地记账按 max(10, duration)。1 秒音频记 10 秒，24 次大约 240 秒。
BILLED_S_EACH = 10
TOL_S = 2.0
SCALES = (("minute", 60.0), ("hour", 3600.0), ("day", 86400.0))
_UNIT = re.compile(r"(\d+(?:\.\d+)?)(ms|m|s)")


def mask(text: str, secret: str) -> str:
    if secret:
        text = text.replace(secret, "***")
        text = text.replace("Bearer ***", "***")
    return text


def mask_headers(headers: dict[str, str], secret: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for name, value in headers.items():
        if name.lower() == "authorization" or (secret and secret in value):
            out[name] = "***"
        else:
            out[name] = mask(value, secret)
    return out


def parse_reset(value: str | None) -> float | None:
    """把 Groq 的时长（7.5s、2m、120ms、1m30s）收成秒。纯数字当秒。HTTP 日期解析不了。"""
    if value is None:
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        pass
    total = 0.0
    found = False
    for match in _UNIT.finditer(text):
        found = True
        number = float(match.group(1))
        unit = match.group(2)
        if unit == "ms":
            total += number / 1000.0
        elif unit == "m":
            total += number * 60.0
        else:
            total += number
    return total if found else None


def classify_window(now: float, first: float, reset_s: float | None) -> str:
    """对照 reset 剩余时间和墙钟整点、以及本轮第一发请求。

    容差 2 秒。分钟 / 小时 / 天各看一次。两边都贴上就是 ambiguous。
    这是证据，不是窗口语义的裁决。开在整分或整点上时，滚动和整点会重合。
    """
    if reset_s is None or reset_s < 0:
        return "insufficient"
    hits: list[str] = []
    elapsed = now - first
    for name, window in SCALES:
        clock_left = window - (now % window)
        if abs(reset_s - clock_left) <= TOL_S:
            hits.append(f"clock-{name}")
        if 0 <= elapsed <= window and abs(reset_s - (window - elapsed)) <= TOL_S:
            hits.append(f"rolling-{name}")
    kinds = set(hits)
    if not kinds:
        return "insufficient"
    if len(kinds) == 1:
        return next(iter(kinds))
    return "ambiguous"


def slice_wav(path: Path) -> bytes:
    with wave.open(str(path), "rb") as src:
        if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1, 2, SAMPLE_RATE):
            raise RuntimeError(f"{path.name} 需要 16 kHz mono s16")
        need = SAMPLE_RATE
        raw = src.readframes(need)
    if len(raw) < need * 2:
        raw = raw + (b"\x00" * (need * 2 - len(raw)))
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(SAMPLE_RATE)
        dst.writeframes(raw[: need * 2])
    return buf.getvalue()


def build_multipart(wav: bytes, boundary: str) -> bytes:
    def field(name: str, value: str) -> bytes:
        return (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n"
        ).encode()

    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="one-second.wav"\r\n'
        "Content-Type: audio/wav\r\n\r\n"
    ).encode()
    return b"".join(
        [
            head,
            wav,
            b"\r\n",
            field("model", "whisper-large-v3"),
            field("language", "de"),
            field("response_format", "verbose_json"),
            f"--{boundary}--\r\n".encode(),
        ]
    )


def form_fields(body: bytes, boundary: str) -> dict[str, bytes]:
    marker = f"--{boundary}".encode()
    out: dict[str, bytes] = {}
    for chunk in body.split(marker):
        if chunk.startswith(b"--") or chunk in (b"", b"\r\n", b"--\r\n"):
            continue
        if chunk.startswith(b"\r\n"):
            chunk = chunk[2:]
        head, sep, data = chunk.partition(b"\r\n\r\n")
        if not sep:
            continue
        if data.endswith(b"\r\n"):
            data = data[:-2]
        header = head.decode("utf-8", "replace")
        name = ""
        for part in header.split(";"):
            part = part.strip()
            if part.startswith("name="):
                name = part.split("=", 1)[1].strip().strip('"')
        if name:
            out[name] = data
    return out


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ARG002
        # 重定向会把 Authorization 带到别的主机。探针只收 api.groq.com 的直接响应。
        return None


def post_once(wav: bytes, secret: str) -> tuple[int, dict[str, str], str]:
    boundary = "rsprobe" + secrets.token_hex(16)
    body = build_multipart(wav, boundary)
    request = urllib.request.Request(
        URL,
        data=body,
        method="POST",
        headers={
            "Authorization": "Bearer " + secret,
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "rslite-probe-429",
        },
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            raw = response.read(4096)
            headers = {key: value for key, value in response.headers.items()}
            return response.status, headers, raw.decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        raw = exc.read(4096)
        headers = {key: value for key, value in exc.headers.items()} if exc.headers else {}
        return exc.code, headers, raw.decode("utf-8", "replace")


def _reset_rows(headers: dict[str, str], now: float, first: float) -> list[dict[str, object]]:
    rows = []
    for name, value in headers.items():
        lowered = name.lower()
        if lowered != "retry-after" and "reset" not in lowered:
            continue
        seconds = parse_reset(value)
        rows.append(
            {
                "header": name,
                "raw": value,
                "seconds": seconds,
                "guess": classify_window(now, first, seconds),
            }
        )
    return rows


def _pick(headers: dict[str, str], needle: str) -> str | None:
    for name, value in headers.items():
        if name.lower() == needle:
            return value
    return None


def build_report(
    secret: str,
    captured: dict[str, object] | None,
    sent: int,
    counts: dict[str, int],
    first_unix: float | None,
    stopped_on_401: bool,
) -> str:
    headers = mask_headers(captured["headers"], secret) if captured else {}
    body = mask(captured["body"], secret)[:500] if captured else ""
    now = float(captured["now"]) if captured else None
    guesses = _reset_rows(headers, now, first_unix) if captured and now is not None and first_unix is not None else []
    request_guess = "insufficient"
    for row in guesses:
        header = str(row["header"]).lower()
        if header == "x-ratelimit-reset-requests":
            request_guess = str(row["guess"])
            break
    else:
        for row in guesses:
            if str(row["header"]).lower() == "retry-after":
                request_guess = str(row["guess"])
                break
    ratelimit = {name: value for name, value in headers.items() if name.lower().startswith("x-ratelimit-")}
    audio_names = [
        name
        for name in headers
        if "audio" in name.lower() or "second" in name.lower()
    ]
    report: dict[str, object] = {
        "captured": captured is not None and int(captured["status"]) == 429,
        "http_status": None if captured is None else int(captured["status"]),
        "headers": headers,
        "body_redacted": body,
        "has_retry_after": _pick(headers, "retry-after") is not None,
        "retry_after": _pick(headers, "retry-after"),
        "x_ratelimit": ratelimit,
        "audio_header_names": audio_names,
        "reset_guesses": guesses,
        "request_window_guess": request_guess,
        "timing": {
            "first_request_unix": first_unix,
            "response_unix": now,
            "elapsed_s": None if now is None or first_unix is None else round(now - first_unix, 3),
            "unix_mod_60": None if now is None else round(now % 60.0, 3),
            "unix_mod_3600": None if now is None else round(now % 3600.0, 3),
        },
        "requests": {
            "sent": sent,
            "status_counts": {str(key): value for key, value in sorted(counts.items())},
            "stopped_on_401": stopped_on_401,
        },
        "audio_s": AUDIO_S,
        "billed_s_each_local": BILLED_S_EACH,
        "billed_s_if_all_sent": BILLED_S_EACH * N_REQUESTS,
        "note": (
            "request_window_guess 只解释请求次数那条 reset（先看 x-ratelimit-reset-requests，"
            "没有再看 retry-after）。音频秒窗口看 audio_header_names / 名字里带 reset 的另一条，"
            "不要把请求次数的猜测抄过去。启发式容差 2 秒，不是裁决。"
            "这一轮打满的是每分钟请求数，不是每小时音频秒。"
        ),
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if secret and secret in text:
        raise RuntimeError("打码失败，没有写出文件")
    return text


def load_key() -> str:
    if not KEY_PATH.is_file():
        raise RuntimeError(f"找不到 key 文件 {KEY_PATH}")
    secret = KEY_PATH.read_text(encoding="utf-8").strip()
    if not secret:
        raise RuntimeError(f"key 文件是空的 {KEY_PATH}")
    return secret


def run_self_check() -> int:
    import urllib.request as request

    def boom(*_args, **_kwargs):
        raise AssertionError("self-check 不该访问网络")

    request.urlopen = boom
    request.build_opener = boom
    failures: list[str] = []
    secret = "probe-fake-key-not-a-secret-0123456789abcdef"

    samples = {
        "2s": 2.0,
        "120ms": 0.12,
        "1m30s": 90.0,
        "7.5s": 7.5,
        "1m2.5s": 62.5,
        "2": 2.0,
    }
    for raw, want in samples.items():
        got = parse_reset(raw)
        if got is None or abs(got - want) > 1e-9:
            failures.append(f"parse:{raw}")
    if parse_reset("Tue, 15 Nov 1994 08:12:31 GMT") is not None:
        failures.append("parse:http-date")

    # 1000 % 60 = 40，整分还剩 20 秒。刚发出时滚动窗口还剩 60 秒。
    if classify_window(1000.0, 1000.0, 20.0) != "clock-minute":
        failures.append("window:clock-minute")
    if classify_window(1000.0, 1000.0, 60.0) != "rolling-minute":
        failures.append("window:rolling-minute")
    # 第一发在 960，现在 1000，两边都剩 20 秒。
    if classify_window(1000.0, 960.0, 20.0) != "ambiguous":
        failures.append("window:ambiguous")
    if classify_window(1000.0, 1000.0, 5.0) != "insufficient":
        failures.append("window:insufficient")
    if classify_window(1000.0, 1000.0, None) != "insufficient":
        failures.append("window:none")
    # 1000 % 3600 = 1000，整点小时还剩 2600 秒。
    if classify_window(1000.0, 1000.0, 2600.0) != "clock-hour":
        failures.append("window:clock-hour")

    blob = mask(f"Authorization: Bearer {secret}\nsee {secret}", secret)
    if secret in blob or "Bearer " in blob:
        failures.append("mask")
    headers = mask_headers(
        {"Authorization": f"Bearer {secret}", "x-ratelimit-remaining-requests": "0", "X-Note": secret},
        secret,
    )
    if secret in json.dumps(headers):
        failures.append("mask-headers")
    if headers["Authorization"] != "***":
        failures.append("mask-authorization")

    wav = synthesize_wav(SAMPLE_RATE)
    boundary = "rsprobe-self-check"
    body = build_multipart(wav, boundary)
    fields = form_fields(body, boundary)
    if fields.get("model") != b"whisper-large-v3":
        failures.append("form:model")
    if fields.get("language") != b"de":
        failures.append("form:language")
    if fields.get("response_format") != b"verbose_json":
        failures.append("form:format")
    if not fields.get("file", b"").startswith(b"RIFF"):
        failures.append("form:wav")
    if secret.encode() in body:
        failures.append("form:secret")

    with tempfile.TemporaryDirectory(dir="/tmp") as folder:
        short_path = Path(folder) / "short.wav"
        short_path.write_bytes(synthesize_wav(100))
        padded = slice_wav(short_path)
    with wave.open(io.BytesIO(padded), "rb") as check:
        if check.getnframes() != SAMPLE_RATE or check.getframerate() != SAMPLE_RATE:
            failures.append("slice:pad")

    # 1012.5 % 60 = 52.5，整分还剩 7.5 秒。滚动窗口在刚发出时还剩 60 秒，所以 7.5s 只贴整分。
    text = build_report(
        secret,
        {
            "status": 429,
            "headers": {
                "Authorization": f"Bearer {secret}",
                "retry-after": "7.5s",
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "7.5s",
            },
            "body": f"limited {secret}",
            "now": 1012.5,
        },
        N_REQUESTS,
        {"429": 1},
        1012.5,
        False,
    )
    if secret in text:
        failures.append("report-leaks")
    report = json.loads(text)
    if report["request_window_guess"] != "clock-minute":
        failures.append("report-guess")
    if report["headers"]["Authorization"] != "***":
        failures.append("report-auth")

    src = Path(__file__).read_text(encoding="utf-8")
    # 拼出来再搜，避免检查语句自己带上那两个名字。
    if ("os." + "environ") in src or ("get" + "env") in src:
        failures.append("source-reads-environ")

    if failures:
        print("groq_429_probe self-check failed:", ", ".join(failures), file=sys.stderr)
        return 1
    print("groq_429_probe self-check: ok")
    return 0


def synthesize_wav(frames: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as dst:
        dst.setnchannels(1)
        dst.setsampwidth(2)
        dst.setframerate(SAMPLE_RATE)
        dst.writeframes(b"\x00\x00" * frames)
    return buf.getvalue()


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓一份打码后的 Groq 429 响应头")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    if not args.self_check and args.wav is None:
        parser.error("真跑需要 --wav（1 秒切片从这里切）")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.self_check:
        return run_self_check()
    secret = ""
    try:
        secret = load_key()
        return run_burst_with_secret(args.wav, args.out, secret)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {mask(str(exc), secret)}", file=sys.stderr)
        return 1


def run_burst_with_secret(wav_path: Path, out_path: Path | None, secret: str) -> int:
    # load_key 已经在 main 里做过。这里不再读文件，避免异常路径上重复打开。
    wav = slice_wav(wav_path)
    return _burst(wav, out_path, secret)


def _burst(wav: bytes, out_path: Path | None, secret: str) -> int:
    stop = threading.Event()
    lock = threading.Lock()
    counts: dict[str, int] = {}
    captured: dict[str, object] | None = None
    first_unix: list[float] = []
    sent = 0

    def one() -> tuple[int, dict[str, str], str, float]:
        if stop.is_set():
            return 0, {}, "", time.time()
        started = time.time()
        with lock:
            if not first_unix:
                first_unix.append(started)
        try:
            status, headers, body = post_once(wav, secret)
        except Exception as exc:
            # 单次超时不能把已经到手的 429 弄丢。异常文本里如果夹了 key，先打码。
            return -1, {}, mask(f"{type(exc).__name__}: {exc}", secret), time.time()
        return status, headers, body, time.time()

    def take(done_set) -> None:
        nonlocal captured
        for future in done_set:
            status, headers, body, done_at = future.result()
            if status == 0:
                continue
            with lock:
                counts[str(status)] = counts.get(str(status), 0) + 1
                if status == 401:
                    stop.set()
                if status == 429 and captured is None:
                    captured = {"status": status, "headers": headers, "body": body, "now": done_at}

    with ThreadPoolExecutor(max_workers=N_WORKERS) as pool:
        pending: set = set()
        while sent < N_REQUESTS and not stop.is_set():
            while len(pending) < N_WORKERS and sent < N_REQUESTS and not stop.is_set():
                pending.add(pool.submit(one))
                sent += 1
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            take(done)
        if pending:
            done, _ = wait(pending)
            take(done)

    text = build_report(
        secret,
        captured,
        sent,
        counts,
        first_unix[0] if first_unix else None,
        stop.is_set(),
    )
    if out_path is not None:
        out_path.write_text(text + "\n", encoding="utf-8")
    else:
        sys.stdout.write(text + "\n")
    status = None if captured is None else int(captured["status"])
    names = ", ".join(
        name for name in (captured["headers"] if captured else {}) if str(name).lower().startswith("x-ratelimit-")
    )
    has_retry = bool(captured) and any(str(key).lower() == "retry-after" for key in captured["headers"])
    print(
        f"429: {'有' if status == 429 else '无'}  retry-after: {'有' if has_retry else '无'}  "
        f"x-ratelimit: {names or '无'}  sent={sent} counts={counts}",
        file=sys.stderr,
    )
    if stop.is_set():
        print("出现 401，key 被拒。响应里的 key 已打码。先核对 key 文件，再决定要不要采用样本。", file=sys.stderr)
        return 1
    if status == 429:
        return 0
    print("这一轮没有 429。不要把空样本写成结论。", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
