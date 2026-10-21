from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


BACKOFF_SECONDS = (5, 30, 120, 300)
REPLACE_RATIO = 0.5
EPSILON = 1e-9


@dataclass
class RoutingState:
    has_key: bool = False
    cloud_enabled: bool = True
    cloud_state: str = "available"
    node_enabled: bool = False
    node_healthy: bool = False
    node_is_self: bool = False
    consecutive_failures: int = 0
    backoff_index: int = 0
    cooldown_seconds: int | None = None
    cancel_inflight: bool = False
    discard_response: bool = False
    action: str = "none"
    key_invalid: bool = False

    @classmethod
    def from_fixture(cls, data: dict[str, Any]) -> RoutingState:
        return cls(
            has_key=bool(data.get("has_key", False)),
            cloud_enabled=bool(data.get("cloud_enabled", True)),
            cloud_state=str(data.get("cloud_state", "available")),
            node_enabled=bool(data.get("node_enabled", False)),
            node_healthy=bool(data.get("node_healthy", False)),
            node_is_self=bool(data.get("node_is_self", False)),
            consecutive_failures=int(data.get("consecutive_failures", 0)),
            backoff_index=int(data.get("backoff_index", 0)),
            key_invalid=bool(data.get("key_invalid", data.get("cloud_state") == "disabled_invalid_key")),
        )

    def apply(self, event: dict[str, Any]) -> None:
        self.cooldown_seconds = None
        self.cancel_inflight = False
        self.discard_response = False
        self.action = "none"
        event_type = event["type"]

        if event_type == "noop":
            return
        if event_type == "segment_success":
            self.consecutive_failures = 0
            if self.cloud_state == "probe":
                self.cloud_state = "available"
                self.backoff_index = 0
                self.action = "resume_cloud"
            return
        if event_type == "segment_failure":
            self._fail_segment()
            return
        if event_type == "http_status":
            self._http_status(int(event["status"]), event.get("retry_after"))
            return
        if event_type == "quota_update":
            self._quota_update(float(event["usage_percent"]))
            return
        if event_type == "quota_reset_confirmed":
            # Caller must verify a server-period rollover and recompute *all*
            # quota windows before emitting this event.
            if self.cloud_enabled and self.has_key and not self.key_invalid and self.cloud_state == "cooldown_quota":
                self._quota_update(float(event.get("usage_percent", 100)))
            return
        if event_type == "cooldown_expired":
            if self.cloud_state.startswith("cooldown_"):
                self.cloud_state = "probe"
                self.action = "probe_once"
            return
        if event_type == "switch_off":
            self.cloud_enabled = False
            self._turn_cloud_off()
            return
        if event_type == "switch_on":
            self.cloud_enabled = True
            if self.has_key:
                if self.key_invalid:
                    self.cloud_state = "disabled_invalid_key"
                else:
                    self._resume_cloud()
            return
        if event_type == "key_cleared":
            self.has_key = False
            self._turn_cloud_off()
            return
        if event_type == "key_modified":
            self.key_invalid = False
            self.has_key = True
            self.cloud_enabled = True
            self._resume_cloud()
            return
        raise ValueError(f"unknown route event: {event_type}")

    def tier(self) -> str:
        if self.has_key and self.cloud_enabled and self.cloud_state in {"available", "probe"}:
            return "cloud"
        if self.node_enabled and self.node_healthy and not self.node_is_self:
            return "node"
        return "local"

    def snapshot_for(self, expected: dict[str, Any]) -> dict[str, Any]:
        full = {
            "tier": self.tier(),
            "cloud_state": self.cloud_state,
            "consecutive_failures": self.consecutive_failures,
            "cooldown_seconds": self.cooldown_seconds,
            "backoff_index": self.backoff_index,
            "cancel_inflight": self.cancel_inflight,
            "discard_response": self.discard_response,
            "action": self.action,
            "key_invalid": self.key_invalid,
        }
        return {key: full[key] for key in expected}

    def _fail_segment(self) -> None:
        if self.cloud_state == "probe":
            # 试探失败代表上一轮退避不够，下一档再试。
            self.backoff_index = min(self.backoff_index + 1, len(BACKOFF_SECONDS) - 1)
            self.cloud_state = "cooldown_backoff"
            self.cooldown_seconds = BACKOFF_SECONDS[self.backoff_index]
            self.consecutive_failures = max(self.consecutive_failures, 1)
            self.action = "fallback"
            return
        if self.cloud_state != "available":
            self.action = "fallback"
            return

        self.consecutive_failures += 1
        if self.consecutive_failures < 3:
            self.action = "keep_local"
            return
        self.cloud_state = "cooldown_backoff"
        self.cooldown_seconds = BACKOFF_SECONDS[self.backoff_index]
        self.action = "fallback"

    def _http_status(self, status: int, retry_after: Any) -> None:
        if status in {401, 403}:
            self.key_invalid = True
            self.cloud_state = "disabled_invalid_key"
            self.action = "mark_key_invalid"
            return
        if status == 429:
            if self.key_invalid or self.cloud_state == "disabled_invalid_key":
                # 失效 key 只有 key_modified 能解除。401 之后迟到的 429 是并发在飞请求的
                # 回执，若照常进限流冷却，冷却到期会 probe→tier() 回到 cloud，
                # 把已经失效的 key 又带回云端发请求，所以这里整条忽略。
                return
            self.cloud_state = "cooldown_rate_limited"
            self.cooldown_seconds = int(retry_after) if retry_after is not None else 60
            self.action = "rate_limited"
            return
        if 500 <= status <= 599:
            self._fail_segment()

    def _quota_update(self, usage_percent: float) -> None:
        if not self.cloud_enabled or not self.has_key or self.cloud_state in {"disabled_invalid_key", "off"}:
            return
        if usage_percent >= 90:
            self.cloud_state = "cooldown_quota"
            self.action = "quota_cooldown"
        elif self.cloud_state == "cooldown_quota" and usage_percent < 80:
            self._resume_cloud()
        elif self.cloud_state == "cooldown_quota":
            self.action = "quota_cooldown"

    def _turn_cloud_off(self) -> None:
        # 关开关或清 key 必须立刻取消在飞请求；之后到达的响应都不能再上屏。
        self.cloud_state = "off"
        self.cancel_inflight = True
        self.discard_response = True
        self.action = "cloud_off"

    def _resume_cloud(self) -> None:
        self.cloud_state = "available"
        self.consecutive_failures = 0
        self.backoff_index = 0
        self.action = "resume_cloud"


def replay_route_case(case: dict[str, Any]) -> list[dict[str, Any]]:
    state = RoutingState.from_fixture(case["initial"])
    actual = []
    for step in case["steps"]:
        state.apply(step["event"])
        actual.append(state.snapshot_for(step["expect"]))
    return actual


@dataclass
class P5Line:
    key: int
    t0: float | None
    t1: float | None
    source: str
    src: str
    dst: str | None = None


@dataclass
class P5LineStore:
    lines: list[P5Line] = field(default_factory=list)
    next_remote_key: int = -1

    def add_local(self, key: int, t0: float | None, t1: float | None, src: str, dst: str | None = None) -> None:
        line = P5Line(key=key, t0=t0, t1=t1, source="local", src=src, dst=dst)
        if self._should_replace(line, self._remote_intervals()):
            return
        self._insert(line)

    def apply_remote(
        self,
        node_id: str,
        t0: float | None,
        t1: float | None,
        src: str,
        dst: str | None = None,
    ) -> list[int]:
        del node_id
        line = P5Line(key=self.next_remote_key, t0=t0, t1=t1, source="remote", src=src, dst=dst)
        self.next_remote_key -= 1
        self._insert(line)
        intervals = self._remote_intervals()
        replaced = [line.key for line in self.lines if self._should_replace(line, intervals)]
        replaced_set = set(replaced)
        self.lines = [line for line in self.lines if line.source != "local" or line.key not in replaced_set]
        return replaced

    def serialise_for(self, expected_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if len(self.lines) != len(expected_lines):
            raise AssertionError(f"expected {len(expected_lines)} lines, got {len(self.lines)}")
        return [_filter_line(line, expected) for line, expected in zip(self.lines, expected_lines, strict=True)]

    def visible_for(self, limit: int, expected_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        visible = self.lines[-limit:] if limit > 0 else []
        if len(visible) != len(expected_lines):
            raise AssertionError(f"expected {len(expected_lines)} visible lines, got {len(visible)}")
        return [_filter_line(line, expected) for line, expected in zip(visible, expected_lines, strict=True)]

    def _insert(self, line: P5Line) -> None:
        if line.source == "remote" and line.t0 is not None:
            for index, existing in enumerate(self.lines):
                existing_t0 = existing.t0 if existing.t0 is not None else float("-inf")
                if existing_t0 > line.t0:
                    self.lines.insert(index, line)
                    return
            for index in range(len(self.lines) - 1, -1, -1):
                if self.lines[index].t0 is not None:
                    self.lines.insert(index + 1, line)
                    return
        self.lines.append(line)

    def _remote_intervals(self) -> list[tuple[float, float]]:
        intervals = []
        for line in self.lines:
            if (
                line.source == "remote"
                and line.t0 is not None
                and line.t1 is not None
                and line.t1 >= line.t0
            ):
                intervals.append((line.t0, line.t1))
        return intervals

    def _should_replace(self, line: P5Line, intervals: list[tuple[float, float]]) -> bool:
        if line.source != "local" or line.t0 is None or line.t1 is None:
            return False
        duration = max(line.t1 - line.t0, 0)
        if duration <= EPSILON:
            return any(line.t0 >= start - EPSILON and line.t0 <= end + EPSILON for start, end in intervals)
        return _covered_duration((line.t0, line.t1), intervals) >= duration * REPLACE_RATIO - EPSILON


def replay_p5_case(case: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    store = P5LineStore()
    for op in case["operations"]:
        if op["type"] == "add_local":
            store.add_local(op["key"], op.get("t0"), op.get("t1"), op["src"], op.get("dst"))
        elif op["type"] == "apply_remote":
            replaced = store.apply_remote(op["node_id"], op.get("t0"), op.get("t1"), op["src"], op.get("dst"))
            if replaced != op.get("expect_replaced", replaced):
                raise AssertionError(f"{case['name']}: expected replaced {op['expect_replaced']}, got {replaced}")
        else:
            raise ValueError(f"unknown P5 operation: {op['type']}")
    expected = case["expect"]
    return {
        "lines": store.serialise_for(expected["lines"]),
        "visible": store.visible_for(case.get("visible_limit", len(store.lines)), expected["visible"]),
    }


def _covered_duration(local: tuple[float, float], intervals: list[tuple[float, float]]) -> float:
    clipped = []
    for start, end in intervals:
        clipped_start = max(local[0], start)
        clipped_end = min(local[1], end)
        if clipped_end > clipped_start:
            clipped.append((clipped_start, clipped_end))
    clipped.sort()
    if not clipped:
        return 0.0
    current_start, current_end = clipped[0]
    total = 0.0
    for start, end in clipped[1:]:
        if start <= current_end + EPSILON:
            current_end = max(current_end, end)
        else:
            total += current_end - current_start
            current_start, current_end = start, end
    return total + current_end - current_start


def _filter_line(line: P5Line, expected: dict[str, Any]) -> dict[str, Any]:
    full = {
        "source": line.source,
        "key": line.key,
        "src": line.src,
        "dst": line.dst,
        "t0": line.t0,
        "t1": line.t1,
    }
    return {key: full[key] for key in expected}
