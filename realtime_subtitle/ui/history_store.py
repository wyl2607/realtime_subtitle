"""四个显示面共用的本场字幕历史（spec 003 P3，接口见 .governance/specs/003-subtitle-ui/p3-design.md）。

☠️ 不许 import PyQt6：要能不起 QApplication 单测。只在 Qt 主线程写，不加锁。
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import time

import realtime_subtitle.ui.design_tokens as design_tokens


HISTORY_STORE_MAX = 2000


@dataclass
class HistoryRow:
    key: str
    src: str
    dst: str
    wall: float
    t0: float | None = None
    t1: float | None = None
    source: str = "local"
    revision: int = 0

    @property
    def status(self) -> str:
        if self.dst == design_tokens.TEXT_UNTRANSLATED:
            return "failed"
        if self.dst == design_tokens.TEXT_TRANSLATING:
            return "retrying"
        return "ok"


@dataclass(frozen=True)
class HistoryEvent:
    kind: str
    keys: tuple[str, ...] = ()
    index: int | None = None
    fields: frozenset[str] = frozenset()
    reason: str = ""


def _export_dst(row: HistoryRow) -> str:
    return "" if row.status != "ok" else (row.dst or "").strip()


def rows_to_srt(rows: Iterable[HistoryRow], mode: str = "bilingual") -> str:
    """时间轴规则和 transcript 会话导出同一套：有音频时钟用它，缺的按墙钟估，整体单调不重叠。"""
    if mode not in {"bilingual", "target", "source"}:
        raise ValueError(f"unknown SRT mode: {mode}")
    rows = list(rows)
    if not rows:
        return ""

    # 导出时才 import：别让 UI 启动把 translate 包整条拉起来。
    from realtime_subtitle.translate.transcript import _srt_time, estimate_duration

    first_wall = float(rows[0].wall)
    previous_end = 0.0
    chunks = []
    for number, row in enumerate(rows, 1):
        src = (row.src or "").strip()
        dst = _export_dst(row)
        if mode == "source":
            body = src
        elif mode == "target":
            body = dst or src
        else:
            body = f"{src}\n{dst}" if dst else src

        duration = estimate_duration(src or dst)
        try:
            audio_start, audio_end = float(row.t0), float(row.t1)
        except (TypeError, ValueError):
            audio_start = audio_end = None
        if audio_start is not None and audio_end is not None:
            start = max(previous_end, audio_start)
            end = audio_end if audio_end > start else start + duration
        else:
            try:
                elapsed = max(0.0, float(row.wall) - first_wall)
            except (TypeError, ValueError):
                elapsed = previous_end
            end = elapsed
            start = max(previous_end, end - duration)
            if end <= start:
                end = start + duration
        previous_end = end
        chunks.append(f"{number}\n{_srt_time(start)} --> {_srt_time(end)}\n{body}\n")
    return "\n".join(chunks)


class SubtitleHistoryStore:
    def __init__(self, capacity: int = HISTORY_STORE_MAX):
        self.capacity = max(0, int(capacity))
        self._rows: list[HistoryRow] = []
        self._by_key: dict[str, HistoryRow] = {}
        self._listeners: list[Callable[[HistoryEvent], None]] = []
        self._languages: dict[int, str] = {}
        self._generated = 0

    # ---- read ---------------------------------------------------------
    def rows(self) -> list[HistoryRow]:
        return list(self._rows)

    def get(self, key) -> HistoryRow | None:
        return self._by_key.get(str(key))

    def last(self) -> HistoryRow | None:
        return self._rows[-1] if self._rows else None

    def __len__(self) -> int:
        return len(self._rows)

    def language_label(self, revision: int) -> str:
        return self._languages.get(int(revision), "")

    def search(self, query: str) -> list[HistoryRow]:
        needle = (query or "").strip().casefold()
        if not needle:
            return self.rows()
        return [row for row in self._rows if needle in row.src.casefold() or needle in row.dst.casefold()]

    # ---- write --------------------------------------------------------
    def add(self, src, dst, *, key=None, t0=None, t1=None, source="local", revision=0, wall=None) -> HistoryRow:
        row_key = "" if key is None else str(key)
        row_key = row_key or self._next_key()
        old = self._by_key.get(row_key)
        if old is not None:
            self.update(row_key, src=src, dst=dst)
            return old
        row = HistoryRow(
            row_key, str(src or ""), str(dst or ""),
            time.time() if wall is None else float(wall), t0, t1,
            str(source or "local"), int(revision or 0),
        )
        self._rows.append(row)
        self._by_key[row.key] = row
        self._emit(HistoryEvent("insert", (row.key,), len(self._rows) - 1))
        self._trim()
        return row

    def update(self, key, *, dst=None, src=None) -> bool:
        row = self.get(key)
        if row is None:
            return False
        fields = set()
        if dst is not None and row.dst != str(dst):
            row.dst = str(dst)
            fields.add("dst")
        if src:
            src = str(src)
            if row.src != src:
                row.src = src
                fields.add("src")
        if not fields:
            return False
        self._emit(HistoryEvent("update", (row.key,), fields=frozenset(fields)))
        return True

    def drop(self, keys) -> list[str]:
        wanted = {str(key) for key in (keys or ())}
        deleted = [row.key for row in self._rows if row.key in wanted]
        if not deleted:
            return []
        self._rows = [row for row in self._rows if row.key not in wanted]
        for key in deleted:
            self._by_key.pop(key, None)
        self._emit(HistoryEvent("delete", tuple(deleted), reason="drop"))
        return deleted

    def move_by_t0(self, key, *, source=None) -> bool:
        row = self.get(key)
        if row is None:
            return False
        source_changed = source is not None and row.source != str(source)
        if source_changed:
            row.source = str(source)
        self._rows.remove(row)
        index = len(self._rows)
        # 被挪的行没有 t0 就留在末尾（原 _reinsert_pair_by_t0 的行为）；别把它当 -inf 插到最前
        for candidate_index, candidate in enumerate(self._rows if row.t0 is not None else ()):
            candidate_t0 = candidate.t0 if candidate.t0 is not None else float("-inf")
            try:
                later = candidate_t0 > row.t0
            except TypeError:
                later = False
            if later:
                index = candidate_index
                break
        self._rows.insert(index, row)
        self._emit(HistoryEvent("move", (row.key,), index))
        if source_changed:
            self._emit(HistoryEvent("update", (row.key,), fields=frozenset({"source"})))
        return True

    def clear(self) -> None:
        self._rows.clear()
        self._by_key.clear()
        self._emit(HistoryEvent("reset"))

    def mark_language(self, src, tgt, revision) -> None:
        self._languages[int(revision)] = f"{src}→{tgt}"
        self._emit(HistoryEvent("language"))

    # ---- subscription -------------------------------------------------
    def subscribe(self, listener: Callable[[HistoryEvent], None]) -> None:
        self._listeners.append(listener)

    def unsubscribe(self, listener) -> None:
        try:
            self._listeners.remove(listener)
        except ValueError:
            pass

    # ---- export -------------------------------------------------------
    def to_text(self, rows=None) -> str:
        selected = self.rows() if rows is None else list(rows)
        chunks = []
        for row in selected:
            stamp = time.strftime("%H:%M:%S", time.localtime(row.wall))
            chunks.append(f"[{stamp}] {row.src}\n{row.dst}\n")
        return "\n".join(chunks)

    def to_srt(self, rows=None, mode="bilingual") -> str:
        return rows_to_srt(self.rows() if rows is None else rows, mode)

    def _next_key(self) -> str:
        self._generated += 1
        return f"@n:{self._generated}"

    def _trim(self) -> None:
        if len(self._rows) <= self.capacity:
            return
        removed = self._rows[:len(self._rows) - self.capacity]
        del self._rows[:len(removed)]
        keys = tuple(row.key for row in removed)
        for key in keys:
            self._by_key.pop(key, None)
        self._emit(HistoryEvent("delete", keys, reason="trim"))

    def _emit(self, event: HistoryEvent) -> None:
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as exc:
                # 调用链是 Qt 槽，PyQt6 槽里未捕获的异常会 abort 整个进程（第 27 条）
                print(f"⚠️ SubtitleHistoryStore listener failed: {exc}")
