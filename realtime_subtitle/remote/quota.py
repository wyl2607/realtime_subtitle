"""Groq 免费档本地记账。

四个窗口：1 分钟请求数、1 小时音频秒、24 小时音频秒，全是本地滚动；
24 小时请求数只在拿到 x-ratelimit-remaining-requests 时改写成
2000 - remaining（这是每日令牌桶，不能拿去改 1 分钟窗口）。
任一窗口 ≥90% 就向 routing_rules 发 quota_update，<80% 由那台状态机恢复。
这里不自己记云端状态。
"""
from __future__ import annotations

import json
import math
import os
import tempfile
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from realtime_subtitle.remote.routing_rules import RoutingState

LIMIT_REQUESTS_PER_MINUTE = 20
LIMIT_AUDIO_PER_HOUR_S = 7200
LIMIT_REQUESTS_PER_DAY = 2000
LIMIT_AUDIO_PER_DAY_S = 28800
WINDOW_MINUTE_S = 60.0
WINDOW_HOUR_S = 3600.0
WINDOW_DAY_S = 86400.0
MIN_BILLED_S = 10.0
ENTER_PERCENT = 90.0
SERVER_DECREASE_GRACE_S = 60.0
MAX_RESET_REQUESTS_S = WINDOW_DAY_S + SERVER_DECREASE_GRACE_S
_VERSION = 1


@dataclass(frozen=True)
class QuotaUsage:
    minute_requests: int
    hour_audio_s: float
    day_audio_s: float
    day_requests: int
    usage_percent: float
    saturated: bool


class _Event:
    __slots__ = ("ts", "billed_s", "in_server_day")

    def __init__(self, ts: float, billed_s: float, in_server_day: bool):
        self.ts = ts
        self.billed_s = billed_s
        self.in_server_day = in_server_day


@dataclass
class _ResetRecord:
    """一次同时带齐 RPD remaining/reset 的服务端周期观测。"""

    reset_at: float
    observed_at: float
    observed_mono: float | None
    key_fp: str
    consumed: bool = False


class QuotaLedger:
    """事件日志 + 原子替换。

    损坏的文件隔离成 `<name>.corrupt`，并写一条带时间戳的日请求封顶（2000）。
    这等价于 24h 窗口里已经用满，usage=100%。满 24h 后随 prune 失效，用量回到真实事件。
    没有无时间戳的永久饱和标志。服务端用量立即上抬；较小值等旧样本过宽限期，
    以免迟到响应过早解除冷却。
    """

    def __init__(
        self,
        path: Path,
        *,
        routing: RoutingState | None = None,
        now: Callable[[], float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ):
        self.path = Path(path)
        self.routing = routing if routing is not None else RoutingState()
        self._now = now
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.Lock()
        self._events: list[_Event] = []
        self._server_day_used: int | None = None
        self._server_day_used_at: float | None = None
        self._reset_record: _ResetRecord | None = None
        # 自己记得「已经把额度打进冷却」，恢复事件才发得出去。
        # 不读状态机的状态名：那串字只活在 routing_rules。
        self._quota_hot = False
        self.load()

    def note(
        self,
        duration_s: float,
        *,
        now: float | None = None,
        remaining_requests: int | None = None,
        reset_requests_s: float | None = None,
        key_fp: str | None = None,
        sent_at: float | None = None,
    ) -> QuotaUsage:
        """记一次已发出的请求。duration_s 是响应里的 duration，没有响应就用送出的音频秒。"""
        billed = _billed(duration_s)
        if remaining_requests is not None and (
            isinstance(remaining_requests, bool) or not isinstance(remaining_requests, int)
        ):
            raise TypeError("remaining_requests")
        if reset_requests_s is not None:
            if isinstance(reset_requests_s, bool) or not isinstance(reset_requests_s, (int, float)):
                raise TypeError("reset_requests_s")
            if not math.isfinite(float(reset_requests_s)) or float(reset_requests_s) < 0:
                raise ValueError("reset_requests_s")
        if key_fp is not None and (not isinstance(key_fp, str) or not key_fp):
            raise TypeError("key_fp")
        if sent_at is not None:
            if isinstance(sent_at, bool) or not isinstance(sent_at, (int, float)):
                raise TypeError("sent_at")
            if not math.isfinite(float(sent_at)):
                raise ValueError("sent_at")
        with self._lock:
            ts = self._clock(now)
            self._events.append(_Event(ts, billed, False))
            record = self._reset_record
            if record is not None and key_fp != record.key_fp:
                # 换 key 不能继承旧 key 的恢复承诺，但高水位继续 fail-closed。
                # 必须在 late_old_cycle 判定之前，否则旧 key 过期 + 新 key 同时带两头且 sent_at=None
                # 会被误判为 late_old_cycle 而丢掉新周期的恢复记录。
                self._reset_record = None
                record = None
            late_old_cycle = bool(
                record is not None
                and ts >= record.reset_at
                and (sent_at is None or float(sent_at) < record.reset_at)
            )
            # 先消耗已可信的旧周期；当前周期的剩余头随后可建立新的日下限。
            reset_confirmed = self._advance_reset(ts)
            valid_reset = (
                reset_requests_s is not None
                and float(reset_requests_s) <= MAX_RESET_REQUESTS_S
            )
            valid_remaining = (
                remaining_requests is not None
                and isinstance(remaining_requests, int)
                and not isinstance(remaining_requests, bool)
                and 0 <= remaining_requests <= LIMIT_REQUESTS_PER_DAY
            )
            if not late_old_cycle and valid_remaining:
                # 响应头是这一次请求之后的每日剩余。只动 24h 请求窗口。
                used = LIMIT_REQUESTS_PER_DAY - remaining_requests
                if used < 0:
                    used = 0
                self._raise_day_request_floor(used, ts)
                for event in self._events:
                    event.in_server_day = True
            if not late_old_cycle and valid_remaining and valid_reset and key_fp is not None:
                self._reset_record = _ResetRecord(
                    reset_at=ts + float(reset_requests_s),
                    observed_at=ts,
                    observed_mono=self._monotonic_now(),
                    key_fp=key_fp,
                )
            self._prune(ts)
            usage = self._measure(ts)
            self._publish(usage, reset_confirmed=reset_confirmed)
            self.save()
            return usage

    def usage(self, now: float | None = None) -> QuotaUsage:
        """按给定时刻重算窗口。冷却期间靠它发现用量已经降到 80% 以下，不必再打一次请求。"""
        with self._lock:
            ts = self._clock(now)
            before = self._markers()
            reset_confirmed = self._advance_reset(ts)
            self._prune(ts)
            usage = self._measure(ts)
            self._publish(usage, reset_confirmed=reset_confirmed)
            if self._markers() != before:
                self.save()
            return usage

    def load(self) -> None:
        with self._lock:
            self._load_locked()

    def save(self) -> None:
        """调用方已持有锁，或这是 load 失败路径上的唯一写入。"""
        _atomic_write(self.path, _encode(self._snapshot()))

    def _load_locked(self) -> None:
        if not self.path.exists():
            self._publish(self._measure(self._clock(None)))
            return
        try:
            data = json.loads(_normalize_nonfinite(self.path.read_bytes()))
            legacy_saturated = self._apply_payload(data)
        except Exception:
            self._quarantine_and_cap()
            return
        now = self._clock(None)
        before = self._markers()
        reset_confirmed = self._advance_reset(now)
        self._prune(now)
        if legacy_saturated:
            # 旧文件只有无时间戳的 saturated。迁成从这次读取起算的 24h 封顶，然后把标志抹掉。
            self._raise_day_request_floor(LIMIT_REQUESTS_PER_DAY, now)
        self._publish(self._measure(now), reset_confirmed=reset_confirmed)
        if legacy_saturated or self._markers() != before:
            self.save()

    def _apply_payload(self, data: Any) -> bool:
        """应用一份账本。返回 True 表示带了旧的永久 saturated 哨兵，需要迁成带时间戳的封顶。"""
        if not isinstance(data, dict) or data.get("version") != _VERSION:
            raise ValueError("schema")
        events = data.get("events")
        if not isinstance(events, list):
            raise ValueError("events")
        parsed: list[_Event] = []
        dropped = False
        for item in events:
            event = _parse_event(item)
            if event is None:
                dropped = True
                continue
            parsed.append(event)
        # 全是坏数字时不能当成空账本：那会把写坏的文件读成 0。
        if dropped and not parsed:
            raise ValueError("event")
        used = data.get("server_day_used", None)
        used_at = data.get("server_day_used_at", None)
        if used is None and used_at is None:
            server_used = None
            server_at = None
        else:
            server_used, server_at = _parse_server(used, used_at)
        reset_record = _parse_reset_record(data.get("reset_record"))
        legacy_saturated = False
        if "saturated" in data:
            saturated = data["saturated"]
            if not isinstance(saturated, bool):
                raise ValueError("saturated")
            legacy_saturated = saturated
        self._events = parsed
        self._server_day_used = server_used
        self._server_day_used_at = server_at
        self._reset_record = reset_record
        return legacy_saturated

    def _quarantine_and_cap(self) -> None:
        # 解析不了就不能假设没用过。原文件改名为 .corrupt（已存在则覆盖），
        # 再写一条「现在起 24h 内已用满 2000 次」的封顶。过窗后 prune 会把它丢掉。
        moved = _quarantine(self.path)
        now = self._clock(None)
        self._events = []
        self._server_day_used = LIMIT_REQUESTS_PER_DAY
        self._server_day_used_at = now
        self._reset_record = None
        self._publish(self._measure(now))
        if moved:
            self.save()

    def _raise_day_request_floor(self, used: int, ts: float) -> None:
        current = self._server_day_used
        if self._server_active(ts) and current is not None:
            if used == current:
                return
            # 较小值须等 60 秒：迟到乱序响应不该把冷却提前洗掉。
            if used < current and ts - (self._server_day_used_at or ts) < SERVER_DECREASE_GRACE_S:
                return
        self._server_day_used = used
        self._server_day_used_at = ts

    def _publish(self, usage: QuotaUsage, *, reset_confirmed: bool = False) -> None:
        # 没到阈值就别发事件：apply() 每次都会清掉 cooldown_seconds，
        # 空发会把 429 的 retry-after 擦掉。只有本账本已经把额度打进冷却之后，
        # 才继续发 quota_update，让状态机在降到 80% 以下时恢复。
        previously_hot = self._quota_hot
        hot = usage.usage_percent >= ENTER_PERCENT
        if hot:
            self._quota_hot = True
        if reset_confirmed and previously_hot:
            self.routing.apply({"type": "quota_reset_confirmed", "usage_percent": usage.usage_percent})
        elif hot or self._quota_hot:
            self.routing.apply({"type": "quota_update", "usage_percent": usage.usage_percent})
        if usage.usage_percent < 80:
            self._quota_hot = False

    def _measure(self, now: float) -> QuotaUsage:
        minute = self._count(now, WINDOW_MINUTE_S)
        hour_audio = self._audio(now, WINDOW_HOUR_S)
        day_audio = self._audio(now, WINDOW_DAY_S)
        day_requests = self._day_requests(now)
        percent = _percent(minute, LIMIT_REQUESTS_PER_MINUTE)
        percent = max(percent, _percent(hour_audio, LIMIT_AUDIO_PER_HOUR_S))
        percent = max(percent, _percent(day_audio, LIMIT_AUDIO_PER_DAY_S))
        percent = max(percent, _percent(day_requests, LIMIT_REQUESTS_PER_DAY))
        capped = self._server_active(now) and (self._server_day_used or 0) >= LIMIT_REQUESTS_PER_DAY
        return QuotaUsage(
            minute_requests=minute,
            hour_audio_s=hour_audio,
            day_audio_s=day_audio,
            day_requests=day_requests,
            usage_percent=percent,
            saturated=capped,
        )

    def _day_requests(self, now: float) -> int:
        record = self._reset_record
        if (
            record is not None
            and record.consumed
            and (self._server_day_used_at is None or self._server_day_used_at <= record.reset_at)
        ):
            return sum(
                1
                for event in self._events
                if event.ts > record.reset_at and self._in_window(event.ts, now, WINDOW_DAY_S)
            )
        local = self._count(now, WINDOW_DAY_S)
        if not self._server_active(now):
            return local
        extra = 0
        for event in self._events:
            if self._in_window(event.ts, now, WINDOW_DAY_S) and not event.in_server_day:
                extra += 1
        return int(self._server_day_used or 0) + extra

    def _count(self, now: float, window: float) -> int:
        return sum(1 for event in self._events if self._in_window(event.ts, now, window))

    def _audio(self, now: float, window: float) -> float:
        total = 0.0
        for event in self._events:
            if self._in_window(event.ts, now, window):
                total += event.billed_s
        return total

    def _server_active(self, now: float) -> bool:
        if self._server_day_used is None or self._server_day_used_at is None:
            return False
        record = self._reset_record
        if record is not None and record.consumed and self._server_day_used_at <= record.reset_at:
            return False
        # 墙钟回拨时不能把已经观测到的服务端下限当成过期；等墙钟追上再按窗口老化。
        if now < self._server_day_used_at:
            return True
        return self._in_window(self._server_day_used_at, now, WINDOW_DAY_S)

    def _prune(self, now: float) -> None:
        self._events = [event for event in self._events if now - event.ts <= WINDOW_DAY_S]
        if self._server_day_used_at is not None and now - self._server_day_used_at > WINDOW_DAY_S:
            self._server_day_used = None
            self._server_day_used_at = None

    def _in_window(self, ts: float, now: float, window: float) -> bool:
        return now - window <= ts <= now

    def _snapshot(self) -> dict[str, Any]:
        return {
            "version": _VERSION,
            "events": [
                {"ts": event.ts, "billed_s": event.billed_s, "in_server_day": event.in_server_day}
                for event in self._events
            ],
            "server_day_used": self._server_day_used,
            "server_day_used_at": self._server_day_used_at,
            "reset_record": _encode_reset_record(self._reset_record),
        }

    def _markers(self) -> tuple[int, int | None, float | None, tuple[float, float, str, bool] | None]:
        record = self._reset_record
        record_marker = None if record is None else (
            record.reset_at, record.observed_at, record.key_fp, record.consumed)
        return (len(self._events), self._server_day_used, self._server_day_used_at, record_marker)

    def _clock(self, now: float | None) -> float:
        if now is not None:
            return float(now)
        if self._now is not None:
            return float(self._now())
        return time.time()

    def _monotonic_now(self) -> float:
        value = self._monotonic()
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            raise ValueError("monotonic")
        return float(value)

    def _advance_reset(self, now: float) -> bool:
        """在双时钟均可信时消耗恢复记录，并仅清服务端的日请求下限。"""
        record = self._reset_record
        if record is None:
            return False
        if now < record.observed_at - 5.0:
            self._reset_record = None
            return False
        if record.consumed or now < record.reset_at:
            return False
        if record.observed_mono is not None:
            elapsed = self._monotonic_now() - record.observed_mono
            if elapsed < record.reset_at - record.observed_at - 1.0:
                return False
        record.consumed = True
        self._server_day_used = None
        self._server_day_used_at = None
        return True


def _billed(duration_s: float) -> float:
    if isinstance(duration_s, bool) or not isinstance(duration_s, (int, float)):
        raise TypeError("duration_s")
    if not math.isfinite(float(duration_s)) or float(duration_s) < 0:
        raise ValueError("duration_s")
    return max(MIN_BILLED_S, float(duration_s))


def _percent(value: float, limit: float) -> float:
    return (value / limit) * 100.0


def _parse_event(item: Any) -> _Event | None:
    """结构坏了就抛，让整份账本隔离。负数 / NaN / inf 只丢掉这一条。"""
    if not isinstance(item, dict):
        raise ValueError("event")
    if "ts" not in item or "billed_s" not in item:
        raise ValueError("event")
    ts = item.get("ts")
    billed = item.get("billed_s")
    flag = item.get("in_server_day", False)
    if not isinstance(flag, bool):
        raise ValueError("event")
    if isinstance(ts, bool) or (ts is not None and not isinstance(ts, (int, float))):
        raise ValueError("event")
    if isinstance(billed, bool) or (billed is not None and not isinstance(billed, (int, float))):
        raise ValueError("event")
    if ts is None or not math.isfinite(float(ts)):
        return None
    if billed is None or not math.isfinite(float(billed)) or float(billed) < 0:
        return None
    return _Event(float(ts), float(billed), flag)


def _parse_server(used: Any, used_at: Any) -> tuple[int, float]:
    if isinstance(used, bool) or not isinstance(used, int) or used < 0:
        raise ValueError("server")
    if isinstance(used_at, bool) or not isinstance(used_at, (int, float)) or not math.isfinite(float(used_at)):
        raise ValueError("server")
    return used, float(used_at)


def _parse_reset_record(value: Any) -> _ResetRecord | None:
    """恢复记录损坏只丢记录，不把整本 fail-closed 账本误隔离。"""
    if not isinstance(value, dict):
        return None
    reset_at = value.get("reset_at")
    observed_at = value.get("observed_at")
    key_fp = value.get("key_fp")
    consumed = value.get("consumed")
    if (
        isinstance(reset_at, bool)
        or not isinstance(reset_at, (int, float))
        or not math.isfinite(float(reset_at))
        or isinstance(observed_at, bool)
        or not isinstance(observed_at, (int, float))
        or not math.isfinite(float(observed_at))
        or not isinstance(key_fp, str)
        or not key_fp
        or not isinstance(consumed, bool)
    ):
        return None
    return _ResetRecord(
        reset_at=float(reset_at),
        observed_at=float(observed_at),
        observed_mono=None,
        key_fp=key_fp,
        consumed=consumed,
    )


def _encode_reset_record(record: _ResetRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "reset_at": record.reset_at,
        "observed_at": record.observed_at,
        "key_fp": record.key_fp,
        "consumed": record.consumed,
    }


_NONFINITE_TOKENS = (b"-Infinity", b"+Infinity", b"Infinity", b"NaN")


def _normalize_nonfinite(payload: bytes) -> bytes:
    """JSON 不接受 NaN / Infinity。换成会解码为 inf 的合法数字，由字段解析决定丢弃或隔离。"""
    out = bytearray()
    in_string = False
    escaped = False
    index = 0
    limit = len(payload)
    while index < limit:
        byte = payload[index]
        if in_string:
            out.append(byte)
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
            index += 1
            continue
        if byte == 0x22:
            in_string = True
            out.append(byte)
            index += 1
            continue
        previous = None if index == 0 else payload[index - 1]
        if _is_json_boundary(previous):
            replaced = False
            for token in _NONFINITE_TOKENS:
                end = index + len(token)
                if payload.startswith(token, index) and (end == limit or _is_json_boundary(payload[end])):
                    out.extend(b"1e9999")
                    index = end
                    replaced = True
                    break
            if replaced:
                continue
        out.append(byte)
        index += 1
    return bytes(out)


def _is_json_boundary(byte: int | None) -> bool:
    if byte is None:
        return True
    return byte in b" \n\r\t,:[]{}"


def _quarantine(path: Path) -> bool:
    """原文件改名为 `<name>.corrupt`。目标已存在时 os.replace 覆盖。失败则不动原文件。"""
    corrupt = path.with_name(path.name + ".corrupt")
    try:
        os.replace(path, corrupt)
    except OSError:
        return False
    return True


def _encode(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")


def _atomic_write(path: Path, payload: bytes) -> None:
    """同目录临时文件 + fsync + os.replace。替换没发生时原来的账本还在。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".quota-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_name, path)
    except Exception:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
