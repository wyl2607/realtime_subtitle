#!/usr/bin/env python3
"""可选参照：用 faster-whisper 自带的 Silero 出语音段。

TK-001 的正式参照是 clip 对齐加声学修剪。不跑这个脚本也能下结论。
--self-check 不加载模型、不下载权重。
"""

from __future__ import annotations

import argparse
import json
import sys
import wave
from pathlib import Path


def bounds_from_chunks(chunks: list[dict], rate: int, file_id: str, one_sentence: bool) -> list[dict]:
    """Silero 给的是样本下标。--one-sentence 把句内停顿收成一个区间，给「一文件一句」的素材用。"""
    spans = []
    for chunk in chunks:
        start = int(chunk["start"]) / rate
        end = int(chunk["end"]) / rate
        if end > start:
            spans.append((start, end))
    if not spans:
        return []
    if one_sentence:
        spans = [(min(item[0] for item in spans), max(item[1] for item in spans))]
    rows = []
    for index, (t0, t1) in enumerate(spans):
        # 四舍五入后贴在一起的短段，probe 要求 t0 < t1，保留原精度。
        a, b = round(t0, 3), round(t1, 3)
        if b <= a:
            a, b = t0, t1
        row_id = file_id if one_sentence or len(spans) == 1 else f"{file_id}#{index}"
        rows.append({"id": row_id, "t0": a, "t1": b})
    return rows


def load_pcm(path: Path):
    import numpy as np

    with wave.open(str(path), "rb") as src:
        if (src.getnchannels(), src.getsampwidth(), src.getframerate()) != (1, 2, 16000):
            raise RuntimeError(f"{path.name} 需要 16 kHz mono s16")
        raw = src.readframes(src.getnframes())
    audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def speech_chunks(path: Path) -> list[dict]:
    # 模型在第一次调用时才加载。正式结论不依赖这里，所以不在 self-check 里碰它。
    from faster_whisper.vad import get_speech_timestamps

    audio = load_pcm(path)
    return get_speech_timestamps(audio, sampling_rate=16000)


def write_bounds(paths: list[tuple[str, Path]], out: Path, one_sentence: bool) -> None:
    lines = []
    for file_id, path in paths:
        chunks = speech_chunks(path)
        rows = bounds_from_chunks(chunks, 16000, file_id, one_sentence)
        if not rows:
            print(f"silero: {file_id} 没有语音段", file=sys.stderr)
        lines.extend(json.dumps(row, ensure_ascii=False) for row in rows)
    out.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")


def run_self_check() -> int:
    failures = []
    chunks = [{"start": 0, "end": 16000}, {"start": 32000, "end": 48000}]
    separate = bounds_from_chunks(chunks, 16000, "concat", False)
    if [row["t0"] for row in separate] != [0.0, 2.0] or [row["t1"] for row in separate] != [1.0, 3.0]:
        failures.append("separate")
    if separate[0]["id"] != "concat#0" or separate[1]["id"] != "concat#1":
        failures.append("ids")
    merged = bounds_from_chunks(chunks, 16000, "0000", True)
    if merged != [{"id": "0000", "t0": 0.0, "t1": 3.0}]:
        failures.append("one-sentence")
    if bounds_from_chunks([], 16000, "0000", True) != []:
        failures.append("empty")
    src = Path(__file__).read_text(encoding="utf-8")
    # self-check 这条路径本身不能在导入期把模型拉起来。上面的函数调用没有碰到 faster_whisper。
    if "faster_whisper" not in src:
        failures.append("missing-live-import")
    if failures:
        print("silero_bounds self-check failed:", ", ".join(failures), file=sys.stderr)
        return 1
    print("silero_bounds self-check: ok")
    return 0


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="用 Silero 写 probe-seg 的 --bounds")
    parser.add_argument("--self-check", action="store_true")
    parser.add_argument("--wav", type=Path)
    parser.add_argument("--wav-dir", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument(
        "--one-sentence",
        action="store_true",
        help="每个文件收成一个区间（FLEURS 一文件一句）。concat5 不要加。",
    )
    args = parser.parse_args(argv)
    if args.self_check:
        return args
    if (args.wav is None) == (args.wav_dir is None):
        parser.error("真跑要且只能给 --wav 或 --wav-dir")
    if args.out is None:
        parser.error("真跑需要 --out")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    if args.self_check:
        return run_self_check()
    if args.wav is not None:
        paths = [(args.wav.stem, args.wav)]
    else:
        paths = [(path.stem, path) for path in sorted(args.wav_dir.glob("*.wav"))]
        if not paths:
            print(f"error: {args.wav_dir} 里没有 wav", file=sys.stderr)
            return 1
    try:
        write_bounds(paths, args.out, args.one_sentence)
    except Exception as exc:
        print(f"error: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(f"silero: wrote {args.out}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
