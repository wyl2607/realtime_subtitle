"""
系统音频捕获模块
使用WASAPI Loopback捕获系统音频输出

2026-07-06 重写：不再用能量VAD切"语音片段"（剧集背景音乐把能量阈值打穿，
切出来的边界也不可靠）——改为按固定节奏（CHUNK_SUBMIT_SECONDS）连续提交，
句子边界完全交给识别端的 local agreement + Whisper 内置 Silero VAD 判断。
能量阈值只剩一个用途：整块静音且上一块也静音时不提交，省GPU。
"""
import numpy as np
import pyaudiowpatch as pyaudio
import soxr
import queue
import time
import os
from threading import Thread
import config

# 暂停标志文件：存在则暂停捕获，不重启进程也能停/恢复识别与翻译
PAUSE_FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".paused")

class AudioCapture:
    """系统音频捕获器"""

    def __init__(self, callback):
        """
        初始化音频捕获器

        Args:
            callback: 音频回调函数，接收numpy array (float32, [-1, 1])
        """
        self.callback = callback
        self.running = False
        self.audio_queue = queue.Queue(maxsize=10)  # 限制队列大小
        self.capture_thread = None
        self.process_thread = None

        print(f"🎤 音频捕获模块已初始化（连续流式提交）")
        print(f"   提交节奏: {config.CHUNK_SUBMIT_SECONDS}秒/块")
        print(f"   静音门: 能量 < {config.ENERGY_THRESHOLD_SPEECH}")

    def start(self):
        """启动音频捕获"""
        if self.running:
            print("⚠️  音频捕获已在运行")
            return

        self.running = True

        # 启动捕获线程
        self.capture_thread = Thread(
            target=self._capture_loop,
            name="AudioCapture",
            daemon=True
        )
        self.capture_thread.start()

        # 启动处理线程
        self.process_thread = Thread(
            target=self._process_loop,
            name="AudioProcess",
            daemon=True
        )
        self.process_thread.start()

        print("✅ 音频捕获已启动")

    def stop(self):
        """停止音频捕获"""
        if not self.running:
            return

        print("⏹️  正在停止音频捕获...")
        self.running = False

        if self.capture_thread:
            self.capture_thread.join(timeout=2)
        if self.process_thread:
            self.process_thread.join(timeout=2)

        print("✅ 音频捕获已停止")


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

    def _capture_loop(self):
        """音频捕获循环（在独立线程中运行）- 固定节奏连续提交"""
        p = None
        stream = None
        try:
            # 初始化PyAudio
            p = pyaudio.PyAudio()

            # 获取默认音频输出设备（Loopback）
            try:
                default_speakers = p.get_default_wasapi_loopback()
            except Exception as e:
                print(f"❌ 无法获取默认音频设备: {e}")
                print("   请确保系统正在播放音频")
                return

            print(f"📢 捕获设备: {default_speakers['name']}")
            print(f"   采样率: {int(default_speakers['defaultSampleRate'])} Hz")
            print(f"   声道数: {default_speakers['maxInputChannels']}")

            # 打开音频流
            stream = p.open(
                format=pyaudio.paInt16,
                channels=default_speakers['maxInputChannels'],
                rate=int(default_speakers['defaultSampleRate']),
                input=True,
                input_device_index=default_speakers['index'],
                frames_per_buffer=config.CHUNK_SIZE
            )

            source_rate = int(default_speakers['defaultSampleRate'])
            channels = default_speakers['maxInputChannels']

            # 连续提交缓冲（numpy数组列表，避免逐样本extend的开销）
            chunk_buffer = []       # 当前积累的音频块
            chunk_samples = 0       # 累计样本数（16kHz重采样后）
            buffer_has_speech = False   # 当前缓冲里有没有超过能量门的块
            prev_had_speech = False     # 上一个提交周期有没有语音（保住词尾）

            print(f"🎵 开始捕获音频... (每{config.CHUNK_SUBMIT_SECONDS}秒提交一块)")

            while self.running:
                try:
                    # 读取音频块（必须持续读，否则WASAPI缓冲区会溢出）
                    data = stream.read(config.CHUNK_SIZE, exception_on_overflow=False)

                    # 暂停：丢弃这一块，不做识别，保持流健康但不占用GPU
                    if os.path.exists(PAUSE_FLAG_FILE):
                        chunk_buffer = []
                        chunk_samples = 0
                        buffer_has_speech = False
                        prev_had_speech = False
                        continue

                    # 转换为float32并归一化到[-1, 1]
                    audio_chunk = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0

                    # 多声道转换为单声道（取平均，兼容2声道以上的设备）
                    if channels > 1:
                        audio_chunk = audio_chunk.reshape(-1, channels).mean(axis=1)

                    # 重采样到目标采样率（如果需要）
                    # 直接用soxr（librosa底层也是它，但librosa整包import要好几秒）
                    if source_rate != config.SAMPLE_RATE:
                        audio_chunk = soxr.resample(
                            audio_chunk, source_rate, config.SAMPLE_RATE
                        )

                    # 计算当前块的能量（RMS），只做静音门用
                    energy = np.sqrt(np.mean(audio_chunk ** 2))
                    if energy > config.ENERGY_THRESHOLD_SPEECH:
                        buffer_has_speech = True

                    chunk_buffer.append(audio_chunk)
                    chunk_samples += len(audio_chunk)

                    # 攒够一个提交周期
                    if chunk_samples >= int(config.CHUNK_SUBMIT_SECONDS * config.SAMPLE_RATE):
                        # 本周期有语音，或上个周期有（把词尾的收音块也送出去）才提交；
                        # 纯静音块直接丢，识别端的flush定时器会处理收尾
                        if buffer_has_speech or prev_had_speech:
                            self._submit_audio(chunk_buffer)
                        prev_had_speech = buffer_has_speech
                        chunk_buffer = []
                        chunk_samples = 0
                        buffer_has_speech = False

                except Exception as e:
                    if self.running:
                        print(f"⚠️  音频读取错误: {e}")
                    time.sleep(0.1)

            # 处理最后的音频（如果还有语音）
            if chunk_buffer and buffer_has_speech:
                print(f"📤 处理最后的音频片段: {chunk_samples / config.SAMPLE_RATE:.2f}秒")
                self._submit_audio(chunk_buffer)

        except Exception as e:
            print(f"❌ 音频捕获严重错误: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # 无论如何退出都释放音频资源
            try:
                if stream is not None:
                    stream.stop_stream()
                    stream.close()
                if p is not None:
                    p.terminate()
                print("🔇 音频流已关闭")
            except Exception:
                pass

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
