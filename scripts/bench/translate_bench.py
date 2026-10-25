"""P0 基线：Ollama 各翻译档位在 Apple Silicon 上的质量、延迟、内存。

质量：FLEURS de_de ↔ cmn_hans_cn 平行句，chrF（字符级，中文不用分词）。
延迟：拆成 load / prompt_eval / eval，并对 wall - total_duration（避坑第 23 条）。
内存：/api/ps 的 size（统一内存里模型实际占用）。

prompt 与生产 translator_queue 的「新闻访谈」风格同构（无上下文、无术语表）。
用法：python translate_bench.py --pairs de_zh_pairs.json --n 40

--pipeline：不跑 Ollama，改量 Apple Translation 这段实时翻译链路，回答
"德语上屏 → 中文跟上"到底花在哪（不依赖 GUI / ASR / 麦克风）：

  python translate_bench.py --pipeline --helper macos-native/.build/release/rstranslate

分三段打印：helper 往返、enqueue→on_pair（排队等待/helper/emit），以及按
Mac 真实节奏（每 0.5s 一块音频、每轮识别 ~2.4s）模拟时草稿能否真的触发。
"""
import argparse
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

import requests

try:  # chrF 只在质量基线里用；--pipeline 不该因为没装 sacrebleu 就跑不起来
    import sacrebleu
except ImportError:  # pragma: no cover - 装了 requirements-dev 才有
    sacrebleu = None

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

URL = "http://127.0.0.1:11434"
NUM_CTX = 4096  # 与 config.OLLAMA_NUM_CTX 一致，避免 runner 重装（避坑第 21 条）

RULES = ("1. 这是新闻播报或访谈：中文要通顺、准确、克制，不要加戏也不要过分口语化\n"
         "2. 机构名、职务、数字、地名必须准确；德语长句按中文习惯拆成短句\n"
         "3. 说话人自己的口语停顿/重复可以省略，但事实一个都不能改")


def prompt_for(sentence):
    return f"""你是德语新闻/访谈节目的字幕翻译。请把德语对白翻译成自然的中文。

【要求】
{RULES}
4. 只输出中文翻译，不要解释，不要输出德语原文
5. 【上下文】只用来帮助理解，绝对不要翻译上下文里的句子——它们已经显示过了
6. 当前对白哪怕只是半句、不完整，也只翻这半句：不要补全、不要加括号注释或说明
7. 时间是24小时制（22:15 = 22点15分，19:10 = 19点10分），照抄小时和分钟，不要换算成上午/下午，数字一律不改

【德语上下文（此前的对白）】
***
（无上下文）
***

【当前对白】
***
{sentence}
***

中文翻译：
"""


def generate(model, prompt):
    t = time.time()
    r = requests.post(f"{URL}/api/generate", json={
        "model": model, "prompt": prompt, "stream": False, "think": False,
        "keep_alive": "10m",
        "options": {"temperature": 0.3, "top_p": 0.9, "num_predict": 512, "num_ctx": NUM_CTX},
    }, timeout=300)
    r.raise_for_status()
    d = r.json()
    d["_wall"] = time.time() - t
    return d


def unload(model):
    requests.post(f"{URL}/api/generate", json={"model": model, "keep_alive": 0}, timeout=60)


def pressure_level():
    out = subprocess.run(["sysctl", "-n", "kern.memorystatus_vm_pressure_level"],
                         capture_output=True, text=True).stdout.strip()
    return int(out or 0)


def bench(model, pairs):
    for m in requests.get(f"{URL}/api/ps").json().get("models", []):
        unload(m["name"])
    time.sleep(2)
    cold = generate(model, prompt_for(pairs[0]["de"]))
    ps = {m["name"]: m for m in requests.get(f"{URL}/api/ps").json().get("models", [])}
    info = ps.get(model, {})

    hyps, refs, walls, evals, overhead, pressures = [], [], [], [], [], []
    for p in pairs:
        d = generate(model, prompt_for(p["de"]))
        hyps.append(d["response"].strip())
        refs.append(p["zh"])
        walls.append(d["_wall"])
        evals.append(d["eval_duration"] / 1e9)
        overhead.append(d["_wall"] - d["total_duration"] / 1e9)
        pressures.append(pressure_level())
    chrf = sacrebleu.corpus_chrf(hyps, [refs]).score
    unload(model)
    return {
        "model": model, "n": len(pairs), "chrf": round(chrf, 2),
        "cold_load_s": round(cold["load_duration"] / 1e9, 2),
        "wall_p50": round(statistics.median(walls), 3),
        "wall_p90": round(sorted(walls)[int(len(walls) * 0.9)], 3),
        "eval_p50": round(statistics.median(evals), 3),
        "transport_overhead_p50_ms": round(statistics.median(overhead) * 1000, 1),
        "mem_gb": round(info.get("size", 0) / 1e9, 2),
        "vram_gb": round(info.get("size_vram", 0) / 1e9, 2),
        "max_pressure": max(pressures),
        "samples": [{"de": p["de"], "ref": r, "hyp": h}
                    for p, r, h in list(zip(pairs, refs, hyps))[:4]],
    }


def _pct(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(q * len(values)))] if values else 0.0


PIPELINE_SENTENCES = [
    "Guten Morgen, meine Damen und Herren.",
    "Der Bundestag hat heute über den Haushalt für das kommende Jahr beraten.",
    "Die Polizei teilte mit, dass es bei dem Vorfall keine Verletzten gab.",
    "Das Wetter bleibt in den nächsten Tagen wechselhaft und kühl.",
    "Ja.",
]


def _make_translator(helper):
    """和生产同一套翻译代码，但绕开 ASR/UI：只喂句子、只量时间。"""
    from collections import deque
    from concurrent.futures import ThreadPoolExecutor
    from threading import Lock

    import realtime_subtitle.config as config
    from realtime_subtitle.translate.apple_translate import AppleTranslator
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    config.SOURCE_LANGUAGE = "de"
    config.TARGET_LANGUAGE = "zh"

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._translate_backend = "apple"
    t.apple_translator = AppleTranslator(helper_path=helper)
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_retry_queue = []
    t._tx_inflight = []
    t._tx_epoch = 0
    t._lang_revision = 0
    t.context_history = deque(maxlen=6)
    t.closing = False
    t._tx_executor = ThreadPoolExecutor(max_workers=1)
    t._tx_slow_until = 0.0
    t._tx_fail_streak = 0
    t._tx_circuit_until = 0.0
    t._ollama_hot = True
    t._stats_lock = Lock()
    t._stats_t0 = time.time()
    t._stat_tx = []
    t._stat_dict = 0
    t._stat_draft = 0
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t._transcript_ok = False
    t._lang_hold_since = 0.0
    t._lang_held = []
    t._lang_hold_spent = False
    t._asr_lock = Lock()
    t._audio_inbox = []
    t._asr_scheduled = False
    t._asr_busy = False
    t._asr_backlog_n = 0
    t._draft_last_text = ""
    t._draft_last_time = 0.0
    t.pending_text = ""
    t.pending_words = []
    t._lookup_inflight_n = 0
    t._inflight_lock = Lock()
    t.on_display = None
    t.on_pair = None
    t.on_draft = None
    t.on_status = None
    t.on_translate_failed = None
    return t


def _helper_roundtrip(helper, repeats):
    from realtime_subtitle.translate.apple_translate import AppleTranslator

    tx = AppleTranslator(helper_path=helper)
    try:
        rows = []
        for i in range(repeats):
            text = PIPELINE_SENTENCES[i % len(PIPELINE_SENTENCES)]
            t0 = time.perf_counter()
            out = tx.translate(text, "de", "zh", timeout=15)
            rows.append({
                "chars": len(text),
                "ms": (time.perf_counter() - t0) * 1000,
                "ok": bool(out),
            })
        return rows
    finally:
        tx.close()


def _commit_to_pair(t, text, t0_seconds):
    """一次完整的"句子入队 → on_pair 回调"，拆成排队/helper/emit 三段。"""
    from realtime_subtitle.translate.translator_queue import TimedSentence

    rec = {}
    pair = {}
    orig = t._translate_single_sentence

    def wrapped(sentence, context, on_partial=None):
        rec["worker_start"] = time.perf_counter()
        out = orig(sentence, context, on_partial=on_partial)
        rec["helper_done"] = time.perf_counter()
        return out

    def on_pair(src, dst, _t0, _t1, _key, _rev):
        pair["at"] = time.perf_counter()
        pair["text"] = dst

    t._translate_single_sentence = wrapped
    t.on_pair = on_pair
    try:
        rec["enqueue"] = time.perf_counter()
        t._enqueue_sentences([TimedSentence(text, t0=t0_seconds, t1=t0_seconds + 2.5,
                                            key=f"bench:{t0_seconds}")])
        deadline = time.perf_counter() + 20
        while "at" not in pair and time.perf_counter() < deadline:
            time.sleep(0.005)
    finally:
        t._translate_single_sentence = orig
    enq = rec["enqueue"]
    start = rec.get("worker_start", enq)
    done = rec.get("helper_done", start)
    at = pair.get("at", enq)
    return {
        "queue_wait_ms": (start - enq) * 1000,
        "helper_ms": (done - start) * 1000,
        "emit_ms": (at - done) * 1000,
        "total_ms": (at - enq) * 1000,
        "chars": len(text),
        "ok": pair.get("text", "") not in ("", text),
    }


def _draft_rounds(t, rounds, round_sec, chunk_sec):
    """按 Mac 真实节奏模拟：每 0.5s 来一块音频，每轮识别 ~2.4s。

    草稿是在每轮识别结束时由 _maybe_draft 提交的；看它能不能真的上屏。
    """
    import threading

    drafts = []
    t.on_draft = lambda text, _rev: drafts.append({
        "at": time.perf_counter(), "text": text})
    stop = threading.Event()

    def feeder():
        while not stop.is_set():
            with t._asr_lock:
                t._audio_inbox.append((b"\0" * 16000, time.time()))
                t._asr_scheduled = True
            stop.wait(chunk_sec)

    th = threading.Thread(target=feeder, daemon=True)
    th.start()
    done_rounds = 0
    latencies = []
    missed = 0
    words = ["Der", "Bundestag", "hat", "heute", "über", "den", "Haushalt",
             "beraten", "und", "weitere", "Punkte", "beschlossen"]
    pending = []
    for i in range(rounds):
        with t._asr_lock:
            t._audio_inbox = []
            t._asr_backlog_n = 0
        t._asr_busy = True
        time.sleep(round_sec)  # MLX 推理
        t._asr_busy = False
        round_end = time.perf_counter()
        # 每轮提交约 3 个词，攒到 ~100 字符就当成整句送走、残句从零开始
        pending.extend(words[(i * 3) % len(words):(i * 3) % len(words) + 3])
        if len(" ".join(pending)) > 100:
            pending = []
        t.pending_text = " ".join(pending)
        t._maybe_draft()
        before = len(drafts)
        deadline = time.perf_counter() + 1.2
        while len(drafts) == before and time.perf_counter() < deadline:
            time.sleep(0.005)
        if len(drafts) > before:
            latencies.append((drafts[-1]["at"] - round_end) * 1000)
        elif pending:
            missed += 1
        done_rounds += 1
    stop.set()
    th.join(timeout=1)
    return {
        "rounds": done_rounds,
        "round_sec": round_sec,
        "chunk_sec": chunk_sec,
        "drafts": len(drafts),
        "missed_rounds_with_pending": missed,
        "draft_latency_after_round_ms": [round(x, 1) for x in latencies],
        "draft_latency_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
    }



def _pipeline(args):
    helper = args.helper or str(REPO_ROOT / "macos-native" / ".build" / "release" / "rstranslate")
    if not Path(helper).is_file():
        raise SystemExit(f"找不到 Apple Translation helper: {helper}（先 cd macos-native && swift build -c release）")
    t = _make_translator(helper)
    try:
        rt = _helper_roundtrip(helper, args.repeats)
        pairs = []
        for i, text in enumerate(PIPELINE_SENTENCES):
            pairs.append(_commit_to_pair(t, text, t0_seconds=float(i * 3)))
        draft = _draft_rounds(t, args.rounds, args.round_sec, 0.5)
    finally:
        t.apple_translator.close()
        t._tx_executor.shutdown(wait=True)

    ms = [r["ms"] for r in rt]
    table = {
        "helper_roundtrip": {
            "n": len(ms),
            "p50_ms": round(statistics.median(ms), 1),
            "p90_ms": round(_pct(ms, 0.9), 1),
            "by_len": [{"chars": r["chars"], "ms": round(r["ms"], 1)} for r in rt[:len(PIPELINE_SENTENCES)]],
        },
        "commit_to_pair": {
            key: {"p50_ms": round(statistics.median([p[key] for p in pairs]), 2),
                  "p90_ms": round(_pct([p[key] for p in pairs], 0.9), 2)}
            for key in ("queue_wait_ms", "helper_ms", "emit_ms", "total_ms")
        },
        "draft_mac_pacing": draft,
    }
    print(json.dumps(table, ensure_ascii=False, indent=2))
    if args.out:
        with open(args.out, "a") as f:
            f.write(json.dumps(table, ensure_ascii=False) + "\n")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", help="FLEURS 平行句 JSON（质量基线用）")
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--models", default="qwen3.5:2b,qwen3.5:4b,qwen3.5:9b")
    ap.add_argument("--out", help="结果写进这个 JSONL（默认 quality 模式 translate_results.jsonl）")
    ap.add_argument("--pipeline", action="store_true", help="量 Apple 实时翻译链路，不跑 Ollama")
    ap.add_argument("--helper", help="rstranslate 路径（默认 macos-native/.build/release）")
    ap.add_argument("--repeats", type=int, default=20, help="helper 往返重复次数")
    ap.add_argument("--rounds", type=int, default=10, help="模拟的 Mac 识别轮数")
    ap.add_argument("--round-sec", type=float, default=2.4, help="每轮识别耗时（秒）")
    a = ap.parse_args()
    if a.pipeline:
        _pipeline(a)
        return
    if not a.pairs:
        ap.error("--pairs 必填（或用 --pipeline）")
    if sacrebleu is None:
        raise SystemExit("质量基线需要 sacrebleu：pip install -r requirements-dev.txt")
    pairs = json.load(open(a.pairs))[: a.n]
    for model in a.models.split(","):
        rec = bench(model, pairs)
        print(json.dumps({k: v for k, v in rec.items() if k != "samples"}, ensure_ascii=False),
              flush=True)
        with open(a.out or "translate_results.jsonl", "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")



if __name__ == "__main__":
    main()
