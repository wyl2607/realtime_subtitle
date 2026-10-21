"""rslite_fleurs.py 的夹具测试：解析 headless 事件、算 WER，不真跑 rslite。"""
import json
import sys
from pathlib import Path

import jiwer

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from scripts.bench import rslite_fleurs as rf  # noqa: E402
from scripts.bench.asr_bench import norm  # noqa: E402


def _events(*records: dict) -> str:
    return "\n".join(json.dumps(rec, ensure_ascii=False) for rec in records)


def _cloud_utterance(text: str, local_id: int = 1) -> str:
    return _events(
        {"ev": "clock", "t": 0.0, "t0": 0.0, "text": ""},
        {"ev": "volatile", "t": 1.0, "text": text[:3]},
        {"ev": "final", "t": 1.1, "id": local_id, "text": text, "t0": 0.0, "t1": 1.2},
        {
            "ev": "replace",
            "t": 1.5,
            "text": f"P5 node=cloud:groq replaced=1 ids=[{local_id}]",
            "t0": 0.0,
            "t1": 1.2,
        },
        {"ev": "final", "t": 1.6, "id": None, "text": f"[cloud:groq] {text}", "t0": 0.0, "t1": 1.2},
    )


def test_parse_events_ignores_non_json_stderr_lines():
    text = "rslite.route select node=mini2\n" + _cloud_utterance("Hallo Welt")
    events = rf.parse_events(text)
    assert [event["ev"] for event in events] == ["clock", "volatile", "final", "replace", "final"]
    assert rf.final_text(events) == "hallo welt"


def test_cloud_final_replaces_local_line_and_scores_zero():
    events = rf.parse_events(_cloud_utterance("Hallo Welt"))
    scored = rf.score_sample("Hallo Welt", events)
    assert scored["hyp"] == "hallo welt"
    assert scored["wer"] == 0.0
    assert scored["problems"] == []


def test_duplicate_final_lines_are_reported_as_problem():
    events = rf.parse_events(_events(
        {"ev": "final", "t": 1.0, "id": 1, "text": "Guten Tag", "t0": 0.0, "t1": 0.8},
        {"ev": "final", "t": 2.0, "id": 2, "text": "Guten Tag", "t0": 1.0, "t1": 1.8},
    ))
    scored = rf.score_sample("Guten Tag", events)
    assert any(problem.startswith("重复") for problem in scored["problems"])
    assert scored["wer"] > 0


def test_aggregate_wer_uses_all_utterances():
    items = [
        {"ref": "Hallo Welt", "hyp": rf.final_text(rf.parse_events(_cloud_utterance("Hallo Welt")))},
        {
            "ref": "Guten Tag",
            "hyp": rf.final_text(rf.parse_events(_events(
                {"ev": "final", "t": 1.0, "id": 1, "text": "Guten Tag", "t0": 0.0, "t1": 0.8},
            ))),
        },
    ]
    expected = jiwer.wer(
        [norm("Hallo Welt"), norm("Guten Tag")],
        [item["hyp"] for item in items],
    )
    assert rf.aggregate(items) == expected
    assert rf.aggregate(items) == 0.0


def test_build_command_passes_isolated_config_dir():
    assert rf.build_command("/bin/rslite", "/data/0000.wav") == [
        "/bin/rslite", "--mode", "hybrid", "--source", "file:/data/0000.wav", "--headless",
    ]
    env = rf.rslite_env("/tmp/rslite-fleurs-config")
    assert env["RSLITE_CONFIG_DIR"] == "/tmp/rslite-fleurs-config"


def test_load_samples_reads_refs_and_wavs(tmp_path):
    (tmp_path / "refs.jsonl").write_text(
        json.dumps({"id": "0001", "ref": "Guten Tag"}) + "\n"
        + json.dumps({"id": "0000", "ref": "Hallo Welt"}) + "\n",
        encoding="utf-8",
    )
    for sid in ("0000", "0001"):
        (tmp_path / f"{sid}.wav").write_bytes(b"RIFF")
    samples = rf.load_samples(tmp_path)
    assert [sample["id"] for sample in samples] == ["0000", "0001"]
    assert samples[0]["wav"].endswith("0000.wav")
    assert samples[1]["ref"] == "Guten Tag"
