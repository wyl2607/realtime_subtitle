"""ScreenCaptureKit 子进程采集：协议头一行 JSON，后续为交错 f32le PCM。"""
import json
import os
from pathlib import Path
import select
import subprocess
import time
from threading import Event, Lock, Thread, current_thread

import numpy as np

from realtime_subtitle import config
from realtime_subtitle.paths import repo_path
from .base import AudioCaptureBase, PAUSE_FLAG_FILE, STOP_FLAG_FILE


PERMISSION_EXIT_CODE = 2
PERMISSION_HINT = "请在「系统设置 → 隐私与安全性 → 屏幕与系统录音」里允许（终端/本程序），然后重启字幕"
HELPER_DIR = repo_path("tools", "macos", "sc_audio_tap")


class MacOSAudioCapture(AudioCaptureBase):
    """和 WASAPI 后端使用相同回调、暂停标记和提交节奏。"""

    HEADER_TIMEOUT = 15.0
    RESTART_INITIAL = 0.5
    RESTART_MAX = 8.0

    def __init__(self, callback, on_status=None):
        super().__init__(callback, on_status)
        self._stop_event = Event()
        self._helper_lock = Lock()
        self._helper = None
        self._reset_buffer()

    def _status(self, message):
        print(message)
        if self.on_status:
            self.on_status(message)

    def _helper_command(self):
        setting = getattr(config, "MAC_AUDIO_HELPER", "")
        path = Path(setting).expanduser() if setting else Path(HELPER_DIR) / ".build/release/sc-audio-tap"
        if not path.is_absolute():
            path = Path(repo_path(str(path)))
        if not path.is_file():
            raise FileNotFoundError(
                f"找不到 macOS 音频助手: {path}；请在 {HELPER_DIR} 运行 swift build -c release")
        command = [str(path)]
        bundle_ids = getattr(config, "MAC_AUDIO_BUNDLE_IDS", "") or ""
        if not isinstance(bundle_ids, str):
            bundle_ids = ",".join(bundle_ids)
        if bundle_ids.strip():
            command += ["--bundle-ids", bundle_ids]
        return command

    @staticmethod
    def _parse_header(line):
        try:
            header = json.loads(line)
            rate, channels = header["sample_rate"], header["channels"]
            if (header["format"] != "f32le" or type(rate) is not int or
                    type(channels) is not int or not 8000 <= rate <= 192000 or not 1 <= channels <= 32):
                raise ValueError("不支持的 PCM 格式")
            return rate, channels
        except (ValueError, KeyError, TypeError, UnicodeError) as e:
            raise ValueError(f"音频助手协议头无效: {e}") from e

    @staticmethod
    def _downmix(data, channels):
        audio = np.frombuffer(data, dtype="<f4")
        if channels > 1:
            audio = audio.reshape(-1, channels).mean(axis=1)
        return audio.astype(np.float32, copy=False)

    def start(self):
        if self.running:
            return
        try:
            self._helper_command()
        except (OSError, ValueError) as e:
            self._status(f"❌ {e}")
            raise
        # 权限拒绝会自行结束采集；再次 start 前等旧回调线程退出，避免两个消费者。
        for thread in (self.capture_thread, self.process_thread):
            if thread and thread is not current_thread():
                thread.join(timeout=3)
        while not self.audio_queue.empty():
            self.audio_queue.get_nowait()
        self._stop_event.clear()
        self.running = True
        self.process_thread = Thread(target=self._process_loop, name="AudioProcess", daemon=True)
        self.capture_thread = Thread(target=self._capture_loop, name="MacOSAudioCapture", daemon=True)
        self.process_thread.start()
        self.capture_thread.start()

    def _close_helper(self):
        # ☠️ stop() 和采集线程 finally 都会来这里；持锁收割一次，不能只 terminate
        # 不 wait，否则每次崩溃重启都会留下僵尸进程。
        with self._helper_lock:
            proc = self._helper
            if proc is None:
                return
            if proc.poll() is None:
                proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
            for pipe in (proc.stdout, proc.stderr):
                if pipe:
                    pipe.close()
            self._helper = None

    def stop(self):
        self.running = False
        self._stop_event.set()
        self._close_helper()
        for thread in (self.capture_thread, self.process_thread):
            if thread and thread is not current_thread():
                thread.join(timeout=3)
        self._release_resampler()

    def _read_helper(self, proc):
        """同时排空 stdout/stderr，静音或启动挂起也能响应停止标记。"""
        streams = {proc.stdout.fileno(): "pcm", proc.stderr.fileno(): "stderr"}
        pending = bytearray()
        header = None
        deadline = time.monotonic() + self.HEADER_TIMEOUT
        for fd in streams:
            os.set_blocking(fd, False)
        while self.running and not self._stop_event.is_set():
            if os.path.exists(STOP_FLAG_FILE):
                self.running = False  # 主程序仍负责移除标记、关闭模型。
                return
            if header is None and time.monotonic() > deadline:
                raise TimeoutError("音频助手启动超时（未收到 PCM 协议头）")
            ready, _, _ = select.select(list(streams), [], [], 0.1)
            for fd in ready:
                try:
                    data = os.read(fd, 65536)
                except BlockingIOError:
                    continue
                if not data:
                    kind = streams.pop(fd)
                    if kind == "pcm":
                        # 先等子进程退出码，权限错误没有协议头，不能误判为普通 EOF。
                        proc.wait(timeout=2)
                        if pending:
                            if header is None or len(pending) % (header[1] * 4):
                                raise ValueError("音频助手在不完整的 PCM 帧中退出")
                            self._accept_block(bytes(pending), *header)
                        return
                    continue
                if streams[fd] == "stderr":
                    print(f"[sc-audio-tap] {data.decode('utf-8', errors='replace').rstrip()}")
                    continue
                pending.extend(data)
                if header is None:
                    newline = pending.find(b"\n")
                    if newline < 0:
                        if len(pending) > 4096:
                            raise ValueError("音频助手协议头超过 4096 字节")
                        continue
                    if newline > 4096:
                        raise ValueError("音频助手协议头超过 4096 字节")
                    header = self._parse_header(pending[:newline])
                    del pending[:newline + 1]
                    rate, channels = header
                    self._open_resampler(rate)
                    self._reset_buffer()
                    self._status("🔊 正在捕获: macOS 系统音频")
                rate, channels = header
                block_bytes = config.CHUNK_SIZE * channels * 4
                # ☠️ pipe 的 read 边界不是 PCM 帧边界，保留余数，不能直接 reshape。
                while len(pending) >= block_bytes:
                    block = bytes(pending[:block_bytes])
                    del pending[:block_bytes]
                    self._accept_block(block, rate, channels)
        # 不 flush 半块：崩溃/暂停后不能把旧滤波器的尾巴接到新音频里。

    def _reset_buffer(self):
        self._chunks = []
        self._samples = 0
        self._has_speech = False
        self._prev_speech = False
        self._silent_periods = 0
        self._quiet_warned = False
        self._paused = False
        self._last_pause_check = 0.0

    def _accept_block(self, data, rate, channels):
        now = time.time()
        if now - self._last_pause_check >= self.PAUSE_CHECK_INTERVAL:
            self._last_pause_check = now
            self._paused = os.path.exists(PAUSE_FLAG_FILE)
        if self._paused:
            self._chunks = []
            self._samples = 0
            self._has_speech = self._prev_speech = False
            self._silent_periods = 0
            self._quiet_warned = False
            self._open_resampler(rate)
            return
        audio = self._resample(self._downmix(data, channels))
        if audio is None:
            return
        if np.sqrt(np.mean(audio ** 2)) > config.ENERGY_THRESHOLD_SPEECH:
            self._has_speech = True
        self._chunks.append(audio)
        self._samples += len(audio)
        if self._samples >= int(config.CHUNK_SUBMIT_SECONDS * config.SAMPLE_RATE):
            if self._has_speech or self._prev_speech:
                self._submit_audio(self._chunks)
                self._silent_periods = 0
                self._quiet_warned = False
            else:
                self._silent_periods += 1
                if not self._quiet_warned and self._silent_periods * config.CHUNK_SUBMIT_SECONDS >= 60:
                    self._quiet_warned = True
                    self._status("🔇 一直没听到声音：确认正在播放且音量不是最小，或到 ⚙️ 调低「语音能量阈值」")
            self._prev_speech = self._has_speech
            self._chunks = []
            self._samples = 0
            self._has_speech = False

    def _capture_loop(self):
        delay = self.RESTART_INITIAL
        try:
            while self.running and not self._stop_event.is_set():
                proc = None
                started = time.monotonic()
                failure = "音频助手已退出"
                try:
                    with self._helper_lock:
                        if not self.running:
                            break
                        proc = subprocess.Popen(self._helper_command(), stdout=subprocess.PIPE,
                                                stderr=subprocess.PIPE, bufsize=0)
                        self._helper = proc
                    self._read_helper(proc)
                except Exception as e:
                    failure = str(e)
                finally:
                    code = proc.poll() if proc else None
                    self._close_helper()
                    if code is None and proc:
                        code = proc.returncode
                    self._release_resampler()
                if not self.running or self._stop_event.is_set():
                    break
                if code == PERMISSION_EXIT_CODE:
                    self._status(PERMISSION_HINT)
                    break
                if time.monotonic() - started >= 30:
                    delay = self.RESTART_INITIAL
                self._status(f"⚠️ macOS 音频助手异常（{failure}，退出码 {code}），{delay:g} 秒后重新连接…")
                if self._stop_event.wait(delay):
                    break
                delay = min(delay * 2, self.RESTART_MAX)
        finally:
            self.running = False
            self._close_helper()
            self._release_resampler()
            print("🔇 macOS 音频流已关闭")
