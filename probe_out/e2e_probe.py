"""Python 桌面链路端到端延迟探针：WAV 按实时节奏喂生产 WhisperQueueTranslator。

量：每句「音频里句尾时刻 → 进翻译队列 → 句对上屏(on_pair)」；每轮 ASR 耗时；
首个 on_display / on_pair 时刻；构造耗时与 RSS。
用法：python e2e_probe.py <repo> <wav> [--chunk 0.5] [--json out]
"""
import argparse, json, os, resource, sys, threading, time, wave

ap = argparse.ArgumentParser()
ap.add_argument("repo"); ap.add_argument("wav")
ap.add_argument("--json"); ap.add_argument("--gate", action="store_true"); ap.add_argument("--tail", type=float, default=8.0)
a = ap.parse_args()
sys.path.insert(0, a.repo)
os.chdir(a.repo)
os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
import numpy as np

T_IMPORT0 = time.time()
from realtime_subtitle import config
config.WHISPER_BACKEND = "mlx"
config.WHISPER_MLX_REPO = "mlx-community/whisper-large-v3-turbo"
config.SHOW_PERFORMANCE = False
from realtime_subtitle.translate import translator_queue as tq
_rp = tq.repo_path
tq.repo_path = lambda *p: (os.path.expanduser("~/projects/rs-mac-native/macos-native/.build/release/rstranslate")
                           if p[:1] == ("macos-native",) else _rp(*p))
T_IMPORT = time.time() - T_IMPORT0

with wave.open(a.wav) as w:
    sr = w.getframerate(); ch = w.getnchannels()
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
if ch > 1:
    pcm = pcm.reshape(-1, ch).mean(axis=1)
assert sr == 16000, sr

t0 = time.time()
tr = tq.WhisperQueueTranslator()
T_CTOR = time.time() - t0

ev = {"pairs": [], "display": [], "enq": [], "asr": [], "release": []}
FEED0 = [None]
tr.on_pair = lambda s, t, t0_=None, t1=None, key=None, rev=None, **k: ev["pairs"].append(
    dict(wall=time.time(), src=str(s), t0=t0_, t1=t1, n=len(s), ok=bool(t)))
tr.on_display = lambda *x, **k: ev["display"].append(time.time())
tr.on_draft = lambda *x, **k: None
tr.on_status = lambda *x, **k: None
orig_enq = tr._enqueue_sentences
def enq(sents):
    now = time.time()
    for s in sents or []:
        ev["enq"].append(dict(wall=now, src=str(s), t1=getattr(s, "t1", None)))
    return orig_enq(sents)
tr._enqueue_sentences = enq
orig_pi = tr.processor.process_iter
def pi():
    s = time.time(); r = orig_pi(); ev["asr"].append(time.time() - s)
    ev.setdefault("iters", []).append(dict(wall=time.time(), c=r[0], u=r[1], buf=tr.processor.buffer_seconds(), pend=tr.pending_text))
    return r
tr.processor.process_iter = pi
import inspect
orig_rel = tr._release_held_boundary
def rel(*args, **kwargs):
    force = kwargs.get("force", args[0] if args else False)
    lookahead = kwargs.get("lookahead", args[1] if len(args) > 1 else "")
    caller = inspect.currentframe().f_back.f_code.co_name
    now = time.time()
    held = tr._held_since
    pw = list(getattr(tr, "pending_words", []) or [])
    it = ev["iters"][-1] if ev.get("iters") else {}
    rec = dict(wall=now, force=bool(force), lookahead=str(lookahead),
               pending_text=tr.pending_text,
               sample_clock_sec=tr._sample_clock_samples / float(config.SAMPLE_RATE),
               caller=caller, held_since=held,
               held_age=(now - held) if held else None,
               pending_last_t1=(pw[-1][1] if pw else None),
               iter_idx=len(ev.get("iters", [])) - 1,
               last_c=str(it.get("c") or ""), last_u=str(it.get("u") or ""))
    ev["release"].append(rec)
    ret = orig_rel(*args, **kwargs)
    rec["released"] = bool(held) and not tr._held_since
    return ret
tr._release_held_boundary = rel

# 等模型可用（Apple 后端无需预热；MLX 已在构造里加载）
chunk = int(config.CHUNK_SUBMIT_SECONDS * sr)
FEED0[0] = time.time()
stop = threading.Event()
def flusher():
    while not stop.wait(0.5):
        tr.request_flush()
threading.Thread(target=flusher, daemon=True).start()
prev_sp = [False]; fedmap = []; fed = [0.0]
for i in range(0, len(pcm), chunk):
    target = FEED0[0] + (i + chunk) / sr
    d = target - time.time()
    if d > 0:
        time.sleep(d)
    blk = pcm[i:i + chunk]
    # 生产按采集回调小块（WASAPI 4096 帧@48k ≈ 85ms → 16k 下 ~1365 样本）判，任一块过门即算有声
    sp = any(float(np.sqrt(np.mean(blk[j:j + 1365] ** 2))) > config.ENERGY_THRESHOLD_SPEECH
             for j in range(0, len(blk), 1365))
    if a.gate and not sp and not prev_sp[0]:
        prev_sp[0] = sp; continue
    prev_sp[0] = sp
    fedmap.append((fed[0], i / sr)); fed[0] += len(blk) / sr
    tr.enqueue_audio(blk.copy(), time.time())
time.sleep(a.tail)
stop.set()

f0 = FEED0[0]
def lat(items):
    import bisect
    def orig(t):
        k = max(0, bisect.bisect_right([m[0] for m in fedmap], t) - 1)
        return fedmap[k][1] + (t - fedmap[k][0])
    xs = [it["wall"] - (f0 + orig(it["t1"])) for it in items if it.get("t1") is not None]
    return dict(n=len(xs), p50=float(np.median(xs)) if xs else None,
                p90=float(np.percentile(xs, 90)) if xs else None,
                mx=float(max(xs)) if xs else None)
out = dict(
    import_s=T_IMPORT, ctor_s=T_CTOR,
    rss_mb=resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1e6,
    backend=tr._translate_backend,
    audio_s=len(pcm) / sr,
    first_display_s=(ev["display"][0] - f0) if ev["display"] else None,
    first_pair_s=(ev["pairs"][0]["wall"] - f0) if ev["pairs"] else None,
    end_to_enqueue=lat(ev["enq"]), end_to_pair=lat(ev["pairs"]),
    pairs=len(ev["pairs"]), failed=sum(not p["ok"] for p in ev["pairs"]),
    asr_n=len(ev["asr"]), asr_p50=float(np.median(ev["asr"])), asr_p90=float(np.percentile(ev["asr"], 90)),
)
print(json.dumps(out, ensure_ascii=False, indent=1))
if a.json:
    json.dump(dict(summary=out, ev=ev, feed0=f0), open(a.json, "w"))
sys.stdout.flush()
tr.shutdown()
os._exit(0)
