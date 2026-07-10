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
# 停止标志：stop_subtitles.ps1 创建后，主程序优雅退出（先关模型再退）
STOP_FLAG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".stop")

class AudioCapture:
    """系统音频捕获器"""

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

        print(f"🎤 音频捕获模块已初始化（连续流式提交）")
        print(f"   提交节奏: {config.CHUNK_SUBMIT_SECONDS}秒/块")
        print(f"   静音门: 能量 < {config.ENERGY_THRESHOLD_SPEECH}")
        pref = (getattr(config, "LOOPBACK_DEVICE_NAME", "") or "").strip()
        if pref:
            print(f"   指定设备名包含: {pref}")

    @staticmethod
    def _resolve_loopback(p):
        """解析要打开的 WASAPI loopback 设备。

        config.LOOPBACK_DEVICE_NAME 为空 → 系统默认播放设备的 loopback；
        非空 → 设备名（不区分大小写）包含该子串的第一个 loopback。
        找不到匹配时回退默认并打印警告。
        """
        preferred = (getattr(config, "LOOPBACK_DEVICE_NAME", "") or "").strip().lower()
        default = p.get_default_wasapi_loopback()
        if not preferred:
            return default

        matches = []
        try:
            for dev in p.get_loopback_device_info_generator():
                name = dev.get("name") or ""
                if preferred in name.lower():
                    matches.append(dev)
        except Exception:
            # 旧版 pyaudiowpatch 没有 generator 时，暴力扫设备表
            for i in range(p.get_device_count()):
                try:
                    dev = p.get_device_info_by_index(i)
                except Exception:
                    continue
                name = (dev.get("name") or "")
                if "loopback" in name.lower() and preferred in name.lower():
                    matches.append(dev)

        if matches:
            return matches[0]
        print(f"⚠️  未找到名称包含「{preferred}」的 loopback 设备，回退系统默认: {default.get('name')}")
        return default

    @staticmethod
    def _current_desired_device_name():
        """用临时 PyAudio 查「当前应捕获」的设备名（默认或配置的名字匹配）。

        PortAudio 设备列表在 Pa_Initialize 时冻结——用打开着流的旧实例
        查不到设备变更，必须新建实例（实测每次~10-30ms，5秒查一次可忽略）。
        """
        p = pyaudio.PyAudio()
        try:
            return AudioCapture._resolve_loopback(p).get("name")
        finally:
            p.terminate()

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
        """音频捕获循环（在独立线程中运行）- 固定节奏连续提交。

        外层是"重开循环"：系统默认播放设备变了（换耳机/FiiO/HDMI），或者
        连续读流失败（设备拔了），就关掉旧流用新的默认设备重开——
        之前换设备后 loopback 悄悄失效，用户只看到"完全没字幕"。
        """
        DEVICE_CHECK_INTERVAL = 5.0   # 每几秒查一次默认设备有没有变
        MAX_READ_ERRORS = 10          # 连续读失败这么多次就重开流

        while self.running:
            p = None
            stream = None
            try:
                # 每次(重)开都用新PyAudio实例：PortAudio设备列表在初始化时
                # 冻结，旧实例看不到新的默认设备
                p = pyaudio.PyAudio()
                try:
                    default_speakers = self._resolve_loopback(p)
                except Exception as e:
                    print(f"❌ 无法获取音频设备: {e}（2秒后重试）")
                    if self.on_status:
                        self.on_status("⚠️ 找不到播放设备，等待设备可用…")
                    p.terminate()
                    p = None
                    time.sleep(2)
                    continue

                device_name = default_speakers['name']
                print(f"📢 捕获设备: {device_name}")
                print(f"   采样率: {int(default_speakers['defaultSampleRate'])} Hz")
                print(f"   声道数: {default_speakers['maxInputChannels']}")
                if self.on_status:
                    self.on_status(f"🔊 正在捕获: {device_name}")

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
                read_errors = 0
                last_device_check = time.time()

                print(f"🎵 开始捕获音频... (每{config.CHUNK_SUBMIT_SECONDS}秒提交一块)")

                while self.running:
                    # 应捕获设备变了（系统默认切换，或 LOOPBACK_DEVICE_NAME 运行时改了）→ 重开
                    if time.time() - last_device_check > DEVICE_CHECK_INTERVAL:
                        last_device_check = time.time()
                        try:
                            current = self._current_desired_device_name()
                            if current and current != device_name:
                                print(f"🔀 捕获设备变更: {device_name} → {current}，重开音频流")
                                if self.on_status:
                                    self.on_status(f"🔀 播放设备已切换: {current}")
                                break
                        except Exception:
                            pass  # 查询失败不影响正常捕获

                    try:
                        # 读取音频块（必须持续读，否则WASAPI缓冲区会溢出）
                        data = stream.read(config.CHUNK_SIZE, exception_on_overflow=False)
                        read_errors = 0

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
                        if not self.running:
                            break
                        read_errors += 1
                        print(f"⚠️  音频读取错误({read_errors}/{MAX_READ_ERRORS}): {e}")
                        if read_errors >= MAX_READ_ERRORS:
                            # 设备八成被拔了/驱动重置，重开流
                            print("🔀 连续读取失败，重开音频流")
                            if self.on_status:
                                self.on_status("⚠️ 音频设备异常，正在重新连接…")
                            break
                        time.sleep(0.1)

                # 提交最后的音频（如果还有语音且是正常退出）
                if not self.running and chunk_buffer and buffer_has_speech:
                    print(f"📤 处理最后的音频片段: {chunk_samples / config.SAMPLE_RATE:.2f}秒")
                    self._submit_audio(chunk_buffer)

            except Exception as e:
                print(f"❌ 音频捕获严重错误: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(1)  # 防错误风暴
            finally:
                # 无论如何退出/重开都释放音频资源
                try:
                    if stream is not None:
                        stream.stop_stream()
                        stream.close()
                    if p is not None:
                        p.terminate()
                except Exception:
                    pass

        print("🔇 音频流已关闭")

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
