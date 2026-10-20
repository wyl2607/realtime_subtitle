"""冷启动契约：import realtime_subtitle.app 不提前拉起识别/采集重栈。

2026-10-21 实测（M2 16GB）：translator_queue 带 numpy/requests ~97ms、
audio_capture 带 sounddevice ~62ms，都不参与窗口构造，却让窗口晚出现约
0.15 秒。它们现在在 _load_models 的后台线程里导入（scripts/bench/
startup_profile.py 量得到）。这条用例拦的是"顺手把 import 加回模块顶部"。
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

_FORBIDDEN_AT_IMPORT = (
    "realtime_subtitle.translate.translator_queue",
    "realtime_subtitle.capture.audio_capture",
    "sounddevice",
    "requests",
    "numpy",
    "mlx",
)


def test_importing_app_does_not_pull_recognizer_or_audio_stack():
    code = (
        "import json, sys\n"
        "import realtime_subtitle.app\n"
        "print(json.dumps(sorted(n for n in sys.modules)))\n"
    )
    env = os.environ.copy()
    env["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO_ROOT)
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    loaded = set(json.loads(done.stdout.strip().splitlines()[-1]))
    forbidden = list(_FORBIDDEN_AT_IMPORT)
    if sys.platform == "win32":
        # Windows 的 torch 预导入（DLL 顺序约定）自己会拉 numpy，那条不算泄漏
        forbidden.remove("numpy")
    leaked = [name for name in forbidden if name in loaded]
    assert not leaked, (
        "import realtime_subtitle.app 提前拉起了 " + ", ".join(leaked)
        + "；它们应该在 SubtitleApp._load_models 里延迟导入"
    )


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 上 torch 仍按 DLL 顺序预导入")
def test_importing_app_does_not_pull_torch_on_non_windows():
    code = "import sys, realtime_subtitle.app; print('torch' in sys.modules)"
    env = os.environ.copy()
    env["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO_ROOT)
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "False"


def test_app_stop_flag_constant_matches_capture_module():
    """app 里的 STOP_FLAG_FILE 是给启动路径省 import 的副本，不许和定义处走散。"""
    code = (
        "import realtime_subtitle.app as app\n"
        "import realtime_subtitle.capture.audio_capture as audio_capture\n"
        "print(app.STOP_FLAG_FILE == audio_capture.STOP_FLAG_FILE)\n"
    )
    env = os.environ.copy()
    env["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["PYTHONPATH"] = str(REPO_ROOT)
    done = subprocess.run(
        [sys.executable, "-c", code],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.strip().splitlines()[-1] == "True"
