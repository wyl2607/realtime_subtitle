"""Windows 节点安装自检：加载识别引擎、实测 rtf、写 asr_state.json。

和 install_node.sh 的 SELFCHECK_SH 同一份契约（字段 model/backend/rtf/translator，
原子写），区别只在翻译：Windows 没有系统翻译，固定走本机 Ollama（config_local.py
里的 OLLAMA_MODEL）。输出只有 KEY=VALUE 行，异常只报类名（S7）。

用法（工作目录 = 新版本根）：.venv\\Scripts\\python.exe scripts\\node\\windows\\selfcheck.py <wav>
"""

import json
import math
import os
import sys
import time
import wave
from pathlib import Path

sys.path.insert(0, os.getcwd())

import numpy as np  # noqa: E402

from realtime_subtitle import config  # noqa: E402
from realtime_subtitle.node.engines import WhisperEngine  # noqa: E402
from realtime_subtitle.node.info import ASR_STATE_FILENAME, STATE_DIR  # noqa: E402


def measure_rtf(engine, wav_path):
    with wave.open(wav_path) as w:
        if w.getframerate() != 16000 or w.getnchannels() != 1 or w.getsampwidth() != 2:
            print("RTF_NOTE=自检音频不是 16k 单声道 16bit，跳过 rtf")
            return None
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2")
    audio = pcm.astype(np.float32) / 32768.0
    dur = len(audio) / 16000
    engine.transcribe(audio, "de")  # 预热：首次调用含 CUDA 初始化，不计入
    spent = 0.0
    for _ in range(3):
        t0 = time.perf_counter()
        engine.transcribe(audio, "de")
        spent += time.perf_counter() - t0
    return round(spent / (3 * dur), 4) if dur > 0 else None


def main():
    wav_path = sys.argv[1] if len(sys.argv) > 1 else ""
    engine = WhisperEngine()
    try:
        engine.load()
    except Exception as e:  # noqa: BLE001
        print("ENGINE_LOAD_FAILED=" + type(e).__name__)
        return 3

    rtf = None
    if wav_path and Path(wav_path).exists():
        try:
            rtf = measure_rtf(engine, wav_path)
        except Exception as e:  # noqa: BLE001
            print("RTF_NOTE=识别失败（" + type(e).__name__ + "），跳过 rtf")
    else:
        print("RTF_NOTE=没有自检音频，跳过 rtf（之后每次会话结束会自动更新）")

    path = STATE_DIR / ASR_STATE_FILENAME
    if rtf is None:
        try:
            old = json.loads(path.read_text(encoding="utf-8")).get("rtf")
            if isinstance(old, int | float) and not isinstance(old, bool) and math.isfinite(old) and old >= 0:
                rtf = old
        except (OSError, ValueError, AttributeError):
            pass

    info = engine.info()
    state = {
        "model": info["model"],
        "backend": info["backend"],
        "rtf": rtf,
        "translator": "ollama:" + str(config.OLLAMA_MODEL),
    }
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_DIR / ("." + ASR_STATE_FILENAME + "." + str(os.getpid()) + ".tmp")
    tmp.write_text(json.dumps(state, allow_nan=False), encoding="utf-8")
    os.replace(tmp, path)
    print("BACKEND=" + str(info["backend"]))
    print("TRANSLATOR=" + state["translator"])
    print("RTF=" + str(rtf))
    return 0


if __name__ == "__main__":
    sys.exit(main())
