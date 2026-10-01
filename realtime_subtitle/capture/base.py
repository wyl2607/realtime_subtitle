"""音频后端共用的队列、回调线程和有状态重采样。

Windows 的读流、暂停和静音门保持原样；平台资源的生命周期由各自后端管理。
"""
import sys
import queue
import time

import numpy as np
import soxr

from realtime_subtitle import config
from realtime_subtitle.paths import repo_path

# ☠️ 和启动/停止脚本是跨进程约定，必须仍落在仓库根。
PAUSE_FLAG_FILE = repo_path(".paused")
STOP_FLAG_FILE = repo_path(".stop")


class AudioCaptureBase:
    PAUSE_CHECK_INTERVAL = 0.5
    _mac_device_warned = False

    def __init__(self, callback, on_status=None):
        """
        初始化音频捕获器

        Args:
            callback: 音频回调函数，接收numpy array (float32, [-1, 1])
            on_status: 可选的状态提示回调（设备名/设备切换，线程安全）
        """
        self.callback = callback
        self.on_status = on_status
        self.running = False
        self.audio_queue = queue.Queue(maxsize=10)  # 限制队列大小
        self.capture_thread = None
        self.process_thread = None
        # 「当前应捕获的设备名」由独立的探测线程写、采集线程读（见 _probe_loop）。
        # None = 还没探到过，采集线程按"没变化"处理
        self._desired_device_name = None
        self.probe_thread = None
        # ☠️ 重采样器挂在实例上、**不挂在采集线程的栈帧局部变量上**。
        # 采集线程是 daemon，退出时它多半正卡在 stream.read() 里（见 stop()），
        # 解释器不会展开它的栈帧 → 局部变量 resampler 一直活着 → soxr 的
        # nanobind 扩展在卸载时打印 "leaked 1 instances / CSoxr"。挂到实例上
        # 之后 stop() 能从线程外面把它放掉（见 _release_resampler）。
        self._resampler = None
        # 源采样率恰好==目标时不需要重采样器，此时 None 代表"原样透传"而不是
        # "已经放掉了"，两者对 _resample() 的含义相反，用这个标志区分
        self._passthrough = False

        print(f"🎤 音频捕获模块已初始化（连续流式提交）")
        print(f"   提交节奏: {config.CHUNK_SUBMIT_SECONDS}秒/块")
        print(f"   静音门: 能量 < {config.ENERGY_THRESHOLD_SPEECH}")
        pref = (getattr(config, "LOOPBACK_DEVICE_NAME", "") or "").strip()
        if sys.platform == "darwin":
            if pref and not AudioCaptureBase._mac_device_warned:
                AudioCaptureBase._mac_device_warned = True
                print("ℹ️ macOS 使用系统音频混音，忽略 LOOPBACK_DEVICE_NAME")
        elif pref:
            print(f"   指定设备名包含: {pref}")

    def _open_resampler(self, source_rate):
        """建一个新的 ResampleStream 并挂到实例上（源采样率==目标时不建）。"""
        self._release_resampler()
        if source_rate == config.SAMPLE_RATE:
            self._passthrough = True
            return
        self._resampler = soxr.ResampleStream(
            source_rate, config.SAMPLE_RATE, 1, dtype="float32")

    def _release_resampler(self):
        """放掉重采样器。可以从采集线程外面调（stop()），靠引用计数保证安全：
        采集线程若正在 _resample() 里，那一帧自己持有一个临时引用。"""
        r = self._resampler
        self._resampler = None
        self._passthrough = False
        if r is not None:
            try:
                r.clear()
            except Exception:
                pass

    def _resample(self, audio_chunk):
        """重采样到目标采样率。

        ☠️ 必须写成独立方法，不能在采集循环里用 `resampler = self._resampler`
        取个局部变量——那个局部变量会在循环各次迭代之间一直活着，线程卡在
        stream.read() 时它照样持有 CSoxr，等于没改。本方法的栈帧每次返回就弹掉，
        采集循环的栈帧里于是一个 soxr 对象引用都不留。

        返回 None 表示"本块丢弃"：要么流式重采样的首块还在填充滤波器没有输出，
        要么 stop() 已经把重采样器收走了（此时 self.running 也已是 False）。
        """
        r = self._resampler
        if r is None:
            # 源采样率==目标时本来就没有重采样器，原样透传；
            # 正在停止时（running 已 False）丢掉，别把未重采样的音频当 16k 提交
            return audio_chunk if self._passthrough else None
        out = r.resample_chunk(audio_chunk)
        return out if len(out) else None

    def _submit_audio(self, speech_buffer):
        """拼接缓冲并提交到处理队列（队列满则丢弃，避免阻塞捕获）

        随音频附带段尾时间戳，翻译端用它算两段音频的真实间隔
        （不能用处理时刻算，那会把识别耗时也混进"静音时长"里）
        """
        audio_data = np.concatenate(speech_buffer)
        try:
            self.audio_queue.put((audio_data, time.time()), timeout=0.1)
        except queue.Full:
            print("⚠️  处理队列已满，丢弃一段音频（识别/翻译可能跟不上）")

    def _process_loop(self):
        """音频处理循环（在独立线程中运行）"""
        print("🔄 音频处理线程已启动")

        while self.running:
            try:
                # 从队列获取音频（阻塞等待）
                audio_data, capture_time = self.audio_queue.get(timeout=1.0)

                # 调用回调函数
                if self.callback:
                    try:
                        self.callback(audio_data, capture_time)
                    except Exception as e:
                        print(f"❌ 回调函数错误: {e}")
                        import traceback
                        traceback.print_exc()

            except queue.Empty:
                continue
            except Exception as e:
                if self.running:
                    print(f"❌ 音频处理错误: {e}")
                time.sleep(0.1)

        print("🔄 音频处理线程已停止")
