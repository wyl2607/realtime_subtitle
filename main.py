"""
主程序入口
整合所有模块，协调工作流程
"""
import warnings
import logging
import sys
import os

# 控制台可能默认使用非UTF-8编码（如cp1252），会导致emoji/中文print崩溃
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 单实例守卫：双开会有两路音频采集互抢+热键注册冲突+两个悬浮窗叠着。
# 必须放在重量级 import（torch/模型加载）之前——第二个实例秒退，
# 不浪费几秒加载时间和显存。句柄存模块级变量，进程活着就一直持有。
if sys.platform == "win32":
    import ctypes
    _single_instance_mutex = ctypes.windll.kernel32.CreateMutexW(
        None, False, "realtime_subtitle_single_instance")
    if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
        print("⚠️  实时字幕已经在运行了，不再启动第二个实例")
        # 启动脚本是Hidden窗口+日志重定向，print用户看不见——弹个会自动
        # 消失的提示框（MessageBoxTimeoutW：user32未文档化但十几年稳定存在）
        try:
            ctypes.windll.user32.MessageBoxTimeoutW(
                None, "实时字幕已经在运行了，没有启动第二个。",
                "实时字幕", 0x40 | 0x1000, 0, 8000)  # INFO | SYSTEMMODAL, 8秒自动关
        except Exception:
            pass
        sys.exit(0)

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
from audio_capture import AudioCapture, PAUSE_FLAG_FILE, STOP_FLAG_FILE
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
        
        # 接线：识别提交的德语立即上屏（live行），翻译完成变句对
        # 两个回调都走Qt信号，从任何线程调都安全
        self.translator.on_display = self.subtitle_window.update_live
        self.translator.on_pair = self.subtitle_window.add_pair
        self.translator.on_draft = self.subtitle_window.update_draft
        self.translator.on_status = self.subtitle_window.show_status
        # 点词查词：窗口点击→translator查Ollama→回调线程安全地弹结果
        self.subtitle_window.on_lookup = lambda word, ctx: self.translator.lookup_word(
            word, ctx, self.subtitle_window.show_lookup_result)

        # 初始化音频捕获（设备名/设备切换提示直接上悬浮窗）
        try:
            self.audio_capture = AudioCapture(
                callback=self.on_audio_received,
                on_status=self.subtitle_window.show_status)
        except Exception as e:
            print(f"❌ 音频捕获初始化失败: {e}")
            sys.exit(1)
        
        # 识别/翻译线程池都由translator自己管理（收件箱合并模式，永不丢块）
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

        # 音频进translator收件箱：识别线程忙时块在收件箱里攒着，
        # 醒来一口气全塞进缓冲只识别一遍——GPU被游戏抢走时字幕只滞后不丢词
        self.translator.enqueue_audio(audio_data, capture_time)

    def _toggle_pause(self):
        """暂停/继续（和 暂停继续字幕.bat 完全等价：切换.paused标记文件）"""
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

    def _switch_language(self):
        cycle = config.LANGUAGE_CYCLE
        try:
            idx = cycle.index(config.SOURCE_LANGUAGE)
        except ValueError:
            idx = -1  # 当前语言不在循环列表里（手改过config），切到列表第一个
        new_lang = cycle[(idx + 1) % len(cycle)]
        name = config.LANGUAGE_NAMES.get(new_lang, new_lang)
        # "清上下文+改语言"作为一个任务在识别线程内串行执行。之前是这里
        # 先改config再排清理任务——窗口期内会拿新语言参数识别缓冲里的
        # 旧语言音频，蹦出乱词
        self.translator.request_switch_language(new_lang)
        self.subtitle_window.show_status(f"🌐 源语言切换中: {name}…")
        print(f"🌐 [热键] 请求切换源语言: {name}")

    def _toggle_game_mode(self):
        """游戏模式一键降配：识别频率减半+贪心解码+关草稿中文+切轻量翻译模型。

        四个旋钮都是采集/识别/翻译循环里每轮现读 config 的，改属性即热生效
        （提交节奏下一块生效，beam下一次识别生效，模型下一次请求生效）。
        开启时保存当前值，关闭时原样恢复——用户在⚙️面板改过的值不会被
        覆盖成出厂默认。切模型后预热排进翻译线程，第一句不付冷加载费。"""
        saved = getattr(self, '_game_mode_saved', None)
        game_model = getattr(config, 'GAME_MODE_OLLAMA_MODEL', None)
        if saved is None:
            self._game_mode_saved = {
                'CHUNK_SUBMIT_SECONDS': config.CHUNK_SUBMIT_SECONDS,
                'WHISPER_BEAM_SIZE': config.WHISPER_BEAM_SIZE,
                'DRAFT_TRANSLATION': getattr(config, 'DRAFT_TRANSLATION', True),
                'OLLAMA_MODEL': config.OLLAMA_MODEL,
            }
            config.CHUNK_SUBMIT_SECONDS = config.GAME_MODE_SUBMIT_SECONDS
            config.WHISPER_BEAM_SIZE = config.GAME_MODE_BEAM_SIZE
            if config.GAME_MODE_DISABLE_DRAFT:
                config.DRAFT_TRANSLATION = False
            if game_model and game_model != config.OLLAMA_MODEL:
                config.OLLAMA_MODEL = game_model
                self.translator.request_warm_model()
            self.subtitle_window.show_status(
                "🎮 游戏模式已开启：GPU降配，字幕稍慢（Ctrl+Alt+G 恢复）")
            print(f"🎮 [热键] 游戏模式开启: 节奏{config.CHUNK_SUBMIT_SECONDS}s "
                  f"beam{config.WHISPER_BEAM_SIZE} 草稿{'关' if not config.DRAFT_TRANSLATION else '开'} "
                  f"模型{config.OLLAMA_MODEL}")
        else:
            config.CHUNK_SUBMIT_SECONDS = saved['CHUNK_SUBMIT_SECONDS']
            config.WHISPER_BEAM_SIZE = saved['WHISPER_BEAM_SIZE']
            config.DRAFT_TRANSLATION = saved['DRAFT_TRANSLATION']
            if saved['OLLAMA_MODEL'] != config.OLLAMA_MODEL:
                config.OLLAMA_MODEL = saved['OLLAMA_MODEL']
                self.translator.request_warm_model()
            self._game_mode_saved = None
            self.subtitle_window.show_status("🎮 游戏模式已关闭，恢复正常配置")
            print(f"🎮 [热键] 游戏模式关闭: 恢复节奏{config.CHUNK_SUBMIT_SECONDS}s "
                  f"beam{config.WHISPER_BEAM_SIZE} 模型{config.OLLAMA_MODEL}")

    def _setup_hotkey(self):
        """全局快捷键：Windows 原生 RegisterHotKey。

        ☠️ 之前用 keyboard 库的低级键盘钩子：keyboard.send 注入的测试事件
        能触发，但用户的物理键盘（德语QWERTZ布局系统）按了完全没反应——
        keyboard 库对非美式布局的组合键匹配不可靠是它的老毛病。
        RegisterHotKey 按虚拟键码在系统层注册，与键盘布局无关，游戏里
        也有效（仅管理员权限窗口在前台时无效——Windows UIPI 限制）。
        注册和消息循环必须在同一个线程（热键投递到注册线程的消息队列）。
        回调跑在热键线程里：文件操作+Qt信号都线程安全。
        """
        import threading
        import ctypes
        from ctypes import wintypes

        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
        WM_HOTKEY = 0x0312
        handlers = {
            1: ("Ctrl+Alt+P", ord('P'), self._toggle_pause),
            2: ("Ctrl+Alt+L", ord('L'), self._switch_language),
            3: ("Ctrl+Alt+M", ord('M'), self.subtitle_window.toggle_click_through),
            4: ("Ctrl+Alt+G", ord('G'), self._toggle_game_mode),
        }

        def hotkey_loop():
            user32 = ctypes.windll.user32
            # 记下线程id，stop()里PostThreadMessage(WM_QUIT)让循环优雅退出
            self._hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            registered = []
            for hid, (label, vk, _) in handlers.items():
                if user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
                    registered.append(hid)
                else:
                    print(f"⚠️  快捷键 {label} 注册失败（可能被其它程序占用）")
            if not registered:
                return
            print(f"⌨️  全局快捷键已注册(系统级): "
                  f"{', '.join(handlers[h][0] for h in registered)}")
            msg = wintypes.MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0:
                    break  # WM_QUIT（stop()里PostThreadMessage发来）
                if r == -1:
                    print("⚠️  热键消息循环出错，退出（GetMessage返回-1）")
                    break  # 出错返回-1：不能当"有消息"继续转，会死循环
                if msg.message == WM_HOTKEY and msg.wParam in handlers:
                    try:
                        handlers[msg.wParam][2]()
                    except Exception as e:
                        print(f"⚠️  快捷键处理错误: {e}")
            for hid in registered:
                user32.UnregisterHotKey(None, hid)

        self._hotkey_tid = None
        threading.Thread(target=hotkey_loop, name="HotkeyLoop", daemon=True).start()

    def _flush_check(self):
        """定时兜底：一段话说完后没有新音频，识别不会再被触发，
        队列里攒着的未翻译尾句由这里冲出去（忙时不插队，判断在translator里）"""
        if not self.running:
            return
        self.translator.request_flush()

    def _stop_flag_check(self):
        """停止脚本写 .stop 文件后，在此优雅退出（关线程池/模型，再退 Qt）"""
        if not self.running:
            return
        if not os.path.exists(STOP_FLAG_FILE):
            return
        try:
            os.remove(STOP_FLAG_FILE)
        except OSError:
            pass
        print("\n⏹️  收到停止请求，正在优雅退出...")
        # quit 会触发 aboutToQuit → stop()；随后事件循环结束
        self.subtitle_window.app.quit()

    def start(self):
        """启动应用"""
        print("\n🚀 正在启动应用...")
        self.running = True
        # 启动前清掉残留停止标记，避免立刻又退
        try:
            if os.path.exists(STOP_FLAG_FILE):
                os.remove(STOP_FLAG_FILE)
        except OSError:
            pass
        
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

        # 停止标记轮询（0.5s：停止.bat 体感更快）
        self._stop_timer = QTimer()
        self._stop_timer.timeout.connect(self._stop_flag_check)
        self._stop_timer.start(500)

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

        # 停掉定时器，避免向关闭中的线程池提交任务
        if hasattr(self, '_flush_timer'):
            self._flush_timer.stop()
        if hasattr(self, '_stop_timer'):
            self._stop_timer.stop()

        # 让热键线程退出消息循环并注销热键（WM_QUIT = 0x0012）
        if getattr(self, '_hotkey_tid', None):
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._hotkey_tid, 0x0012, 0, 0)

        # 先停止音频捕获，避免向已关闭的线程池提交新任务
        try:
            self.audio_capture.stop()
        except Exception as e:
            print(f"⚠️  停止音频捕获时出错: {e}")

        # 优雅地关闭识别/翻译线程（translator内部先ASR后翻译）
        print("   - 正在关闭识别与翻译线程...")
        self.translator.shutdown()

        # 清掉停止标记（若仍在）
        try:
            if os.path.exists(STOP_FLAG_FILE):
                os.remove(STOP_FLAG_FILE)
        except OSError:
            pass

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
        print(f"   - 处理模式: local agreement 增量识别 (每{config.CHUNK_SUBMIT_SECONDS}秒一块, 缓冲上限{config.BUFFER_TRIM_SEC:.0f}秒)")
        print(f"   - 收尾静音: {config.IDLE_FLUSH_SEC}秒")
        print(f"   - 翻译: Qwen + Whisper (Ollama {config.OLLAMA_MODEL})")
        print(f"   - 设备: {config.WHISPER_DEVICE.upper()}")
        print(f"   - 源语言: {config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)}")
        print(f"   - 快捷键: Ctrl+Alt+P 暂停/继续, Ctrl+Alt+L 切换源语言,")
        print(f"             Ctrl+Alt+M 鼠标穿透, Ctrl+Alt+G 游戏模式降配")
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
