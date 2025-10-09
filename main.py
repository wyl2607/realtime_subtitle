"""
主程序入口
整合所有模块，协调工作流程
"""
import warnings
import logging
import sys
import os
import time
from threading import Thread
from concurrent.futures import ThreadPoolExecutor

# 在导入其他模块前先禁用所有警告和日志
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

# 完全禁用所有日志
logging.basicConfig(level=logging.CRITICAL)
for logger_name in ['root', 'transformers', 'bitsandbytes']:
    logging.getLogger(logger_name).setLevel(logging.CRITICAL)
    logging.getLogger(logger_name).disabled = True

from translator_queue import WhisperQueueTranslator
from audio_capture import AudioCapture
from subtitle_window import SubtitleWindow
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
        
        # 翻译任务控制
        self.is_translating = False
        self.skipped_count = 0
        
        # 字幕历史管理
        self.subtitle_history = []  # 存储历史字幕
        
        self.running = False
        print("✅ 所有组件初始化完成")
    
    def _print_header(self):
        """打印启动标题"""
        print("\n" + "=" * 60)
        print(" " * 15 + "🎬 实时字幕软件 v2.0")
        print(" " * 12 + "基于 Faster-Whisper")
        print("=" * 60)
        print()
    
    def on_audio_received(self, audio_data):
        """
        音频接收回调函数
        将翻译任务提交到线程池，避免阻塞音频处理线程
        
        Args:
            audio_data: numpy array, float32, shape=(n_samples,)
        """
        if not self.running:
            return

        # 每次处理音频块
        if config.SHOW_PERFORMANCE:
            audio_duration = len(audio_data) / config.SAMPLE_RATE
            print(f"\n📦 处理音频块: {audio_duration:.2f}秒")
        
        # 提交翻译任务到线程池，不阻塞当前线程
        future = self.translator_executor.submit(self.translator.translate, audio_data)
        # 添加一个回调，当翻译完成后更新UI
        future.add_done_callback(self.on_translation_completed)

    def on_translation_completed(self, future):
        """
        翻译完成后的回调（在线程池的线程中执行）
        """
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
        
        # 优雅地关闭线程池
        print("   - 正在关闭翻译器线程...")
        self.translator_executor.shutdown(wait=True, cancel_futures=False)
        
        # 停止音频捕获
        try:
            self.audio_capture.stop()
        except Exception as e:
            print(f"⚠️  停止音频捕获时出错: {e}")
        
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
