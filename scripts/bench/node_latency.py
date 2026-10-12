"""节点端到端延迟探针：按真实速度把一段 16k 单声道 WAV 推给节点，量每句的出字延迟。

每句两项：
  final_lag  = final 到达时刻 − 这句话音频结束（a1）在墙钟上的时刻
  tx_lag     = translation 到达时刻 − final 到达时刻
用法（仓库根）：
  python scripts/bench/node_latency.py <nodes.json 里的 id> <de.wav> [--json]
token 只从 nodes.json 指向的文件读，只进请求头。钉 Tailscale IP、不走代理。
"""
import argparse
import asyncio
import json
import os
import statistics
import sys
import time
import urllib.parse
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts" / "node"))
import cluster  # noqa: E402  复用 nodes.json 读取与 Tailscale 解析

CHUNK_S = 0.1


def wer(ref: str, hyp: str) -> float:
    import re
    import unicodedata

    import jiwer

    def norm(t):
        t = unicodedata.normalize("NFKC", t).lower()
        return re.sub(r"\s+", " ", re.sub(r"[^\w\säöüß]", " ", t)).strip()
    return round(jiwer.wer(norm(ref), norm(hyp)) * 100, 2)


async def run(node: dict, wav_path: str, ref: str | None = None) -> dict:
    import websockets

    u = urllib.parse.urlsplit(node["url"])
    ip = cluster.resolve_tailnet_ip(u.hostname or "")
    url = f"ws://{ip}:{u.port or cluster.PORT}/v2/session"
    with wave.open(wav_path) as w:
        assert w.getframerate() == 16000 and w.getnchannels() == 1 and w.getsampwidth() == 2
        pcm = w.readframes(w.getnframes())
    step = int(16000 * CHUNK_S) * 2
    finals: dict[int, dict] = {}
    async with websockets.connect(url, additional_headers={"Authorization": f"Bearer {cluster.read_token(node)}"},
                                  proxy=None, max_size=2**20) as ws:
        await ws.send(json.dumps({"type": "hello", "v": 2, "src": "de", "dst": "zh",
                                  "sample_rate": 16000, "format": "s16le"}))
        t_hello = time.perf_counter()
        ready = json.loads(await ws.recv())
        ready_s = time.perf_counter() - t_hello
        t0 = time.perf_counter()

        async def sender():
            for i, off in enumerate(range(0, len(pcm), step)):
                await asyncio.sleep(max(0.0, t0 + i * CHUNK_S - time.perf_counter()))
                await ws.send(pcm[off:off + step])
            await ws.send(json.dumps({"type": "drain"}))

        send_task = asyncio.create_task(sender())
        async for raw in ws:
            ev = json.loads(raw)
            now = time.perf_counter()
            if ev.get("ev") == "final":
                finals[ev["id"]] = {"final_lag": now - (t0 + ev["a1"]), "t": now, "seg": ev["a1"] - ev["a0"],
                                    "text": ev.get("text", "")}
            elif ev.get("ev") == "translation" and ev.get("id") in finals:
                finals[ev["id"]]["tx_lag"] = now - finals[ev["id"]]["t"]
            elif ev.get("ev") == "drained":
                break
        await send_task
    fl = [f["final_lag"] for f in finals.values()]
    tl = [f["tx_lag"] for f in finals.values() if "tx_lag" in f]

    def pct(xs, p):
        return round(sorted(xs)[min(len(xs) - 1, int(p * len(xs)))], 2) if xs else None
    out_wer = wer(ref, " ".join(finals[k]["text"] for k in sorted(finals))) if ref else None
    return {"node": node["id"], "wer_pct": out_wer, "ready_s": round(ready_s, 2), "cold": ready.get("cold"),
            "sentences": len(finals), "translated": len(tl),
            "final_lag_p50": pct(fl, 0.5), "final_lag_p90": pct(fl, 0.9),
            "tx_lag_p50": pct(tl, 0.5), "tx_lag_p90": pct(tl, 0.9),
            "total_p50": round(statistics.median([f["final_lag"] + f.get("tx_lag", 0) for f in finals.values()]), 2)
            if finals else None,
            "_texts": [finals[k]["text"] for k in sorted(finals)]}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("node_id")
    ap.add_argument("wav")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--ref", help="参考原文文件，给了就算 WER")
    ap.add_argument("--dump", help="把节点识别出的原文按句写到这个文件")
    a = ap.parse_args()
    node = next((n for n in cluster.load_nodes() if n.get("id") == a.node_id), None)
    if node is None:
        print(f"nodes.json 里没有 {a.node_id}", file=sys.stderr)
        return 2
    ref = Path(os.path.expanduser(a.ref)).read_text(encoding="utf-8") if a.ref else None
    res = asyncio.run(run(node, os.path.expanduser(a.wav), ref))
    if a.dump:
        Path(a.dump).write_text("\n".join(res.pop("_texts")), encoding="utf-8")
    else:
        res.pop("_texts")
    print(json.dumps(res, ensure_ascii=False) if a.json else
          "  ".join(f"{k}={v}" for k, v in res.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
