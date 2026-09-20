"""采集线程的 soxr 重采样器必须能从线程外面放掉。

背景：采集线程是 daemon，而 WASAPI loopback 在**没有音频播放时不投递数据**，
用户又基本都是先暂停视频再点停止 —— 于是停止时它正卡在 stream.read() 里，
join(timeout=2) 等不到，`finally` 里的释放永远不执行。解释器退出时不展开
daemon 线程的栈帧，局部变量里那个 ResampleStream 就一直活着，soxr 的 nanobind
扩展在卸载时打印：

    nanobind: leaked 1 instances!
     - leaked type "soxr.soxr_ext.CSoxr"

归档日志里 "⏹️ 正在停止音频捕获" 之后从来没有出现过 "🔇 音频流已关闭"，
就是这条路径没跑到的直接证据；而 subtitle.err.log 里每次运行都只有这几行，
等于把"有内容=出事了"这个信号给废掉了。
"""
import subprocess
import sys
import threading

import numpy as np

from realtime_subtitle import config
from realtime_subtitle.capture.audio_capture import AudioCapture
from realtime_subtitle.paths import REPO_ROOT


def _cap():
    return AudioCapture(callback=lambda *a, **k: None)


def test_open_resampler_creates_stream_when_rate_differs():
    cap = _cap()
    cap._open_resampler(48000)
    assert cap._resampler is not None
    assert cap._passthrough is False


def test_open_resampler_uses_passthrough_when_rate_matches():
    cap = _cap()
    cap._open_resampler(config.SAMPLE_RATE)
    assert cap._resampler is None
    assert cap._passthrough is True
    chunk = np.zeros(1024, dtype=np.float32)
    assert cap._resample(chunk) is chunk


def test_resample_keeps_one_stateful_instance_across_chunks():
    """☠️ CLAUDE.md 第 4 节第 12 条：必须是有状态的 ResampleStream，
    不能每块新建/每块调无状态的 soxr.resample()（实测 90.4dB → 34.3dB）。"""
    cap = _cap()
    cap._open_resampler(48000)
    first = cap._resampler
    for _ in range(4):
        cap._resample(np.zeros(4096, dtype=np.float32))
    assert cap._resampler is first


def test_released_resampler_drops_chunks_instead_of_mislabelling_them():
    """放掉之后不能退化成"原样透传" —— 那会把 48k 的音频当 16k 提交出去。"""
    cap = _cap()
    cap._open_resampler(48000)
    cap._release_resampler()
    assert cap._resampler is None
    assert cap._resample(np.zeros(4096, dtype=np.float32)) is None


def test_stop_releases_resampler_even_when_capture_thread_is_stuck():
    """join 超时是常态而不是异常，stop() 必须自己兜底释放。"""
    cap = _cap()
    cap._open_resampler(48000)
    blocked = threading.Event()

    # 模拟卡在 stream.read() 里的采集线程：join(timeout=2) 一定等不到它
    stuck = threading.Thread(target=blocked.wait, daemon=True)
    stuck.start()
    cap.running = True
    cap.capture_thread = stuck
    try:
        cap.stop()
        assert cap._resampler is None
        assert cap._passthrough is False
        assert stuck.is_alive()          # 线程确实还卡着，释放不是靠它退出
    finally:
        blocked.set()
        stuck.join(timeout=2)


_LEAK_PROBE = """
import threading, time, numpy as np
from realtime_subtitle.capture.audio_capture import AudioCapture

cap = AudioCapture(callback=lambda *a, **k: None)
cap._open_resampler(48000)
cap._resample(np.zeros(4096, dtype=np.float32))

blocked = threading.Event()
stuck = threading.Thread(target=blocked.wait, daemon=True)
stuck.start()
cap.running = True
cap.capture_thread = stuck
cap.stop()            # 卡住的线程不会退出，靠 stop() 自己释放
"""


def test_no_nanobind_leak_on_shutdown_with_stuck_thread():
    """端到端盯住真正的症状：退出时 stderr 不能再有 nanobind 泄漏报告。

    起子进程是因为这行字由 soxr 的扩展在**解释器卸载阶段**打印，
    进程内无论如何都观察不到。
    """
    proc = subprocess.run(
        [sys.executable, "-c", _LEAK_PROBE],
        capture_output=True, text=True, timeout=120, cwd=str(REPO_ROOT),
    )
    assert "nanobind: leaked" not in proc.stderr, proc.stderr
    assert "CSoxr" not in proc.stderr, proc.stderr
