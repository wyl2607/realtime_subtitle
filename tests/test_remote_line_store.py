from __future__ import annotations

import json
from pathlib import Path

from realtime_subtitle.remote.line_store import LineStore


FIXTURE = Path(__file__).with_name("fixtures") / "p5_cases.json"


def test_line_store_runs_all_p5_fixture_cases():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert data["p5_cases"]

    for case in data["p5_cases"]:
        store = LineStore()
        for op in case["operations"]:
            if op["type"] == "add_local":
                store.add_local(
                    key=op["key"],
                    t0=op.get("t0"),
                    t1=op.get("t1"),
                    src=op["src"],
                    dst=op.get("dst"),
                )
            elif op["type"] == "apply_remote":
                replaced = store.apply_remote(
                    node_id=op["node_id"],
                    t0=op.get("t0"),
                    t1=op.get("t1"),
                    src=op["src"],
                    dst=op.get("dst"),
                )
                assert replaced == op.get("expect_replaced", replaced), (
                    f"{case['name']}: replaced expected {op.get('expect_replaced')} got {replaced}"
                )
        expected = case["expect"]
        assert store.serialise_for(expected["lines"]) == expected["lines"]
        limit = case.get("visible_limit", len(store.lines))
        assert store.visible_for(limit, expected["visible"]) == expected["visible"]


def test_line_store_set_translation():
    store = LineStore()
    store.add_local(key=1, t0=0.0, t1=2.0, src="Hallo Welt")
    assert store.lines[0].dst is None

    store.set_translation(key=1, dst="你好世界")
    assert store.lines[0].dst == "你好世界"

    # 非法或不存在 key 不崩溃
    store.set_translation(key=999, dst="不存在")
    assert len(store.lines) == 1


def test_line_store_visible_lines():
    store = LineStore()
    assert store.visible_lines(2) == []

    store.add_local(key=1, t0=0.0, t1=1.0, src="line 1")
    store.add_local(key=2, t0=1.0, t1=2.0, src="line 2")
    store.add_local(key=3, t0=2.0, t1=3.0, src="line 3")

    visible = store.visible_lines(2)
    assert len(visible) == 2
    assert [v.key for v in visible] == [2, 3]

    assert store.visible_lines(0) == []
    assert store.visible_lines(-1) == []


def test_line_store_apply_node_alias():
    store = LineStore()
    store.add_local(key=1, t0=0.0, t1=10.0, src="local")
    replaced = store.apply_node("node1", t0=0.0, t1=10.0, src="node text", dst="译文")
    assert replaced == [1]
    assert len(store.lines) == 1
    assert store.lines[0].src == "node text"
    assert store.lines[0].dst == "译文"


def test_line_store_capacity_trimming():
    store = LineStore(capacity=3)
    for i in range(5):
        store.add_local(key=i, t0=float(i), t1=float(i + 1), src=f"line {i}")

    assert len(store.lines) == 3
    assert [line.key for line in store.lines] == [2, 3, 4]


def test_line_store_clear():
    store = LineStore()
    store.add_local(key=1, t0=0.0, t1=1.0, src="test")
    assert len(store.lines) == 1
    store.clear()
    assert len(store.lines) == 0
