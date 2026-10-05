"""真模型流式冒烟：生产的 OnlineASRProcessor + 当前后端，模拟实时到达的音频。

音频按 CHUNK_SUBMIT_SECONDS 一块"实时"到达；识别耗时期间到达的块攒起来，
下一轮一次性塞进去（等价于 translator_queue._process_inbox 的整批合并）。
输出：流式 committed 文本对整段参考的 WER、每轮识别耗时、平均刷新间隔。

用法（仓库根）：python scripts/bench/stream_smoke.py --parquet <fleurs_de_80.parquet> --n 8
"""
import argparse
import io
import re
import sys
import time
import unicodedata
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


def norm(s):
    s = unicodedata.normalize("NFKC", s).lower()
    s = re.sub(r"[^\w\säöüß]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--n", type=int, default=8)
    a = ap.parse_args()

    import jiwer
    import pyarrow.parquet as pq
    import soundfile as sf

    import realtime_subtitle.config as config
    from realtime_subtitle.asr.backends import create_whisper_model, selected_whisper_backend
    from realtime_subtitle.asr.streaming_asr import OnlineASRProcessor

    rows = pq.read_table(a.parquet, columns=["audio", "transcription"]).slice(0, a.n).to_pylist()
    clips, refs = [], []
    for r in rows:
        audio, sr = sf.read(io.BytesIO(r["audio"]["bytes"]), dtype="float32")
        clips.append(audio)
        clips.append(np.zeros(int(sr * 0.6), dtype=np.float32))  # 句间停顿
        refs.append(r["transcription"])
    audio = np.concatenate(clips)
    total_s = len(audio) / 16000

    print(f"后端: {selected_whisper_backend()}  音频 {total_s:.1f}s")
    t0 = time.time()
    model = create_whisper_model()
    proc = OnlineASRProcessor(model)
    chunk = int(16000 * config.CHUNK_SUBMIT_SECONDS)
    blocks = [audio[i:i + chunk] for i in range(0, len(audio), chunk)]

    committed, iters, sim_clock, next_block = [], [], 0.0, 0
    while next_block < len(blocks):
        # 模拟时钟：识别期间"实时"到达的块全部进这一批
        arrived = max(next_block + 1, min(len(blocks), int(sim_clock / config.CHUNK_SUBMIT_SECONDS) + 1))
        for b in blocks[next_block:arrived]:
            proc.insert_audio_chunk(b)
        next_block = arrived
        t = time.time()
        done, _unstable = proc.process_iter()
        dt = time.time() - t
        iters.append(dt)
        sim_clock = max(sim_clock, next_block * config.CHUNK_SUBMIT_SECONDS) + dt
        if done:
            committed.append(done if isinstance(done, str) else " ".join(w for *_, w in done))
    tail = proc.finish()
    if tail:
        committed.append(tail if isinstance(tail, str) else " ".join(w for *_, w in tail))

    hyp = norm(" ".join(committed))
    ref = norm(" ".join(refs))
    print(f"总耗时 {time.time() - t0:.1f}s（首轮含 MLX 惰性加载模型）；识别 {len(iters)} 轮，"
          f"单轮 p50 {np.median(iters):.2f}s / max {max(iters):.2f}s；"
          f"模拟时钟 {sim_clock:.1f}s 消化 {total_s:.1f}s 音频（落后 {sim_clock - total_s:+.1f}s）")
    print(f"流式 WER: {jiwer.wer(ref, hyp):.4f}")
    print("REF:", ref[:300])
    print("HYP:", hyp[:300])


if __name__ == "__main__":
    main()
