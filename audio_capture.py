"""
系统音频捕获模块
使用WASAPI Loopback捕获系统音频输出
使用动态音频块处理（借鉴whisper_streaming）
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
        
        # 动态块处理参数
        print(f"🎤 音频捕获模块已初始化（动态分句）")
        print(f"   最小时长: {config.MIN_AUDIO_DURATION}秒")
        print(f"   最大时长: {config.MAX_AUDIO_DURATION}秒")
        print(f"   静音阈值: {config.SILENCE_DURATION}秒")
        
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
        """拼接语音缓冲并提交到处理队列（队列满则丢弃，避免阻塞捕获）

        随音频附带段尾时间戳，翻译端用它算两段音频的真实间隔
        （不能用处理时刻算，那会把识别耗时也混进"静音时长"里）
        """
        audio_data = np.concatenate(speech_buffer)
        try:
            self.audio_queue.put((audio_data, time.time()), timeout=0.1)
        except queue.Full:
            print("⚠️  处理队列已满，丢弃一段音频（识别/翻译可能跟不上）")

    def _capture_loop(self):
        """音频捕获循环（在独立线程中运行）- 动态分句"""
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

            # 简化的音频缓冲管理 - 基于简单能量阈值
            # speech_buffer 保存numpy数组列表（避免逐样本extend成Python list的开销）
            speech_buffer = []  # 语音缓冲区（numpy数组列表）
            speech_samples = 0  # 缓冲区累计样本数
            silence_frames = 0  # 连续静音帧计数
            is_speaking = False  # 是否在说话

            print(f"🎵 开始捕获音频... (简化模式: {config.MIN_AUDIO_DURATION}-{config.MAX_AUDIO_DURATION}秒)")

            while self.running:
                try:
                    # 读取音频块（必须持续读，否则WASAPI缓冲区会溢出）
                    data = stream.read(config.CHUNK_SIZE, exception_on_overflow=False)

                    # 暂停：丢弃这一块，不做VAD/识别，保持流健康但不占用GPU
                    if os.path.exists(PAUSE_FLAG_FILE):
                        speech_buffer = []
                        speech_samples = 0
                        silence_frames = 0
                        is_speaking = False
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

                    # 计算当前块的能量（RMS）
                    energy = np.sqrt(np.mean(audio_chunk ** 2))

                    # 简单的语音活动检测
                    is_speech = energy > config.ENERGY_THRESHOLD_SPEECH

                    if is_speech:
                        # 检测到语音
                        if not is_speaking:
                            print(f"🎤 检测到语音开始...")
                            is_speaking = True

                        speech_buffer.append(audio_chunk)
                        speech_samples += len(audio_chunk)
                        silence_frames = 0  # 重置静音计数

                    else:
                        # 静音帧
                        if is_speaking:
                            silence_frames += 1
                            speech_buffer.append(audio_chunk)  # 继续收集（包含停顿）
                            speech_samples += len(audio_chunk)

                            # 计算静音阈值（实时读取config）
                            silence_threshold = int(config.SILENCE_DURATION * config.SAMPLE_RATE / config.CHUNK_SIZE)

                            # 静音时间足够长 → 语音结束
                            if silence_frames >= silence_threshold:
                                min_frames = int(config.MIN_AUDIO_DURATION * config.SAMPLE_RATE)
                                if speech_samples >= min_frames:
                                    print(f"✅ 语音片段完成: {speech_samples / config.SAMPLE_RATE:.2f}秒")
                                    self._submit_audio(speech_buffer)

                                # 重置状态
                                speech_buffer = []
                                speech_samples = 0
                                silence_frames = 0
                                is_speaking = False
                                print(f"🔇 语音结束")

                    # 检查最大时长限制
                    max_frames = int(config.MAX_AUDIO_DURATION * config.SAMPLE_RATE)
                    if is_speaking and speech_samples >= max_frames:
                        print(f"⚠️ 达到最大时长，强制提交: {speech_samples / config.SAMPLE_RATE:.2f}秒")
                        self._submit_audio(speech_buffer)

                        # 重置状态
                        speech_buffer = []
                        speech_samples = 0
                        silence_frames = 0
                        is_speaking = False

                except Exception as e:
                    if self.running:
                        print(f"⚠️  音频读取错误: {e}")
                    time.sleep(0.1)

            # 处理最后的音频（如果还有）
            min_frames = int(config.MIN_AUDIO_DURATION * config.SAMPLE_RATE)
            if speech_samples >= min_frames:
                print(f"📤 处理最后的音频片段: {speech_samples / config.SAMPLE_RATE:.2f}秒")
                self._submit_audio(speech_buffer)

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