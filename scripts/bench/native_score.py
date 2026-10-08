"""汇总 rsbench 输出，与 Whisper/Ollama 基线对比。"""
import argparse
import json
import statistics

import jiwer
import sacrebleu

import asr_bench


def read_jsonl(path):
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                out.append(json.loads(line))
    return out


def fmt(x, digits=3):
    if x is None:
        return ""
    if isinstance(x, str):
        return x
    return f"{x:.{digits}f}"


def table(headers, rows):
    print("| " + " | ".join(headers) + " |")
    print("| " + " | ".join(["---"] * len(headers)) + " |")
    for row in rows:
        print("| " + " | ".join(str(x) for x in row) + " |")
    print()


def score_asr(native_path, refs_path, baseline_path):
    native = read_jsonl(native_path)
    records = [r for r in native if not r.get("summary")]
    summary = next((r for r in native if r.get("summary")), {})
    refs = {r["id"]: r["ref"] for r in read_jsonl(refs_path)}
    y_true = [asr_bench.norm(refs[r["id"]]) for r in records]
    y_pred = [asr_bench.norm(r["hyp"]) for r in records]
    native_wer = jiwer.wer(y_true, y_pred)

    rows = [[
        "apple-speech",
        len(records),
        fmt(native_wer, 4),
        fmt(summary.get("rtf")),
        fmt(summary.get("proc_s")),
        fmt(summary.get("peak_rss_mb"), 1),
        fmt(summary.get("first_volatile_p50_s")),
    ]]
    for rec in read_jsonl(baseline_path):
        if "error" in rec:
            continue
        rows.append([
            rec["tier"],
            rec.get("n", ""),
            fmt(rec.get("wer"), 4),
            fmt(rec.get("rtf")),
            fmt(rec.get("proc_s")),
            fmt(rec.get("peak_rss_mb"), 1),
            fmt(rec.get("lat12_p50") or rec.get("lat_p50")),
        ])
    print("### ASR")
    table(["tier", "n", "WER", "RTF", "proc_s", "peak_rss_mb", "lat_s"], rows)


def score_translate(native_path, baseline_path):
    native = read_jsonl(native_path)
    records = [r for r in native if not r.get("summary")]
    summary = next((r for r in native if r.get("summary")), {})
    hyps = [r["hyp"] for r in records]
    refs = [r["ref"] for r in records]
    chrf = sacrebleu.corpus_chrf(hyps, [refs]).score
    times = [r["s"] for r in records]
    p50 = statistics.median(times) if times else summary.get("p50")
    p90 = sorted(times)[int(len(times) * 0.9)] if times else summary.get("p90")

    rows = [[
        "apple-translation",
        len(records),
        fmt(chrf, 2),
        fmt(summary.get("cold_s")),
        fmt(p50),
        fmt(p90),
        fmt(summary.get("peak_rss_mb"), 1),
    ]]
    for rec in read_jsonl(baseline_path):
        rows.append([
            rec["model"],
            rec.get("n", ""),
            fmt(rec.get("chrf"), 2),
            fmt(rec.get("cold_load_s")),
            fmt(rec.get("wall_p50")),
            fmt(rec.get("wall_p90")),
            fmt(rec.get("mem_gb"), 1),
        ])
    print("### Translation")
    table(["model", "n", "chrF", "cold_s", "p50", "p90", "mem"], rows)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--asr-native", required=True)
    ap.add_argument("--asr-refs", required=True)
    ap.add_argument("--asr-baseline", required=True)
    ap.add_argument("--translate-native", required=True)
    ap.add_argument("--translate-baseline", required=True)
    a = ap.parse_args()
    score_asr(a.asr_native, a.asr_refs, a.asr_baseline)
    score_translate(a.translate_native, a.translate_baseline)


if __name__ == "__main__":
    main()
