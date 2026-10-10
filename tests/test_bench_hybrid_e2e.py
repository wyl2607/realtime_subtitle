"""hybrid_e2e.sh 的纯合成测试：不运行真实 rslite。"""
import json
import os
import subprocess
import sys
import wave
from pathlib import Path


REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "bench" / "hybrid_e2e.sh"


def _write_wav(path: Path) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes(b"\0\0" * 1600)


def test_hybrid_e2e_uses_overrides_splits_logs_and_writes_json(tmp_path):
    wav = tmp_path / "concat5.wav"
    _write_wav(wav)
    refs = tmp_path / "refs.jsonl"
    refs.write_text(
        "\n".join(
            json.dumps({"id": f"{i:04d}", "ref": text}, ensure_ascii=False)
            for i, text in enumerate(["hallo welt", "wie geht es dir"])
        )
        + "\n",
        encoding="utf-8",
    )
    fake_bin = tmp_path / "rslite"
    fake_bin.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
mode=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --mode) mode="$2"; shift 2 ;;
    *) shift ;;
  esac
done
echo "route log for $mode" >&2
case "$mode" in
  local)
    printf '%s\n' \
      '{"ev":"volatile","t":1.0,"text":"Hal"}' \
      '{"ev":"final","id":1,"t":3.0,"t0":0.0,"t1":2.0,"text":"Hallo Welt"}' \
      '{"ev":"final","id":2,"t":6.0,"t0":2.1,"t1":4.0,"text":"Wie geht es dir"}'
    ;;
  auto|hybrid)
    printf '%s\n' \
      '{"ev":"volatile","t":1.0,"text":"Hal"}' \
      '{"ev":"final","id":1,"t":3.0,"t0":0.0,"t1":2.0,"text":"Hallo Welt"}' \
      '{"ev":"final","id":2,"t":6.0,"t0":2.1,"t1":4.0,"text":"Wie geht es dir"}' \
      '{"ev":"replace","id":null,"t":4.0,"t0":0.0,"t1":2.0,"text":"P5 node=mini2 replaced=1 ids=[1]"}' \
      '{"ev":"final","id":null,"t":4.1,"t0":0.0,"t1":2.0,"text":"[mini2] Hallo Welt"}' \
      '{"ev":"translation","id":null,"t":4.1,"text":"[mini2] zh"}' \
      '{"ev":"replace","id":null,"t":6.5,"t0":2.1,"t1":4.0,"text":"P5 node=mini2 replaced=1 ids=[2]"}' \
      '{"ev":"final","id":null,"t":6.6,"t0":2.1,"t1":4.0,"text":"[mini2] Wie geht es dir"}'
    ;;
esac
""",
        encoding="utf-8",
    )
    fake_bin.chmod(0o755)
    out_base = tmp_path / "results"
    env = os.environ | {
        "RSLITE_BIN": str(fake_bin),
        "RS_PY": sys.executable,
        "RS_REFS": str(refs),
        "HYBRID_E2E_RESULTS_DIR": str(out_base),
        "HYBRID_E2E_TIMESTAMP": "synthetic",
        "HYBRID_E2E_COOLDOWN_S": "0",
    }

    done = subprocess.run(
        ["bash", str(SCRIPT), str(wav)],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    summary = json.loads((out_base / "hybrid_e2e_synthetic" / "summary.json").read_text(encoding="utf-8"))
    assert summary["passed"] is True
    assert summary["c_mode"]["mode"] == "auto"
    assert "closest available" in summary["c_mode"]["note"]
    assert (out_base / "hybrid_e2e_synthetic" / "hybrid.stderr.log").read_text(encoding="utf-8").strip()
    assert all(json.loads(line)["ev"] for line in (out_base / "hybrid_e2e_synthetic" / "hybrid.jsonl").read_text(encoding="utf-8").splitlines())
