"""
主程序入口
整合所有模块，协调工作流程
"""
import warnings
import logging
import sys
import os
from threading import Lock
from concurrent.futures import ThreadPoolExecutor

# 控制台可能默认使用非UTF-8编码（如cp1252），会导致emoji/中文print崩溃
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 在导入其他模块前先禁用所有警告和日志
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

# 压掉三方库的日志噪音，但保留ERROR——之前全禁到CRITICAL，
# faster-whisper的真实报错也被吞掉了，排障时什么都看不到
logging.basicConfig(level=logging.ERROR)

# ⚠️ 导入顺序是生死攸关的：translator_queue（内部加载torch）必须在
# 任何PyQt5导入【之前】。先加载Qt的DLL再初始化torch的c10.dll会直接
# OSError WinError 1114（DLL初始化例程失败），实测100%复现
from translator_queue import WhisperQueueTranslator
from audio_capture import AudioCapture, PAUSE_FLAG_FILE
from subtitle_window import SubtitleWindow
from PyQt5.QtCore import QTimer
import config

class SubtitleApp:
    """实时字幕应用主类"""
    
    def __init__(self):
        """初始化应用"""
        self._print_header()
        
        print("🔧 正在初始化组件...")
        
        # 初始化翻译器（Qwen + Whisper）
        try:
            self.translator = WhisperQueueTranslator()
        except Exception as e:
            print(f"❌ 翻译器初始化失败: {e}")
            sys.exit(1)
        
        # 初始化字幕窗口
        try:
            self.subtitle_window = SubtitleWindow()
        except Exception as e:
            print(f"❌ 字幕窗口初始化失败: {e}")
            sys.exit(1)
        
        # 初始化音频捕获（传入回调函数）
        try:
            self.audio_capture = AudioCapture(callback=self.on_audio_received)
        except Exception as e:
            print(f"❌ 音频捕获初始化失败: {e}")
            sys.exit(1)
        
        # 创建一个单线程的线程池来处理耗时的翻译任务
        self.translator_executor = ThreadPoolExecutor(max_workers=1)

        # 待处理任务计数（用于检测翻译积压导致的字幕延迟）
        self._pending_lock = Lock()
        self._pending_tasks = 0

        self.running = False
        print("✅ 所有组件初始化完成")
    
    def _print_header(self):
        """打印启动标题"""
        print("\n" + "=" * 60)
        print(" " * 15 + "🎬 实时字幕软件 v2.0")
        print(" " * 12 + "基于 Faster-Whisper")
        print("=" * 60)
        print()
    
    def on_audio_received(self, audio_data, capture_time):
        """
        音频接收回调函数
        将翻译任务提交到线程池，避免阻塞音频处理线程

        Args:
            audio_data: numpy array, float32, shape=(n_samples,)
            capture_time: 采集端提交该段音频的时刻（翻译端算真实音频间隔用）
        """
        if not self.running:
            return

        # 每次处理音频块
        if config.SHOW_PERFORMANCE:
            audio_duration = len(audio_data) / config.SAMPLE_RATE
            print(f"\n📦 处理音频块: {audio_duration:.2f}秒")

        # 提交翻译任务到线程池，不阻塞当前线程。
        # 积压设硬上限：识别速度赶不上时（比如GPU被游戏占着），无上限排队
        # 只会让字幕滞后越滚越大（实测到过35个≈半分钟延迟），不如丢块保实时
        with self._pending_lock:
            if self._pending_tasks >= 8:
                print(f"⚠️  翻译积压 {self._pending_tasks} 个任务，丢弃本块音频保实时性")
                return
            self._pending_tasks += 1
            pending = self._pending_tasks
        if pending > 5:
            print(f"⚠️  翻译积压 {pending} 个任务，字幕会有延迟（可考虑换更小的Whisper模型）")
        future = self.translator_executor.submit(self.translator.translate, audio_data, capture_time)
        # 添加一个回调，当翻译完成后更新UI
        future.add_done_callback(self.on_translation_completed)

    def _setup_hotkey(self):
        """注册全局暂停快捷键 Ctrl+Alt+P

        和 暂停继续字幕.bat 完全等价：都只是切换.paused标记文件，
        游戏全屏时不用切出来找bat。keyboard库自己起钩子线程，
        回调里只做文件操作+发Qt信号，都是线程安全的。
        """
        try:
            import keyboard
        except ImportError:
            print("⚠️  keyboard库未安装，全局暂停快捷键不可用（pip install keyboard）")
            return

        def toggle_pause():
            try:
                if os.path.exists(PAUSE_FLAG_FILE):
                    os.remove(PAUSE_FLAG_FILE)
                    self.subtitle_window.show_status("▶️ 已继续识别翻译")
                    print("▶️ [热键] 继续识别与翻译")
                else:
                    open(PAUSE_FLAG_FILE, "w").close()
                    self.subtitle_window.show_status("⏸️ 字幕已暂停（Ctrl+Alt+P 继续）")
                    print("⏸️ [热键] 已暂停识别与翻译")
            except OSError as e:
                print(f"⚠️  切换暂停状态失败: {e}")

        keyboard.add_hotkey("ctrl+alt+p", toggle_pause)
        print("⌨️  全局快捷键已注册: Ctrl+Alt+P = 暂停/继续（游戏里也能按）")

    def _flush_check(self):
        """定时兜底：一段话说完后没有新音频，translate()不会再被调用，
        队列里攒着的未翻译尾句由这里冲出去（具体判断在flush_pending里）"""
        if not self.running:
            return
        with self._pending_lock:
            if self._pending_tasks > 0:
                return  # 还有音频在排队处理，translate自己会消化
            self._pending_tasks += 1
        future = self.translator_executor.submit(self.translator.flush_pending)
        future.add_done_callback(self.on_translation_completed)

    def on_translation_completed(self, future):
        """
        翻译完成后的回调（在线程池的线程中执行）
        """
        with self._pending_lock:
            self._pending_tasks -= 1

        if not self.running:
            return

        try:
            subtitle = future.result()
            
            # 更新字幕（whisper_streaming会自动累积，我们直接显示）
            if subtitle and subtitle.strip():
                self.subtitle_window.update_subtitle(subtitle)
            # else:
            #     如果没有输出，说明还在累积，不需要提示
            
        except Exception as e:
            print(f"❌ 翻译任务执行错误: {e}")
            import traceback
            traceback.print_exc()
    
    def start(self):
        """启动应用"""
        print("\n🚀 正在启动应用...")
        self.running = True
        
        # 启动音频捕获
        try:
            self.audio_capture.start()
        except Exception as e:
            print(f"❌ 音频捕获启动失败: {e}")
            sys.exit(1)
        
        # 打印使用提示
        self._print_usage()

        # 注册全局暂停快捷键
        self._setup_hotkey()

        # 尾句兜底定时器（跑在Qt主线程，每秒检查一次）
        self._flush_timer = QTimer()
        self._flush_timer.timeout.connect(self._flush_check)
        self._flush_timer.start(1000)

        # 运行UI事件循环（阻塞，直到窗口关闭）
        try:
            # 连接退出信号
            self.subtitle_window.app.aboutToQuit.connect(self.stop)
            self.subtitle_window.run()
        except KeyboardInterrupt:
            print("\n⚠️  收到中断信号")
        finally:
            self.stop()
    
    def stop(self):
        """停止应用"""
        if not self.running:
            return
        
        print("\n⏹️  正在停止应用...")
        self.running = False

        # 停掉兜底定时器，避免向关闭中的线程池提交任务
        if hasattr(self, '_flush_timer'):
            self._flush_timer.stop()

        # 先停止音频捕获，避免向已关闭的线程池提交新任务
        try:
            self.audio_capture.stop()
        except Exception as e:
            print(f"⚠️  停止音频捕获时出错: {e}")

        # 优雅地关闭线程池
        print("   - 正在关闭翻译器线程...")
        self.translator_executor.shutdown(wait=True, cancel_futures=False)
        
        print("👋 应用已关闭，再见！")
    
    def _print_usage(self):
        """打印使用说明"""
        print("\n" + "=" * 60)
        print("✅ 应用已成功启动！")
        print("=" * 60)
        print("\n💡 使用提示：")
        print("   1. 字幕窗口会显示在屏幕底部（可拖动）")
        print("   2. 播放任何视频/音频，系统会自动捕获并翻译")
        print("   3. 支持YouTube、本地视频、音乐播放器等")
        print("   4. 点击 ➖ 按钮可最小化字幕")
        print("   5. 点击 ⚙️ 按钮可调节参数")
        print("   6. 点击 ❌ 按钮可退出程序")
        print("   7. 或按 Ctrl+C 中断程序")
        print("\n⚙️  当前配置：")
        print(f"   - Whisper模型: {config.WHISPER_MODEL}")
        print(f"   - 处理模式: 动态分句 ({config.MIN_AUDIO_DURATION}-{config.MAX_AUDIO_DURATION}秒)")
        print(f"   - 语音停顿检测: {config.SILENCE_DURATION}秒")
        print(f"   - 翻译: Qwen + Whisper (Ollama {config.OLLAMA_MODEL})")
        print(f"   - 设备: {config.WHISPER_DEVICE.upper()}")
        print("\n" + "=" * 60)
        print("🎉 开始享受实时字幕吧！")
        print("=" * 60 + "\n")

def main():
    """主函数"""
    try:
        app = SubtitleApp()
        app.start()
    except KeyboardInterrupt:
        print("\n👋 用户中断，程序退出")
    except Exception as e:
        print(f"\n❌ 程序异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
