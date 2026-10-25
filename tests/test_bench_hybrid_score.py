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
        ev("clock", 0.0, "", t0=0.0),
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
    events = [ev("clock", 0.0, "", t0=0.0),
              local(5.0, "Hallo Welt", 0.0, 4.0), node(20.0, "Hallo Welt", 0.0, 4.0)]
    r = hs.score(events, ["hallo welt"])
    assert r["checks"]["精修中位延迟 ≤ 4s"] is False


def test_load_events_skips_non_json(tmp_path):
    p = tmp_path / "e.events"
    p.write_text("warning: x\n" + json.dumps(ev("final", 1, "a")) + "\n{broken\n", encoding="utf-8")
    assert len(hs.load_events(p)) == 1


def test_accumulated_node_overlap_detects_duplicate():
    # 客户端（rslite）没有替换本机句：replace 事件明确列出的 ids 不含本机 id=1，
    # 两条节点句各覆盖 40%、合计 80% -> 本机句与节点句同时可见 = 重复
    events = [
        ev("final", 5.0, "local sentence", id=1, t0=0.0, t1=10.0),
        ev("replace", 6.0, "P5 node=mini2 replaced=1 ids=[7]"),
        real_node(6.0, "node1", 0.0, 4.0),
        ev("replace", 8.0, "P5 node=mini2 replaced=1 ids=[7]"),
        real_node(8.0, "node2", 6.0, 10.0),
    ]
    b_events = [ev("volatile", 1.0, "x")]
    r = hs.score(events, refs=["local sentence"], b_events=b_events, c_wer=0.0)
    assert r["problem_counts"]["duplicates"] == 1
    assert any("累计重叠≥50%" in p for p in r["problems"])


def test_node_first_cumulative_coverage_drops_late_local():
    # 003 P5：两条节点句先到（各 <50%、合计 ≥50%），后到的本机句按并集覆盖被丢弃
    events = [
        real_node(2.0, "node1", 0.0, 4.0),
        real_node(3.0, "node2", 6.0, 10.0),
        local(5.0, "local sentence", 0.0, 10.0),
    ]
    lines = hs.replay_lines(events)
    assert all(ln["node"] for ln in lines) and len(lines) == 2


def test_accumulated_overlap_below_50_no_duplicate():
    # Local sentence: 0.0-10.0
    # Two node sentences: each 0.0-2.0 (overlap 2.0 -> 20% each, total 40% < 50%)
    events = [
        local(5.0, "local sentence", 0.0, 10.0),
        node(2.0, "node1", 0.0, 2.0),
        node(4.0, "node2", 0.0, 2.0),
    ]
    b_events = [ev("volatile", 1.0, "x")]
    r = hs.score(events, refs=["local sentence"], b_events=b_events, c_wer=0.0)
    assert r["problem_counts"]["duplicates"] == 0
    assert r["problem_counts"]["dropped"] == 0


def test_local_sentence_replaced_by_node_not_considered_for_duplicate():
    # Even if the accumulated overlap would be high, if the local sentence is replaced, it should not be considered.
    events = [
        local(5.0, "local sentence", 0.0, 10.0) | {"id": 1},
        node(2.0, "node1", 0.0, 4.0),
        node(8.0, "node2", 6.0, 10.0),
        ev("replace", 9.0, "P5 node=mini2 replaced=1 ids=[1]", id=None, t0=0.0, t1=10.0),
        real_node(9.1, "replaced sentence", 0.0, 10.0),
    ]
    b_events = [ev("volatile", 1.0, "x")]
    r = hs.score(events, refs=["local sentence", "replaced sentence"], b_events=b_events, c_wer=0.0)
    # The local sentence should be replaced, so no duplicate problem for it.
    # We expect no duplicate problems (the replaced sentence is a node line and we don't check node lines for duplicates with local? 
    # Actually, we only check local lines (non-node) for duplicates with node lines. The local line is gone, so no duplicate.
    assert r["problem_counts"]["duplicates"] == 0
    assert r["problem_counts"]["dropped"] == 0


def test_clock_event_removes_startup_offset_from_refine_latency():
    # headless t 从进程启动算，t1 从第一块样本算；clock 事件给出两者零点差 1.0s。
    events = [
        ev("clock", 1.0, "", t0=0.0),
        local(6.0, "Hallo Welt", 0.5, 4.0),
        real_node(6.0, "Hallo Welt", 0.5, 4.0),
    ]
    r = hs.score(events, REFS, b_events=[ev("volatile", 1.0, "Hal")], c_wer=0.0)
    assert r["clock_offset_s"] == pytest.approx(1.0)
    assert r["refine_median_s"] == pytest.approx(1.0)  # 2.0 raw - 1.0 零点差


def test_missing_clock_event_makes_refine_unevaluable():
    # 和 acceptance/run_mac_key.sh 同一契约：没有共同零点就不硬算 t - t1。
    events = [local(5.0, "Hallo Welt", 0.0, 4.0), node(8.0, "Hallo Welt", 0.0, 4.0)]
    r = hs.score(events, ["hallo welt"])
    assert r["clock_offset_s"] is None
    assert r["refine_median_s"] is None
    assert r["refine_median_raw_s"] == pytest.approx(4.0)
    assert r["checks"]["精修中位延迟 ≤ 4s"] is None


def test_parse_node_log_splits_wait_translate_and_show():
    log = "\n".join([
        "rslite.node send node=mini2 audio_end=9.000 t=10.500",
        "rslite.node send node=mini2 audio_end=10.000 t=10.520",
        "rslite.node recv node=mini2 ev=final id=1 a1=9.100 t=11.600",
        "rslite.node recv node=mini2 ev=final id=2 a1=10.200 t=12.900",
        "rslite.node recv node=mini2 ev=translation id=1 t=11.700",
        "rslite.node show node=mini2 id=1 t0=4.000 t1=9.100 dst=1 t=11.710",
        "rslite.node show node=mini2 id=2 t0=9.200 t1=10.200 dst=0 t=12.930",
        "rslite.route select node=mini2 score=1.0",
    ])
    nt = hs.parse_node_log(log, clock_offset=0.5)
    assert nt["send_n"] == 2
    assert nt["send_lag_median_s"] == pytest.approx(0.51)   # (1.0 + 0.02) / 2
    assert nt["final_n"] == 2
    assert nt["final_wait_median_s"] == pytest.approx(2.1)  # (2.0 + 2.2) / 2
    assert nt["translate_gap_median_s"] == pytest.approx(0.1)
    assert nt["show_gap_median_s"] == pytest.approx(0.02)   # (0.01 + 0.03) / 2
    assert nt["show_with_node_translation"] == 1


def test_parse_node_log_without_clock_keeps_only_wall_to_wall_gaps():
    log = "\n".join([
        "rslite.node send node=mini2 audio_end=9.000 t=10.500",
        "rslite.node recv node=mini2 ev=final id=1 a1=9.100 t=11.600",
        "rslite.node recv node=mini2 ev=translation id=1 t=11.700",
        "rslite.node show node=mini2 id=1 t0=4.000 t1=9.100 dst=1 t=11.710",
    ])
    nt = hs.parse_node_log(log)
    assert nt["send_lag_median_s"] is None and nt["final_wait_median_s"] is None
    assert nt["translate_gap_median_s"] == pytest.approx(0.1)
    assert nt["show_gap_median_s"] == pytest.approx(0.01)


def test_offline_summary_reads_hybrid_node_log(tmp_path):
    (tmp_path / "B.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [ev("volatile", 1.0, "Hal"), local(5.0, "Hallo Welt", 0.0, 4.0)])
        + "\n", encoding="utf-8")
    (tmp_path / "C.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [local(5.0, "Hallo Welt", 0.0, 4.0)])
        + "\n", encoding="utf-8")
    (tmp_path / "hybrid.jsonl").write_text(
        "\n".join(json.dumps(e) for e in [
            ev("clock", 1.0, "", t0=0.0),
            local(5.0, "Hallo Welt", 0.0, 4.0),
            real_node(6.0, "Hallo Welt", 0.0, 4.0),
        ]) + "\n", encoding="utf-8")
    (tmp_path / "hybrid.stderr.log").write_text(
        "rslite.node recv node=mini2 ev=final id=1 a1=4.000 t=6.000\n", encoding="utf-8")
    summary = hs.offline_summary(tmp_path, ["hallo welt"])
    # clock 零点 = 1.0：recv t=6.0、a1=4.0 -> 6.0 - 4.0 - 1.0 = 1.0
    assert summary["hybrid_mode"]["node_timings"]["final_wait_median_s"] == pytest.approx(1.0)
