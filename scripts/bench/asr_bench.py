"""P0 基线：Apple Silicon 上各 Whisper 后端/档位的准确度、延迟、内存。

每个档位在独立子进程里跑（--one），这样峰值内存互不污染。
数据：FLEURS de_de test（人工转写），按 utterance 算 WER。

两种量法都要：
- 整句（离线）：WER 和 RTF，衡量"这档模型本身准不准"；
- 流式代价：12 秒缓冲单次 transcribe 的 p50 延迟——生产里每 0.5 秒调一次，
  单次必须明显小于 CHUNK_SUBMIT_SECONDS 才跟得上（CUDA 参考机 p50 0.26 秒）。

需要拆分 MLX 一轮时加 ``--profile --vad``：输出 ``timing_p50_s``，含 VAD、
mel、encoder、decoder、词时间戳对齐（cross-attention + CPU DTW）和 Python 余量。
它只用于基准，默认路径保持历史 WER/延迟口径不变。

用法：
  python asr_bench.py --parquet fleurs_de_test.parquet --n 60 --out results.jsonl
"""
import argparse
import io
import json
import re
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

# 从 scripts/bench 直接执行时，Python 不会把仓库根目录加入 sys.path。
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

TIERS = {
    # name: (backend, model)
    "mlx-turbo-fp16": ("mlx", "mlx-community/whisper-large-v3-turbo"),
    "mlx-turbo-8bit": ("mlx", "mlx-community/whisper-large-v3-turbo-8bit"),
    "mlx-turbo-4bit": ("mlx", "mlx-community/whisper-large-v3-turbo-4bit"),
    "mlx-turbo-de-f16": ("mlx", "mlx-community/whisper-large-v3-turbo-german-f16"),
    "mlx-turbo-de-q4": ("mlx", "mlx-community/whisper-large-v3-turbo-german-f16-q4"),
    "mlx-turbo-q4repo": ("mlx", "mlx-community/whisper-large-v3-turbo-q4"),
    # 从 turbo fp16 本地 nn.quantize(group_size=64) 出来的，见 quantize 步骤
    "mlx-turbo-q8-local": ("mlx", "models/turbo-q8-local"),
    "mlx-turbo-q4-local": ("mlx", "models/turbo-q4-local"),
    "mlx-medium": ("mlx", "mlx-community/whisper-medium-mlx"),
    "mlx-small": ("mlx", "mlx-community/whisper-small-mlx"),
    "fw-cpu-turbo-int8": ("fw", "large-v3-turbo"),
    "fw-cpu-small-int8": ("fw", "small"),
}


def load_fleurs(path, n):
    import pyarrow.parquet as pq
    import soundfile as sf

    # 只读前 n 行：整表读进来 RSS 先涨 2.8GB，内存数据就全被污染了
    batch = next(pq.ParquetFile(path).iter_batches(batch_size=n, columns=["audio", "transcription"]))
    t = batch.to_pylist()
    out = []
    for row in t:
        audio, sr = sf.read(io.BytesIO(row["audio"]["bytes"]), dtype="float32")
        if audio.ndim > 1:
            audio = audio.mean(axis=1)
        assert sr == 16000, sr
        out.append((audio, row["transcription"]))
    return out


def norm(s):
    # FLEURS 的 transcription 已是小写去标点；对假设做同样处理
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\säöüß]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def peak_rss_mb():
    # macOS 上 ru_maxrss 单位是字节
    import resource  # 仅 POSIX；放函数里，Windows 上 import norm 等工具函数不受影响

    return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6


def make_transcriber(backend, model, words=True, vad=False, profile=False):
    if backend == "mlx" and (vad or profile):
        from realtime_subtitle.asr.mlx_backend import load_mlx_model

        mlx_model = load_mlx_model(model)
        mlx_model.profile_enabled = profile

        def run(audio):
            segments, _info = mlx_model.transcribe(
                audio, language="de", word_timestamps=words, vad_filter=vad,
            )
            return "".join(segment.text for segment in segments), mlx_model.last_timing
        return run

    if backend == "mlx":
        import mlx_whisper

        def run(audio):
            r = mlx_whisper.transcribe(
                audio, path_or_hf_repo=model, language="de",
                word_timestamps=words, condition_on_previous_text=True,
                verbose=None,
            )
            return r["text"], None
        return run

    from faster_whisper import WhisperModel
    m = WhisperModel(model, device="cpu", compute_type="int8")

    def run(audio):
        segs, _ = m.transcribe(audio, language="de", beam_size=3,
                               word_timestamps=words, condition_on_previous_text=True,
                               vad_filter=True)
        return " ".join(s.text for s in segs), None
    return run


def mlx_peak_mb():
    try:
        import mlx.core as mx
        f = getattr(mx, "get_peak_memory", None) or mx.metal.get_peak_memory
        return f() / 1e6
    except Exception:
        return None


def _timing_percentiles(samples):
    if not samples:
        return None
    return {
        key: round(float(np.median([sample[key] for sample in samples])), 3)
        for key in samples[0]
    }


def run_one(tier, parquet, n, buf=12.0, words=True, vad=False, profile=False):
    import jiwer

    backend, model = TIERS[tier]
    data = load_fleurs(parquet, n)
    rss0 = peak_rss_mb()

    t0 = time.time()
    run = make_transcriber(backend, model, words, vad=vad, profile=profile)
    run(data[0][0])  # 首次调用含加载/编译；开 VAD 时不能用纯静音预热
    load_s = time.time() - t0

    refs, hyps, audio_s, proc_s = [], [], 0.0, 0.0
    for audio, ref in data:
        t = time.time()
        hyp, _timing = run(audio)
        proc_s += time.time() - t
        audio_s += len(audio) / 16000
        refs.append(norm(ref))
        hyps.append(norm(hyp))
    wer = jiwer.wer(refs, hyps)

    # 流式代价：拼出 12 秒缓冲，重复调用量单次延迟
    audio_buf = np.concatenate([a for a, _ in data])[: int(16000 * buf)]
    lat, timing_samples = [], []
    for _ in range(8):
        t = time.time()
        _text, timing = run(audio_buf)
        lat.append(time.time() - t)
        if timing is not None:
            timing_samples.append(timing)

    return {
        "tier": tier, "buf_s": buf, "words": words, "vad": vad, "profile": profile,
        "backend": backend, "model": model, "n": len(data),
        "wer": round(wer, 4), "rtf": round(proc_s / audio_s, 3),
        "audio_s": round(audio_s, 1), "load_s": round(load_s, 1),
        "lat_p50": round(float(np.median(lat)), 3),
        "lat_p90": round(float(np.percentile(lat, 90)), 3),
        "lat_max": round(max(lat), 3),
        "timing_p50_s": _timing_percentiles(timing_samples),
        "peak_rss_mb": round(peak_rss_mb() - 0, 0), "rss_before_mb": round(rss0, 0),
        "mlx_peak_mb": mlx_peak_mb(),
        "samples": [{"ref": r, "hyp": h} for r, h in list(zip(refs, hyps))[:5]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--out", default="asr_results.jsonl")
    ap.add_argument("--tiers", default=",".join(TIERS))
    ap.add_argument("--one")
    ap.add_argument("--buf", type=float, default=12.0)
    ap.add_argument("--no-words", action="store_true")
    ap.add_argument("--vad", action="store_true", help="按生产路径启用 VAD（会改变历史 WER 口径）")
    ap.add_argument("--profile", action="store_true", help="记录 MLX 分阶段耗时（仅基准模式）")
    a = ap.parse_args()

    if a.one:
        print(json.dumps(run_one(a.one, a.parquet, a.n, a.buf, not a.no_words, a.vad, a.profile), ensure_ascii=False))
        return

    for tier in a.tiers.split(","):
        print(f"== {tier}", file=sys.stderr, flush=True)
        p = subprocess.run([sys.executable, __file__, "--one", tier,
                            "--parquet", a.parquet, "--n", str(a.n), "--buf", str(a.buf)]
                           + (["--no-words"] if a.no_words else [])
                           + (["--vad"] if a.vad else [])
                           + (["--profile"] if a.profile else []),
                           capture_output=True, text=True)
        line = p.stdout.strip().splitlines()[-1] if p.stdout.strip() else ""
        if p.returncode != 0 or not line.startswith("{"):
            rec = {"tier": tier, "error": p.stderr[-800:]}
        else:
            rec = json.loads(line)
        print(json.dumps({k: v for k, v in rec.items() if k != "samples"}, ensure_ascii=False),
              file=sys.stderr, flush=True)
        with open(a.out, "a") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
