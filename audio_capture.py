"""
系统音频捕获模块
使用WASAPI Loopback捕获系统音频输出
使用动态音频块处理（借鉴whisper_streaming）
"""
import numpy as np
import pyaudiowpatch as pyaudio
import librosa
import queue
import time
from threading import Thread
import config

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
    
    
    def _capture_loop(self):
        """音频捕获循环（在独立线程中运行）- 动态分句"""
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
            speech_buffer = []  # 语音缓冲区
            silence_frames = 0  # 连续静音帧计数
            is_speaking = False  # 是否在说话
            
            print(f"🎵 开始捕获音频... (简化模式: {config.MIN_AUDIO_DURATION}-{config.MAX_AUDIO_DURATION}秒)")
            
            while self.running:
                try:
                    # 读取音频块
                    data = stream.read(config.CHUNK_SIZE, exception_on_overflow=False)
                    audio_chunk = np.frombuffer(data, dtype=np.int16)
                    
                    # 转换为float32并归一化到[-1, 1]
                    audio_chunk = audio_chunk.astype(np.float32) / 32768.0
                    
                    # 如果是立体声，转换为单声道（取平均）
                    if channels == 2:
                        audio_chunk = audio_chunk.reshape(-1, 2).mean(axis=1)
                    
                    # 重采样到目标采样率（如果需要）
                    if source_rate != config.SAMPLE_RATE:
                        audio_chunk = librosa.resample(
                            audio_chunk,
                            orig_sr=source_rate,
                            target_sr=config.SAMPLE_RATE
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
                        
                        speech_buffer.extend(audio_chunk)
                        silence_frames = 0  # 重置静音计数
                    
                    else:
                        # 静音帧
                        if is_speaking:
                            silence_frames += 1
                            speech_buffer.extend(audio_chunk)  # 继续收集（包含停顿）
                            
                            # 计算静音阈值（实时读取config）
                            silence_threshold = int(config.SILENCE_DURATION * config.SAMPLE_RATE / config.CHUNK_SIZE)
                            
                            # 静音时间足够长 → 语音结束
                            if silence_frames >= silence_threshold:
                                min_frames = int(config.MIN_AUDIO_DURATION * config.SAMPLE_RATE)
                                if len(speech_buffer) >= min_frames:
                                    audio_duration = len(speech_buffer) / config.SAMPLE_RATE
                                    print(f"✅ 语音片段完成: {audio_duration:.2f}秒")
                                    
                                    # 提交音频数据
                                    audio_data = np.array(speech_buffer, dtype=np.float32)
                                    try:
                                        self.audio_queue.put(audio_data, timeout=0.1)
                                    except queue.Full:
                                        pass
                                
                                # 重置状态
                                speech_buffer = []
                                silence_frames = 0
                                is_speaking = False
                                print(f"🔇 语音结束")
                    
                    # 检查最大时长限制
                    max_frames = int(config.MAX_AUDIO_DURATION * config.SAMPLE_RATE)
                    if is_speaking and len(speech_buffer) >= max_frames:
                        audio_duration = len(speech_buffer) / config.SAMPLE_RATE
                        print(f"⚠️ 达到最大时长，强制提交: {audio_duration:.2f}秒")
                        
                        # 强制提交
                        audio_data = np.array(speech_buffer, dtype=np.float32)
                        try:
                            self.audio_queue.put(audio_data, timeout=0.1)
                        except queue.Full:
                            pass
                        
                        # 重置状态
                        speech_buffer = []
                        silence_frames = 0
                        is_speaking = False
                    
                except Exception as e:
                    if self.running:
                        print(f"⚠️  音频读取错误: {e}")
                    time.sleep(0.1)
            
            # 处理最后的音频（如果还有）
            min_frames = int(config.MIN_AUDIO_DURATION * config.SAMPLE_RATE)
            if speech_buffer and len(speech_buffer) >= min_frames:
                audio_duration = len(speech_buffer) / config.SAMPLE_RATE
                print(f"📤 处理最后的音频片段: {audio_duration:.2f}秒")
                audio_data = np.array(speech_buffer, dtype=np.float32)
                try:
                    self.audio_queue.put(audio_data, timeout=0.1)
                except queue.Full:
                    pass
            
            # 清理
            stream.stop_stream()
            stream.close()
            p.terminate()
            print("🔇 音频流已关闭")
            
        except Exception as e:
            print(f"❌ 音频捕获严重错误: {e}")
            import traceback
            traceback.print_exc()
    
    def _process_loop(self):
        """音频处理循环（在独立线程中运行）"""
        print("🔄 音频处理线程已启动")
        
        while self.running:
            try:
                # 从队列获取音频（阻塞等待）
                audio_data = self.audio_queue.get(timeout=1.0)
                
                # 调用回调函数
                if self.callback:
                    try:
                        self.callback(audio_data)
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