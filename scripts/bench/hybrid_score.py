"""混合档验收评分：从 rslite --headless 的事件流算 RFC Done criteria 4 的四项指标。

  1. 最终文本 WER（对 concat5 对应的前 5 句参考）——并与 C 的基线比，要求 ≤ C + 1pp；
  2. 首次出字时间——与 B 的同类数字比，要求不慢于 B；
  3. 精修延迟中位数（节点 final 到达时刻 - 该句音频结束时刻）——要求 ≤ 4s；
  4. 不重复、不丢句。

事件流是 headless 的 JSONL（非 JSON 行，如混进来的 stderr，直接跳过）：
  {"ev":"volatile|final|translation|status","t":墙钟秒,"id":..,"text":..,"t0":..,"t1":..,"source":..}
`source` 以 "node" 开头表示节点精修行；缺省/其它视为本机 B 行。B 版事件没有 source，
所以同一个脚本也能给 B 组打分（作为首字时间基线）。
节点行的区间取 t0/t1（P4：已换算到客户端时钟）。headless 里节点事件的确切字段待 TK-005
合并后核对——这里只依赖 ev/t/text/t0/t1/source。

「最终文本」按 P5 重放：节点行覆盖所有重叠 ≥ 自身时长 50% 的本机行；没有时间的本机行不参与替换。

用法：
  python scripts/bench/hybrid_score.py hybrid.events --refs refs.jsonl [--b-events b.jsonl] [--c-wer 0.026]
"""
import argparse
import difflib
import json
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


def is_node(rec):
    return str(rec.get("source", "")).startswith("node")


def first_text_time(events):
    """首次出字：第一条带文字的 volatile 或 final 的墙钟时间。"""
    times = [e["t"] for e in events if e["ev"] in ("volatile", "final") and e.get("text", "").strip()]
    return min(times) if times else None


def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def replay_lines(events):
    """按 P5 重放 final 事件，返回最终的行列表（按 t0 排序，无时间的按到达顺序）。"""
    lines = []  # 每行 {"text","t0","t1","node"}
    for e in events:
        if e["ev"] != "final" or not e.get("text", "").strip():
            continue
        row = {"text": e["text"], "t0": e.get("t0"), "t1": e.get("t1"), "node": is_node(e)}
        if row["node"] and row["t0"] is not None and row["t1"] is not None:
            kept, hit_at = [], None
            for ln in lines:
                if ln["node"] or ln["t0"] is None or ln["t1"] is None or ln["t1"] <= ln["t0"]:
                    kept.append(ln)
                    continue
                ov = _overlap(ln["t0"], ln["t1"], row["t0"], row["t1"])
                if ov >= 0.5 * (ln["t1"] - ln["t0"]):
                    if hit_at is None:
                        hit_at = len(kept)
                    continue
                kept.append(ln)
            kept.insert(len(kept) if hit_at is None else hit_at, row)
            lines = kept
        else:
            lines.append(row)
    timed = [ln for ln in lines if ln["t0"] is not None]
    if len(timed) == len(lines):
        lines.sort(key=lambda ln: ln["t0"])
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("events", help="混合档（或 B 档）headless 事件流 JSONL")
    ap.add_argument("--refs", required=True, help="refs.jsonl；只取前 --n 句")
    ap.add_argument("--n", type=int, default=N_SENTENCES)
    ap.add_argument("--b-events", help="B 档事件流，用来比首字时间")
    ap.add_argument("--c-wer", type=float, help="C 档最终 WER（小数，如 0.026）")
    a = ap.parse_args()

    # 不复用 native_score.read_jsonl：它的模块级 import 会拖进 sacrebleu，评分用不上
    refs = [r["ref"] for r in load_jsonl(a.refs)][: a.n]
    events = load_events(a.events)
    b_events = load_events(a.b_events) if a.b_events else None
    r = score(events, refs, b_events, a.c_wer)

    print(f"最终行数 {r['lines']}（其中节点行 {r['node_lines']}），参考 {len(refs)} 句")
    print(f"最终文本 WER      {fmt(r['final_wer'], 4)}   （C 基线 {fmt(a.c_wer, 4)}）")
    print(f"首字时间          {fmt(r['first_text_s'])} s （B {fmt(r['b_first_text_s'])} s）")
    print(f"精修延迟中位数    {fmt(r['refine_median_s'])} s （{r['refine_n']} 条节点 final）")
    for p in r["problems"]:
        print("  问题:", p)
    for name, ok in r["checks"].items():
        print(f"  [{'n/a ' if ok is None else 'PASS' if ok else 'FAIL'}] {name}")
    failed = [n for n, ok in r["checks"].items() if ok is False]
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
