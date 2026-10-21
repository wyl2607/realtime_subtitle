"""混合档验收评分：从 rslite --headless 的事件流算 RFC Done criteria 4 的四项指标。

  1. 最终文本 WER（对 concat5 对应的前 5 句参考）——并与 C 的基线比，要求 ≤ C + 1pp；
  2. 首次出字时间——与 B 的同类数字比，要求不慢于 B；
  3. 精修延迟中位数（节点 final 到达时刻 - 该句音频结束时刻）——要求 ≤ 4s；
     两个时钟零点必须对齐：headless 的 t 从进程启动算，t0/t1 从第一块样本算，
     差值在 clock 事件里（df3bf1d 起）；不减掉它，延迟会平白多出启动的 0.5–1.5s；
  4. 不重复、不丢句。

事件流是 headless 的 JSONL（非 JSON 行，如混进来的 stderr，直接跳过）：
  {"ev":"volatile|final|translation|status","t":墙钟秒,"id":..,"text":..,"t0":..,"t1":..,"source":..}
旧夹具里 `source` 以 "node" 开头表示节点精修行；真实 headless 输出不带 source：
节点 final/translation 的 id 为 null，text 形如 "[mini2] 原文"，前面紧跟 replace 事件：
  {"ev":"replace","text":"P5 node=mini2 replaced=N ids=[1,2]","t0":..,"t1":..}

「最终文本」按 LineStore/P5 重放：replace 的 ids 指定被覆盖的本机句；没有 ids 时退回
重叠 ≥ 本机行自身时长 50% 的替换规则。节点行先到、本机行后到时，被节点覆盖的本机行丢弃。

用法：
  python scripts/bench/hybrid_score.py hybrid.events --refs refs.jsonl [--b-events b.jsonl] [--c-wer 0.026]
  python scripts/bench/hybrid_score.py --offline-dir results/hybrid_e2e_xxx --json
"""
import argparse
import difflib
import json
import re
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jiwer  # noqa: E402

from asr_bench import norm  # noqa: E402

N_SENTENCES = 5            # concat5.wav 只对应 refs.jsonl 的前 5 句（refs 共 60 句）
REFINE_LATENCY_MAX_S = 4.0  # RFC：精修结果到达的中位数上限
WER_MARGIN = 0.01           # RFC：最终文本 WER ≤ C + 1 个百分点
COVER_RATIO_MIN = 0.5       # 参考句与最相近的一行相似度低于此值视为丢句
DEFAULT_REFS = Path.home() / "projects" / "rs-mac-native-data" / "refs.jsonl"
NODE_PREFIX_RE = re.compile(r"^\[([^\]]+)\]\s*(.*)$")
REPLACE_RE = re.compile(r"\bnode=([^\s]+).*?\bids=\[([^\]]*)\]")
# 混合档 stderr 埋点（NodeClient/HybridEngine 写，NodeTiming 与 headless t 同一时钟）
NODE_LOG_PREFIX = "rslite.node "
NODE_LOG_FIELD_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=([^\s]+)")


def load_jsonl(path):
    with open(path, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def load_events(path):
    """读事件流；忽略不是 JSON 对象的行（stderr 混入）。"""
    out = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "ev" in rec:
            out.append(rec)
    return out


def parse_node_prefix(text):
    m = NODE_PREFIX_RE.match(str(text or "").strip())
    if not m:
        return None, str(text or "")
    return m.group(1), m.group(2)


def parse_replace(rec):
    if rec.get("ev") != "replace":
        return None
    m = REPLACE_RE.search(str(rec.get("text", "")))
    if not m:
        return None
    ids = [int(x) for x in re.findall(r"-?\d+", m.group(2))]
    return {"node_id": m.group(1), "ids": ids, "t0": rec.get("t0"), "t1": rec.get("t1")}


def node_info(rec):
    """返回 (is_node, node_id, clean_text)。兼容旧 source 字段和真实 [node] 前缀。"""
    text = str(rec.get("text", ""))
    prefixed_node, clean = parse_node_prefix(text)
    source = str(rec.get("source", ""))
    if source.startswith("node"):
        return True, source.split(":", 1)[-1] or prefixed_node, clean if prefixed_node else text
    if rec.get("id") is None and prefixed_node and rec.get("ev") in ("final", "translation"):
        return True, prefixed_node, clean
    return False, None, text


def is_node(rec):
    return node_info(rec)[0]


def first_text_time(events):
    """首次出字：第一条带文字的 volatile 或 final 的墙钟时间。"""
    times = [e["t"] for e in events if e["ev"] in ("volatile", "final") and e.get("text", "").strip()]
    return min(times) if times else None


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def _covered(l0, l1, intervals):
    """本机区间被节点区间并集覆盖的时长（与 LineStore.coveredDuration 同口径）。"""
    clipped = sorted((max(l0, a), min(l1, b)) for a, b in intervals if min(l1, b) > max(l0, a))
    total, cur = 0.0, None
    for a, b in clipped:
        if cur and a <= cur[1] + 1e-9:
            cur[1] = max(cur[1], b)
        else:
            if cur:
                total += cur[1] - cur[0]
            cur = [a, b]
    return total + (cur[1] - cur[0] if cur else 0.0)


def _should_replace(local_line, intervals):
    """P5（003 版）：本机行被所有节点行时间并集覆盖 ≥ 自身时长 50% 即替换。"""
    if local_line["node"]:
        return False
    l0, l1 = local_line.get("t0"), local_line.get("t1")
    if l0 is None or l1 is None:
        return False
    duration = max(l1 - l0, 0.0)
    if duration <= 1e-9:
        return any(a - 1e-9 <= l0 <= b + 1e-9 for a, b in intervals)
    return _covered(l0, l1, intervals) >= 0.5 * duration - 1e-9


def _node_intervals(lines):
    return [(ln["t0"], ln["t1"]) for ln in lines if ln["node"] and ln["t0"] is not None and ln["t1"] is not None]


def _insert_line(lines, row):
    if row["node"] and row["t0"] is not None:
        for i, ln in enumerate(lines):
            if ln["t0"] is not None and ln["t0"] > row["t0"]:
                lines.insert(i, row)
                return
        for i in range(len(lines) - 1, -1, -1):
            if lines[i]["t0"] is not None:
                lines.insert(i + 1, row)
                return
    lines.append(row)


def replay_lines(events):
    """按 P5/LineStore 重放 final 事件，返回最终的行列表。"""
    lines = []  # 每行 {"id","text","t0","t1","node","node_id"}
    pending_replace = None
    for e in events:
        replace = parse_replace(e)
        if replace is not None:
            pending_replace = replace
            continue
        if e["ev"] != "final" or not e.get("text", "").strip():
            continue
        node, node_id, text = node_info(e)
        row = {"id": e.get("id"), "text": text, "t0": e.get("t0"), "t1": e.get("t1"), "node": node, "node_id": node_id}
        if row["node"]:
            replace_ids = []
            if pending_replace and (not pending_replace["node_id"] or not node_id or pending_replace["node_id"] == node_id):
                replace_ids = pending_replace["ids"]
            if replace_ids:
                replace_id_set = set(replace_ids)
                lines = [ln for ln in lines if ln["node"] or ln.get("id") not in replace_id_set]
                _insert_line(lines, row)
            else:
                _insert_line(lines, row)
                intervals = _node_intervals(lines)
                lines = [ln for ln in lines if not _should_replace(ln, intervals)]
            pending_replace = None
        else:
            if _should_replace(row, _node_intervals(lines)):
                continue
            _insert_line(lines, row)
    return lines


def sample_clock_offset(events):
    """样本时钟零点在墙钟上的位置 = clock 事件的 t - t0。

    headless 的 t 从进程启动算，t0/t1 从「第一块样本」算；两者差值就是启动/
    模型加载耗掉的时间。老事件流没有 clock 事件时返回 None（无法校正）。
    """
    for e in events:
        if e.get("ev") != "clock":
            continue
        t, t0 = e.get("t"), e.get("t0")
        if isinstance(t, (int, float)) and isinstance(t0, (int, float)):
            return t - t0
    return None


def refine_latencies(events, clock_offset):
    """节点 final 到达时刻 - 其音频结束时刻（t1，客户端样本时钟）。

    样本时刻 s 对应的墙钟是 s + clock_offset，所以延迟要减掉这个零点差。
    没有 clock 事件时返回空列表：和 acceptance/run_mac_key.sh 的契约一致——
    绝不拿 `t - t1` 硬算（那会把启动/加载时间算进延迟，正是本指标失真的来源）。
    """
    if clock_offset is None:
        return []
    return [
        e["t"] - e["t1"] - clock_offset
        for e in events
        if e["ev"] == "final" and is_node(e) and e.get("t1") is not None
    ]


def raw_refine_latencies(events):
    """老口径（未对齐零点）的原始差值，仅作历史对比，不参与判定。"""
    return [
        e["t"] - e["t1"]
        for e in events
        if e["ev"] == "final" and is_node(e) and e.get("t1") is not None
    ]


def parse_node_log(text, clock_offset=None):
    """解析 stderr 里的 rslite.node send/recv/show 行，返回各段耗时中位数。

    只聚合数字：这些行本身只有节点 id、句子 id、音频秒和时刻，不含正文/音频/token。
    键是 (node, 句子 id)，因为重连后 id 会从 1 重新开始。

    send/recv 的 t 是墙钟、audio_end/a1 是样本时钟，必须减 clock_offset 才能和
    headless 的 t 对账（没有 clock 事件时这两项给 None）；translation/show 之间
    是墙钟对墙钟，不需要校正。
    """
    sends: list[float] = []
    finals: dict[tuple[str, int], dict] = {}
    translations: dict[tuple[str, int], float] = {}
    shows: dict[tuple[str, int], dict] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line.startswith(NODE_LOG_PREFIX):
            continue
        parts = line[len(NODE_LOG_PREFIX):].split()
        if len(parts) < 2:
            continue
        kind, fields = parts[0], dict(NODE_LOG_FIELD_RE.findall(" ".join(parts[1:])))
        try:
            t = float(fields["t"])
        except (KeyError, ValueError):
            continue
        if kind == "send":
            if clock_offset is None:
                continue
            try:
                sends.append(t - float(fields["audio_end"]) - clock_offset)
            except (KeyError, ValueError):
                continue
            continue
        try:
            key = (fields.get("node", ""), int(fields["id"]))
        except (KeyError, ValueError):
            continue
        if kind == "recv" and fields.get("ev") == "final":
            try:
                finals[key] = {"a1": float(fields["a1"]), "t": t}
            except (KeyError, ValueError):
                continue
        elif kind == "recv" and fields.get("ev") == "translation":
            translations[key] = t
        elif kind == "show":
            shows[key] = {"t": t, "dst": fields.get("dst") == "1"}

    def median(xs):
        return round(statistics.median(xs), 3) if xs else None

    final_waits = (
        [r["t"] - r["a1"] - clock_offset for r in finals.values() if r["a1"] >= 0]
        if clock_offset is not None else []
    )
    tx_gaps = [translations[k] - r["t"] for k, r in finals.items() if k in translations]
    # 上屏合并只算译文到达之后那一段；没有译文时退回「final 到达 → 上屏」。
    show_gaps = [
        shows[k]["t"] - translations.get(k, r["t"])
        for k, r in finals.items() if k in shows
    ]
    return {
        "send_n": len(sends),
        "send_lag_median_s": median(sends),
        "final_n": len(finals),
        "final_wait_median_s": median(final_waits),
        "translation_n": len(tx_gaps),
        "translate_gap_median_s": median(tx_gaps),
        "show_n": len(shows),
        "show_gap_median_s": median(show_gaps),
        "show_with_node_translation": sum(1 for r in shows.values() if r["dst"]),
    }


def check_integrity(lines, refs):
    """重复：最终行里出现归一化后相同的两行，或本机行与节点行时间重叠仍并存；丢句：参考句找不到相近的行。"""
    problems = []
    seen = {}
    for i, ln in enumerate(lines):
        key = norm(ln["text"])
        if key in seen:
            problems.append(f"重复：第 {seen[key]} 行与第 {i} 行文本相同")
        seen.setdefault(key, i)
    nodes = [ln for ln in lines if ln["node"] and ln["t0"] is not None and ln["t1"] is not None]
    for ln in lines:
        if ln["node"] or ln["t0"] is None or ln["t1"] is None or ln["t1"] <= ln["t0"]:
            continue
        local_duration = ln["t1"] - ln["t0"]
        total_overlap = 0.0
        for nd in nodes:
            total_overlap += _overlap(ln["t0"], ln["t1"], nd["t0"], nd["t1"])
        if total_overlap >= 0.5 * local_duration:
            problems.append(f"重复：本机行 [{ln['t0']:.1f},{ln['t1']:.1f}] 与节点行累计重叠≥50%")
    texts = [norm(ln["text"]) for ln in lines]
    for i, ref in enumerate(refs):
        best = max((difflib.SequenceMatcher(None, ref, t).ratio() for t in texts), default=0.0)
        if best < COVER_RATIO_MIN:
            problems.append(f"丢句：参考第 {i + 1} 句在最终文本里找不到相近的行（最高相似度 {best:.2f}）")
    return problems


def score(events, refs, b_events=None, c_wer=None, node_timings=None):
    lines = replay_lines(events)
    hyp = norm(" ".join(ln["text"] for ln in lines))
    ref = norm(" ".join(refs))
    wer = jiwer.wer(ref, hyp)
    clock_offset = sample_clock_offset(events)
    lats = refine_latencies(events, clock_offset)
    raw = raw_refine_latencies(events)
    first = first_text_time(events)
    b_first = first_text_time(b_events) if b_events is not None else None
    problems = check_integrity(lines, refs)
    result = {
        "lines": len(lines),
        "node_lines": sum(1 for ln in lines if ln["node"]),
        "final_wer": wer,
        "first_text_s": first,
        "b_first_text_s": b_first,
        "refine_n": len(lats),
        "refine_median_s": statistics.median(lats) if lats else None,
        "clock_offset_s": None if clock_offset is None else round(clock_offset, 3),
        "refine_median_raw_s": statistics.median(raw) if raw else None,
        "problems": problems,
        "problem_counts": {
            "duplicates": sum(1 for p in problems if p.startswith("重复")),
            "dropped": sum(1 for p in problems if p.startswith("丢句")),
        },
    }
    if node_timings is not None:
        result["node_timings"] = node_timings
    checks = {
        "首字不慢于 B": None if first is None or b_first is None else first <= b_first,
        f"最终 WER ≤ C+{WER_MARGIN * 100:.0f}pp": None if c_wer is None else wer <= c_wer + WER_MARGIN,
        f"精修中位延迟 ≤ {REFINE_LATENCY_MAX_S:.0f}s": None if not lats else statistics.median(lats) <= REFINE_LATENCY_MAX_S,
        "不重复不丢句": not problems,
    }
    result["checks"] = checks
    return result


def fmt(x, digits=3):
    return "n/a" if x is None else f"{x:.{digits}f}"


def print_score(r, c_wer=None):
    print(f"最终行数 {r['lines']}（其中节点行 {r['node_lines']}），参考 {r.get('refs_n', '?')} 句")
    print(f"最终文本 WER      {fmt(r['final_wer'], 4)}   （C 基线 {fmt(c_wer, 4)}）")
    print(f"首字时间          {fmt(r['first_text_s'])} s （B {fmt(r['b_first_text_s'])} s）")
    print(f"精修延迟中位数    {fmt(r['refine_median_s'])} s （{r['refine_n']} 条节点 final）")
    if r.get("clock_offset_s") is None:
        print("  ⚠️ 没有 clock 事件（第一样本零点），精修延迟不可判定；"
              f"未对齐零点的原始中位 {fmt(r.get('refine_median_raw_s'))} s（不建议引用）")
    else:
        print(f"  样本时钟零点    {r['clock_offset_s']} s（已从延迟里减掉；"
              f"未对齐时 {fmt(r.get('refine_median_raw_s'))} s）")
    nt = r.get("node_timings")
    if nt:
        print(f"  拆分（中位）    段尾+识别 {fmt(nt['final_wait_median_s'])} / "
              f"翻译 {fmt(nt['translate_gap_median_s'])} / 上屏 {fmt(nt['show_gap_median_s'])} s"
              f"（send lag {fmt(nt['send_lag_median_s'])}）")
    print(f"重复/丢句         {r['problem_counts']['duplicates']} / {r['problem_counts']['dropped']}")
    for p in r["problems"]:
        print("  问题:", p)
    for name, ok in r["checks"].items():
        print(f"  [{'n/a ' if ok is None else 'PASS' if ok else 'FAIL'}] {name}")


def _load_refs(path, n):
    return [r["ref"] for r in load_jsonl(path)][:n]


def _score_path(path, refs, b_events=None, c_wer=None, node_timings=None):
    r = score(load_events(path), refs, b_events=b_events, c_wer=c_wer, node_timings=node_timings)
    r["events_file"] = str(path)
    r["refs_n"] = len(refs)
    return r


def _rfc_checks(hybrid):
    checks = {
        "first_text_not_slower_than_b": {
            "pass": hybrid["first_text_s"] is not None
            and hybrid["b_first_text_s"] is not None
            and hybrid["first_text_s"] <= hybrid["b_first_text_s"],
            "value_s": hybrid["first_text_s"],
            "baseline_s": hybrid["b_first_text_s"],
        },
        "final_wer_le_c_plus_1pp": {
            "pass": hybrid["checks"][f"最终 WER ≤ C+{WER_MARGIN * 100:.0f}pp"] is True,
            "value": hybrid["final_wer"],
            "limit": None if hybrid.get("c_wer") is None else hybrid["c_wer"] + WER_MARGIN,
            "c_wer": hybrid.get("c_wer"),
        },
        "refine_median_le_4s": {
            "pass": hybrid["refine_median_s"] is not None and hybrid["refine_median_s"] <= REFINE_LATENCY_MAX_S,
            "value_s": hybrid["refine_median_s"],
            "limit_s": REFINE_LATENCY_MAX_S,
            "n": hybrid["refine_n"],
        },
        "no_duplicate_no_dropped": {
            "pass": not hybrid["problems"],
            "duplicates": hybrid["problem_counts"]["duplicates"],
            "dropped": hybrid["problem_counts"]["dropped"],
            "problems": hybrid["problems"],
        },
    }
    return checks


def offline_summary(out_dir, refs, c_mode_note=""):
    out_dir = Path(out_dir)
    b_path, c_path, h_path = out_dir / "B.jsonl", out_dir / "C.jsonl", out_dir / "hybrid.jsonl"
    b_events = load_events(b_path)
    b = _score_path(b_path, refs, b_events=b_events)
    c = _score_path(c_path, refs)
    node_log = out_dir / "hybrid.stderr.log"
    node_timings = None
    if node_log.exists():
        node_timings = parse_node_log(
            node_log.read_text(encoding="utf-8", errors="replace"),
            sample_clock_offset(load_events(h_path)),
        )
    hybrid = _score_path(h_path, refs, b_events=b_events, c_wer=c["final_wer"], node_timings=node_timings)
    hybrid["c_wer"] = c["final_wer"]
    checks = _rfc_checks(hybrid)
    return {
        "events_dir": str(out_dir),
        "refs_n": len(refs),
        "b_mode": {"mode": "local", **b},
        "c_mode": {
            "mode": "auto",
            "note": c_mode_note,
            **c,
        },
        "hybrid_mode": {"mode": "hybrid", **hybrid},
        "rfc_checks": checks,
        "passed": all(item["pass"] is True for item in checks.values()),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("events", nargs="?", help="混合档（或 B 档）headless 事件流 JSONL")
    ap.add_argument("--refs", default=str(DEFAULT_REFS), help="refs.jsonl；只取前 --n 句")
    ap.add_argument("--n", type=int, default=N_SENTENCES)
    ap.add_argument("--b-events", help="B 档事件流，用来比首字时间")
    ap.add_argument("--c-events", help="C 档事件流；未显式给 --c-wer 时用它计算 C 基线")
    ap.add_argument("--c-wer", type=float, help="C 档最终 WER（小数，如 0.026）")
    ap.add_argument("--offline-dir", help="包含 B.jsonl、C.jsonl、hybrid.jsonl 的目录，直接离线重算")
    ap.add_argument("--node-log", help="混合档 stderr 日志（rslite.node 埋点）；默认用 --offline-dir 里的 hybrid.stderr.log")
    ap.add_argument("--c-mode-note", default="", help="写入 --offline-dir JSON 汇总的 C 档取舍说明")
    ap.add_argument("--json", action="store_true", help="输出 JSON，适合脚本汇总")
    a = ap.parse_args()

    # 不复用 native_score.read_jsonl：它的模块级 import 会拖进 sacrebleu，评分用不上
    refs = _load_refs(a.refs, a.n)
    if a.offline_dir:
        summary = offline_summary(a.offline_dir, refs, c_mode_note=a.c_mode_note)
        if a.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2, sort_keys=True))
        else:
            print("=== B模式评估（基线）===")
            print_score(summary["b_mode"])
            print("\n=== C模式评估（auto，最接近可用 C 基线）===")
            if a.c_mode_note:
                print(a.c_mode_note)
            print_score(summary["c_mode"])
            print("\n=== 混合档模式评估 ===")
            print_score(summary["hybrid_mode"], c_wer=summary["c_mode"]["final_wer"])
            print("\n=== RFC 判定 ===")
            for name, item in summary["rfc_checks"].items():
                print(f"  [{'PASS' if item['pass'] else 'FAIL'}] {name}")
        sys.exit(0 if summary["passed"] else 1)

    if not a.events:
        ap.error("events 或 --offline-dir 必须提供")
    events = load_events(a.events)
    b_events = load_events(a.b_events) if a.b_events else None
    c_wer = a.c_wer
    if c_wer is None and a.c_events:
        c_wer = score(load_events(a.c_events), refs)["final_wer"]
    node_timings = None
    if a.node_log:
        node_timings = parse_node_log(
            Path(a.node_log).read_text(encoding="utf-8", errors="replace"),
            sample_clock_offset(events),
        )
    r = score(events, refs, b_events, c_wer, node_timings=node_timings)
    r["refs_n"] = len(refs)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_score(r, c_wer=c_wer)
    failed = [n for n, ok in r["checks"].items() if ok is False]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
