"""导出 FLEURS 前 n 条为 rsbench 可读的 wav + refs.jsonl。"""
import argparse
import json
from pathlib import Path

import soundfile as sf

from asr_bench import load_fleurs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--parquet", required=True)
    ap.add_argument("--n", type=int, default=60)
    ap.add_argument("--wav-dir", required=True)
    ap.add_argument("--refs", required=True)
    a = ap.parse_args()

    wav_dir = Path(a.wav_dir)
    wav_dir.mkdir(parents=True, exist_ok=True)
    data = load_fleurs(a.parquet, a.n)

    with open(a.refs, "w", encoding="utf-8") as f:
        for i, (audio, ref) in enumerate(data):
            sid = f"{i:04d}"
            sf.write(wav_dir / f"{sid}.wav", audio, 16000)
            rec = {"id": sid, "ref": ref, "audio_s": round(len(audio) / 16000, 3)}
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")


if __name__ == "__main__":
    main()
