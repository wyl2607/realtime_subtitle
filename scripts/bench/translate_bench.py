"""P0 基线：Ollama 各翻译档位在 Apple Silicon 上的质量、延迟、内存。

质量：FLEURS de_de ↔ cmn_hans_cn 平行句，chrF（字符级，中文不用分词）。
延迟：拆成 load / prompt_eval / eval，并对 wall - total_duration（避坑第 23 条）。
内存：/api/ps 的 size（统一内存里模型实际占用）。

prompt 与生产 translator_queue 的「新闻访谈」风格同构（无上下文、无术语表）。
用法：python translate_bench.py --pairs de_zh_pairs.json --n 40
"""
import argparse
import json
import statistics
import subprocess
import time

import requests
import sacrebleu

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pairs", required=True)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--models", default="qwen3.5:2b,qwen3.5:4b,qwen3.5:9b")
    ap.add_argument("--out", default="translate_results.jsonl")
    a = ap.parse_args()
    pairs = json.load(open(a.pairs))[: a.n]
    for model in a.models.split(","):
        rec = bench(model, pairs)
        print(json.dumps({k: v for k, v in rec.items() if k != "samples"}, ensure_ascii=False),
              flush=True)
        with open(a.out, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
