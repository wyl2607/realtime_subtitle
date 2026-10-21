"""FLEURS 60 句经 rslite 回放的 WER 入口（v3.2.0 Done 2）。

`asr_bench.py` 只加载本机模型，不经过 rslite/Groq；这个脚本逐条把 FLEURS wav
喂给 rslite headless，收集最终文本，再用 asr_bench 的归一化和 jiwer 算 WER。
`--config-dir` 会作为 RSLITE_CONFIG_DIR 只作用于子进程，脚本自己不读写 key，
也不会碰 ~/.config/rslite。

数据集目录布局与 native_export.py 的产出一致：`<id>.wav` + `refs.jsonl`
（refs 也可以在目录的上一级，兼容 `wav/` + `refs.jsonl` 的分开放法）。

用法：
  python scripts/bench/rslite_fleurs.py --dataset ~/projects/rs-mac-native-data/wav \
      --config-dir /tmp/rslite-fleurs-config --out /tmp/rslite-fleurs
"""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import jiwer  # noqa: E402

from asr_bench import norm  # noqa: E402
from hybrid_score import check_integrity, replay_lines  # noqa: E402

UTTERANCE_TIMEOUT_S = 120


def _refs_path(dataset: Path) -> Path | None:
    for candidate in (dataset / "refs.jsonl", dataset.parent / "refs.jsonl"):
        if candidate.is_file():
            return candidate
    return None


def load_refs(dataset: Path) -> dict[str, str]:
    path = _refs_path(dataset)
    if path is None:
        raise SystemExit(f"{dataset} 下找不到 refs.jsonl")
    refs: dict[str, str] = {}
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            rec = json.loads(line)
            if rec.get("id") and rec.get("ref"):
                refs[str(rec["id"])] = str(rec["ref"])
    return refs


def load_samples(dataset: Path) -> list[dict]:
    """按 refs 顺序取音频；目录里缺的 wav 直接报错，避免静默少算样本。"""
    refs = load_refs(dataset)
    wav_dir = dataset if any(dataset.glob("*.wav")) else dataset / "wav"
    samples = []
    missing = []
    for sid, ref in sorted(refs.items()):
        wav = wav_dir / f"{sid}.wav"
        if not wav.is_file():
            missing.append(str(wav))
            continue
        samples.append({"id": sid, "wav": str(wav), "ref": ref})
    if missing:
        raise SystemExit("缺音频：\n" + "\n".join(missing))
    if not samples:
        raise SystemExit(f"{wav_dir} 下没有可回放的 FLEURS wav")
    return samples


def build_command(rslite: str, wav: str) -> list[str]:
    return [rslite, "--mode", "hybrid", "--source", f"file:{wav}", "--headless"]


def rslite_env(config_dir: str) -> dict[str, str]:
    # 只覆盖子进程的配置目录；不读、不传 key，也不改 HOME。
    env = os.environ.copy()
    env["RSLITE_CONFIG_DIR"] = str(config_dir)
    return env


def parse_events(text: str) -> list[dict]:
    """rslite headless 的 stdout 是 JSONL；stderr 混入的非 JSON 行直接跳过。"""
    events = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict) and "ev" in rec:
            events.append(rec)
    return events


def final_text(events: list[dict]) -> str:
    """按 P5/LineStore 重放后的最终文本，和 hybrid_score.py 同一口径。"""
    lines = replay_lines(events)
    return norm(" ".join(line["text"] for line in lines))


def score_sample(ref: str, events: list[dict]) -> dict:
    hyp = final_text(events)
    return {
        "hyp": hyp,
        "wer": float(jiwer.wer([norm(ref)], [hyp])),
        "problems": check_integrity(replay_lines(events), [norm(ref)]),
    }


def aggregate(items: list[dict]) -> float:
    """整体 WER 按所有句子拼起来算 corpus 级，而不是每条 WER 取平均。"""
    refs = [norm(item["ref"]) for item in items]
    hyps = [item["hyp"] for item in items]
    return float(jiwer.wer(refs, hyps))


def run_one(rslite: str, sample: dict, config_dir: str) -> dict:
    command = build_command(rslite, sample["wav"])
    done = subprocess.run(
        command,
        env=rslite_env(config_dir),
        capture_output=True,
        text=True,
        timeout=UTTERANCE_TIMEOUT_S,
    )
    events = parse_events(done.stdout)
    scored = score_sample(sample["ref"], events)
    return {
        "id": sample["id"],
        "wav": sample["wav"],
        "ref": sample["ref"],
        "hyp": scored["hyp"],
        "wer": round(scored["wer"], 4),
        "problems": scored["problems"],
        "returncode": done.returncode,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", required=True, help="FLEURS wav 目录（含 refs.jsonl）")
    ap.add_argument("--config-dir", required=True, help="作为 RSLITE_CONFIG_DIR 的隔离配置目录")
    ap.add_argument(
        "--rslite",
        default=str(Path(__file__).resolve().parents[2] / "macos-native" / ".build" / "release" / "rslite"),
    )
    ap.add_argument("--out", help="结果目录；--dry-run 时可不填")
    ap.add_argument("--dry-run", action="store_true", help="只打印将执行的命令与样本数")
    a = ap.parse_args(argv)

    dataset = Path(a.dataset).expanduser()
    samples = load_samples(dataset)
    if a.dry_run:
        print(f"samples={len(samples)} dataset={dataset}")
        for sample in samples:
            print(" ".join(build_command(a.rslite, sample["wav"])))
        return 0
    if not a.out:
        ap.error("--out 在非 --dry-run 时必填")

    items = []
    for sample in samples:
        record = run_one(a.rslite, sample, a.config_dir)
        items.append(record)
        print(f"{record['id']} wer={record['wer']:.4f} problems={record['problems']}")

    overall = aggregate(items)
    out_dir = Path(a.out).expanduser()
    out_dir.mkdir(parents=True, exist_ok=True)
    result = {
        "dataset": str(dataset),
        "config_dir": str(a.config_dir),
        "n": len(items),
        "wer": round(overall, 4),
        "items": items,
    }
    (out_dir / "results.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"overall n={len(items)} wer={overall:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
