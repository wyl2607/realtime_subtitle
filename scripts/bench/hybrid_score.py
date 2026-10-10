"""混合档验收评分：从 rslite --headless 的事件流算 RFC Done criteria 4 的四项指标。

  1. 最终文本 WER（对 concat5 对应的前 5 句参考）——并与 C 的基线比，要求 ≤ C + 1pp；
  2. 首次出字时间——与 B 的同类数字比，要求不慢于 B；
  3. 精修延迟中位数（节点 final 到达时刻 - 该句音频结束时刻）——要求 ≤ 4s；
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


def _should_replace(local_line, node_t0, node_t1):
    if local_line["node"] or node_t0 is None or node_t1 is None:
        return False
    l0, l1 = local_line.get("t0"), local_line.get("t1")
    if l0 is None or l1 is None:
        return False
    duration = max(l1 - l0, 0.0)
    if duration <= 1e-9:
        return node_t0 - 1e-9 <= l0 <= node_t1 + 1e-9
    return _overlap(l0, l1, node_t0, node_t1) >= 0.5 * duration - 1e-9


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
            elif row["t0"] is not None and row["t1"] is not None:
                lines = [ln for ln in lines if not _should_replace(ln, row["t0"], row["t1"])]
            _insert_line(lines, row)
            pending_replace = None
        else:
            if any(_should_replace(row, ln["t0"], ln["t1"]) for ln in lines if ln["node"]):
                continue
            _insert_line(lines, row)
    return lines


def refine_latencies(events):
    """节点 final 到达时刻 - 其音频结束时刻（t1，客户端样本时钟）。

    file: 源按实时速度回放，样本时钟与 headless 墙钟同起点，所以两者可直接相减。
    """
    return [e["t"] - e["t1"] for e in events if e["ev"] == "final" and is_node(e) and e.get("t1") is not None]


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
        for nd in nodes:
            if _overlap(ln["t0"], ln["t1"], nd["t0"], nd["t1"]) >= 0.5 * (ln["t1"] - ln["t0"]):
                problems.append(f"重复：本机行 [{ln['t0']:.1f},{ln['t1']:.1f}] 与节点行同区间并存")
                break
    texts = [norm(ln["text"]) for ln in lines]
    for i, ref in enumerate(refs):
        best = max((difflib.SequenceMatcher(None, ref, t).ratio() for t in texts), default=0.0)
        if best < COVER_RATIO_MIN:
            problems.append(f"丢句：参考第 {i + 1} 句在最终文本里找不到相近的行（最高相似度 {best:.2f}）")
    return problems


def score(events, refs, b_events=None, c_wer=None):
    lines = replay_lines(events)
    hyp = norm(" ".join(ln["text"] for ln in lines))
    ref = norm(" ".join(refs))
    wer = jiwer.wer(ref, hyp)
    lats = refine_latencies(events)
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
        "problems": problems,
        "problem_counts": {
            "duplicates": sum(1 for p in problems if p.startswith("重复")),
            "dropped": sum(1 for p in problems if p.startswith("丢句")),
        },
    }
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
    print(f"重复/丢句         {r['problem_counts']['duplicates']} / {r['problem_counts']['dropped']}")
    for p in r["problems"]:
        print("  问题:", p)
    for name, ok in r["checks"].items():
        print(f"  [{'n/a ' if ok is None else 'PASS' if ok else 'FAIL'}] {name}")


def _load_refs(path, n):
    return [r["ref"] for r in load_jsonl(path)][:n]


def _score_path(path, refs, b_events=None, c_wer=None):
    r = score(load_events(path), refs, b_events=b_events, c_wer=c_wer)
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
    hybrid = _score_path(h_path, refs, b_events=b_events, c_wer=c["final_wer"])
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
    r = score(events, refs, b_events, c_wer)
    r["refs_n"] = len(refs)

    if a.json:
        print(json.dumps(r, ensure_ascii=False, indent=2, sort_keys=True))
    else:
        print_score(r, c_wer=c_wer)
    failed = [n for n, ok in r["checks"].items() if ok is False]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
