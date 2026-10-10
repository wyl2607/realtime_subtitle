"""hybrid_score.py 的夹具测试：P5 重放、精修延迟、重复/丢句检查。"""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bench"))

import hybrid_score as hs  # noqa: E402

REFS = ["hallo welt", "wie geht es dir"]


def ev(kind, t, text, **kw):
    return {"ev": kind, "t": t, "text": text, **kw}


def local(t, text, t0, t1):
    return ev("final", t, text, t0=t0, t1=t1)


def node(t, text, t0, t1):
    return ev("final", t, text, t0=t0, t1=t1, source="node:mini2")


def real_node(t, text, t0, t1):
    return ev("final", t, f"[mini2] {text}", id=None, t0=t0, t1=t1)


def test_node_replaces_overlapping_local_lines():
    events = [
        ev("volatile", 1.0, "Hal"),
        local(5.0, "Hallo Welt", 0.0, 4.0),
        local(9.0, "wie geht es", 5.0, 8.0),
        node(8.0, "Hallo Welt.", 0.0, 4.2),
        node(12.0, "Wie geht es dir?", 5.0, 8.2),
    ]
    r = hs.score(events, REFS, b_events=[ev("volatile", 1.5, "Hal")], c_wer=0.0)
    assert r["lines"] == 2 and r["node_lines"] == 2
    assert r["final_wer"] == 0.0
    assert r["first_text_s"] == 1.0 and r["b_first_text_s"] == 1.5
    assert r["refine_median_s"] == pytest.approx(3.8)
    assert not r["problems"]
    assert all(v for v in r["checks"].values())


def test_real_headless_replace_ids_drive_node_replay():
    events = [
        local(4.0, "Hallo lokale Welt", 0.0, 3.0) | {"id": 1},
        local(7.0, "wie geht es", 3.2, 6.0) | {"id": 2},
        ev("replace", 7.5, "P5 node=mini2 replaced=1 ids=[1]", id=None, t0=0.0, t1=3.1),
        real_node(7.6, "Hallo Welt", 0.0, 3.1),
        ev("translation", 7.6, "[mini2] Hallo Welt zh", id=None),
    ]
    r = hs.score(events, REFS, b_events=[ev("volatile", 1.0, "Hal")], c_wer=0.0)
    assert [ln["text"] for ln in hs.replay_lines(events)] == ["Hallo Welt", "wie geht es"]
    assert r["node_lines"] == 1
    assert r["problems"] == []


def test_real_node_first_drops_later_covered_local_line():
    events = [
        ev("replace", 7.5, "P5 node=mini2 replaced=0 ids=[]", id=None, t0=0.0, t1=3.1),
        real_node(7.6, "Hallo Welt", 0.0, 3.1),
        local(8.0, "Hallo Welt", 0.0, 3.0) | {"id": 1},
    ]
    lines = hs.replay_lines(events)
    assert len(lines) == 1
    assert lines[0]["node"] is True
    assert hs.check_integrity(lines, ["hallo welt"]) == []


def test_uncovered_local_line_is_kept():
    events = [local(5.0, "Hallo Welt", 0.0, 4.0), local(9.0, "wie geht es dir", 5.0, 8.0),
              node(8.0, "Hallo Welt", 0.0, 4.0)]
    lines = hs.replay_lines(events)
    assert [ln["node"] for ln in lines] == [True, False]


def test_untimed_local_line_not_replaced_and_flags_nothing_lost():
    events = [ev("final", 5.0, "Hallo Welt"), node(8.0, "Hallo Welt", 0.0, 4.0)]
    lines = hs.replay_lines(events)
    assert len(lines) == 2  # 无时间的本机行不参与替换
    assert any("重复" in p for p in hs.check_integrity(lines, ["hallo welt"]))


def test_dropped_sentence_detected():
    lines = hs.replay_lines([local(5.0, "Hallo Welt", 0.0, 4.0)])
    assert any("丢句" in p for p in hs.check_integrity(lines, REFS))


def test_slow_refine_fails_check():
    events = [local(5.0, "Hallo Welt", 0.0, 4.0), node(20.0, "Hallo Welt", 0.0, 4.0)]
    r = hs.score(events, ["hallo welt"])
    assert r["checks"]["精修中位延迟 ≤ 4s"] is False


def test_load_events_skips_non_json(tmp_path):
    p = tmp_path / "e.events"
    p.write_text("warning: x\n" + json.dumps(ev("final", 1, "a")) + "\n{broken\n", encoding="utf-8")
    assert len(hs.load_events(p)) == 1
