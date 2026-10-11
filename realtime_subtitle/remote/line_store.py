from __future__ import annotations

from typing import Any

from realtime_subtitle.remote.routing_rules import P5Line, P5LineStore, replay_p5_case


class LineStore:
    """
    包装 routing_rules.py 的 P5LineStore 为 UI 侧可用的字幕历史存储类。
    直接复用 P5 规则实现（并集覆盖率 >= 50% 替换），不另写一套。
    """

    def __init__(self, capacity: int = 200) -> None:
        self._store = P5LineStore()
        self.capacity = capacity

    @property
    def lines(self) -> list[P5Line]:
        return self._store.lines

    def add_local(
        self,
        key: int,
        t0: float | None = None,
        t1: float | None = None,
        src: str = "",
        dst: str | None = None,
    ) -> None:
        """添加本机行。若已被远端行覆盖（并集覆盖率 >= 50%），则直接丢弃。"""
        self._store.add_local(key=key, t0=t0, t1=t1, src=src, dst=dst)
        self._trim_capacity()

    def set_translation(self, key: int, dst: str) -> None:
        """更新指定 key 对应的译文。"""
        for line in self._store.lines:
            if line.key == key:
                line.dst = dst
                return

    def apply_remote(
        self,
        node_id: str,
        t0: float | None,
        t1: float | None,
        src: str,
        dst: str | None = None,
    ) -> list[int]:
        """应用远端精修行，替换覆盖的本机行，返回被替换的 key 列表。"""
        replaced = self._store.apply_remote(node_id=node_id, t0=t0, t1=t1, src=src, dst=dst)
        self._trim_capacity()
        return replaced

    def apply_node(
        self,
        node_id: str,
        t0: float | None,
        t1: float | None,
        src: str,
        dst: str | None = None,
    ) -> list[int]:
        """apply_remote 的别名，与 Swift LineStore.applyNode 对齐。"""
        return self.apply_remote(node_id=node_id, t0=t0, t1=t1, src=src, dst=dst)

    def visible_lines(self, limit: int = 2) -> list[P5Line]:
        """返回末尾可见的行（默认 2 行）。"""
        if limit <= 0:
            return []
        return self._store.lines[-limit:]

    def clear(self) -> None:
        """清空字幕历史。"""
        self._store.lines.clear()
        self._store.next_remote_key = -1

    def serialise_for(self, expected_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._store.serialise_for(expected_lines)

    def visible_for(self, limit: int, expected_lines: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return self._store.visible_for(limit, expected_lines)

    def _trim_capacity(self) -> None:
        if len(self._store.lines) > self.capacity:
            excess = len(self._store.lines) - self.capacity
            del self._store.lines[:excess]


__all__ = ["LineStore", "P5Line", "replay_p5_case"]
