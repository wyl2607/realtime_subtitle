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

# ⚠️ 导入顺序是生死攸关的：torch 的 c10.dll 必须在任何 PyQt5 导入【之前】
# 完成初始化，否则 OSError WinError 1114（DLL初始化例程失败），实测100%复现。
# 现在窗口先显示、模型在后台线程加载（translator 构造被推迟了），所以这里
# 显式先 import torch 固定 DLL 顺序——别把它挪到 PyQt5 之后，也别删
import torch  # noqa: F401
from translator_queue import WhisperQueueTranslator
from audio_capture import AudioCapture, PAUSE_FLAG_FILE, STOP_FLAG_FILE
from subtitle_window import SubtitleWindow
from settings_window import MODE_ICONS as _MODE_ICON
from PyQt5.QtCore import QTimer
import config

class SubtitleApp:
    """实时字幕应用主类"""
    
    def __init__(self):
        """初始化应用：只建悬浮窗（几秒内可见），模型加载推到后台线程。

        之前是 翻译器(加载模型10-25秒)→窗口→音频 全串行，双击启动后半分钟
        屏幕上什么都没有，用户以为没启动。现在窗口先出现并显示加载进度，
        重活在 _load_models 里做完再接线。"""
        self._print_header()

        print("🔧 正在初始化组件...")
        self.running = False
        self.translator = None
        self.audio_capture = None

        # 这台机器的"正常档位"翻译模型：必须在任何模式切换之前拍快照
        # （此时 config_local 的显存分档已经生效）。非性能模式统一切回它
        self._baseline_ollama_model = config.OLLAMA_MODEL

        # 初始化字幕窗口（必须在主线程；QApplication 也在这里面建）
        try:
            self.subtitle_window = SubtitleWindow()
        except Exception as e:
            print(f"❌ 字幕窗口初始化失败: {e}")
            sys.exit(1)

        # 模式记账：窗口构造时已经从持久化状态恢复了面板高亮，这里必须读那个
        # 值而不是无条件置 None——否则重启后第一次按 Ctrl+Alt+G，"之前的模式"
        # 会被记成兜底的「直播」而不是屏幕上实际高亮的那个
        self._current_mode = self.subtitle_window.settings_window._active_preset
        self._mode_before_perf = None
        # 面板四个按钮 / 手动拨滑块 → 都回到 _apply_mode 这唯一入口
        self.subtitle_window.settings_window.on_mode_change = self._apply_mode

    def _load_models(self):
        """后台线程：加载 Whisper/Ollama + 音频采集，完成后接线并启动。

        窗口的 status 提示 5 秒自动清除，加载要十几秒，所以循环重发；
        这里所有对窗口的调用都走 Qt 信号（show_status 等），线程安全。"""
        import threading
        done = threading.Event()

        def _ticker():
            msg = "⏳ 正在加载识别/翻译模型（约10-30秒）…"
            self.subtitle_window.show_status(msg)
            while not done.wait(4):
                if self._closing:
                    return  # 退出中别再往正在拆的窗口发信号
                self.subtitle_window.show_status(msg)

        threading.Thread(target=_ticker, daemon=True, name="LoadTicker").start()
        # 用户在加载的十几秒里随时可能点❌/跑停止脚本（stop()在主线程执行）。
        # 屏障策略：每个"会启动新活动"的步骤前都重查 _closing；translator 的
        # 线程池是非daemon线程，凡是建出来了就必须 shutdown，否则进程退不掉
        # （残余毫秒级窗口由停止脚本的超时强杀+Ollama卸载兜底，不追求零竞态）
        translator = None
        try:
            # 翻译器（Whisper 模型 + Ollama 预热，启动耗时大头）
            translator = WhisperQueueTranslator()
            if self._closing:
                translator.shutdown()
                return

            # 接线：识别提交的德语立即上屏（live行），翻译完成变句对
            # 两个回调都走Qt信号，从任何线程调都安全
            translator.on_display = self.subtitle_window.update_live
            translator.on_pair = self.subtitle_window.add_pair
            translator.on_draft = self.subtitle_window.update_draft
            translator.on_status = self.subtitle_window.show_status
            self.translator = translator  # 此后 stop() 会负责 shutdown 它
            # 点词查词：窗口点击→translator查Ollama→回调线程安全地弹结果
            self.subtitle_window.on_lookup = lambda word, ctx: self.translator.lookup_word(
                word, ctx, self.subtitle_window.show_lookup_result)
            # 🤖 背景总结 / 点词深度解释：同样走 _lookup_executor，不占翻译队列
            self.subtitle_window.on_ai_analysis = (
                lambda text, cb: self.translator.analyze_background(text, cb))
            self.subtitle_window.on_deep_explain = (
                lambda sentence, cb: self.translator.deep_explain(sentence, cb))

            # 音频捕获（设备名/设备切换提示直接上悬浮窗）。构造要开WASAPI流，
            # 有几百毫秒窗口，之后再查一次 _closing 才真正启动采集/热键
            audio_capture = AudioCapture(
                callback=self.on_audio_received,
                on_status=self.subtitle_window.show_status)
            if self._closing:
                audio_capture.stop()
                return
            self.audio_capture = audio_capture

            print("✅ 所有组件初始化完成")
            self.running = True
            self.audio_capture.start()
            self._print_usage()
            self._setup_hotkey()
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            import traceback
            traceback.print_exc()
            # 失败也要把已建的线程池/模型收掉：不收的话 Whisper 白占显存、
            # 非daemon线程拖住进程退出（shutdown 重入无害，stop()再调也没事）
            if translator is not None:
                try:
                    translator.shutdown()
                except Exception:
                    pass
                self.translator = None
                self.subtitle_window.on_lookup = None
                self.subtitle_window.on_ai_analysis = None
                self.subtitle_window.on_deep_explain = None
            # status 5秒自清，失败信息要用live行持久显示（不会有识别来覆盖它）
            self.subtitle_window.update_live(
                f"❌ 启动失败: {e}", "详见 subtitle.err.log / subtitle.log，可发给AI排查；点❌退出")
            return
        finally:
            done.set()
        self.subtitle_window.show_status("✅ 字幕已就绪，播放德语音视频即可")
    
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

    def _apply_mode(self, name):
        """唯一的"应用模式"入口（⚙️面板按钮和 Ctrl+Alt+G 都走这里）。

        纯 setattr 赋值，不碰任何 Qt widget，所以热键线程和主线程都能直接调
        （widget 的同步由下面那句 notify_mode_applied 走信号 marshal 到主线程）。
        模式里的值全是各循环每轮现读 config 的，改属性即热生效：提交节奏下一块
        生效、beam 下一次识别生效、语域和模型下一次请求生效。

        name=None 表示"用户手动拨了滑块，现在是自定义"——只更新记账和指示器，
        一个 config 值都不动。未知模式名返回 False 且不改任何东西。
        """
        if name is None:
            self._current_mode = None
            self.subtitle_window.notify_mode_applied(None)
            return True
        presets = getattr(config, "PRESETS", {}) or {}
        if name not in presets:
            print(f"⚠️ 未知模式: {name}")
            return False
        for key, val in presets[name].items():
            setattr(config, key, val)
        # 「性能」换轻量翻译模型；其它模式切回启动时的基线（不是写死的值——
        # 不同机器 config_local 里的 OLLAMA_MODEL 不一样，写死会覆盖显存分档）
        target_model = (getattr(config, "GAME_MODE_OLLAMA_MODEL", None) if name == "性能"
                        else self._baseline_ollama_model)
        if target_model and target_model != config.OLLAMA_MODEL:
            old_model = config.OLLAMA_MODEL
            config.OLLAMA_MODEL = target_model
            # ☠️ 模型还在后台加载时（窗口先显示的那十几秒）translator 是 None，
            # 这时只改 config 就够了：翻译器构造后自然用新名字，退出时的
            # _unload_our_models 也会把两个模型名都覆盖到，不会漏显存
            if self.translator is not None:
                self.translator.request_warm_model(old_model=old_model, new_model=target_model)
        self._current_mode = name
        self.subtitle_window.notify_mode_applied(name)
        self.subtitle_window.show_status(f"{_MODE_ICON.get(name, '⚙️')} 已切换到「{name}」模式")
        print(f"🎬 [模式] {name}: 节奏{config.CHUNK_SUBMIT_SECONDS}s "
              f"beam{config.WHISPER_BEAM_SIZE} "
              f"草稿{'开' if config.DRAFT_TRANSLATION else '关'} "
              f"语域{config.TRANSLATION_STYLE} 模型{config.OLLAMA_MODEL}")
        return True

    def _toggle_perf_hotkey(self):
        """Ctrl+Alt+G：跳到「性能」模式；已经在性能模式则跳回进入前那个模式
        （没记录过就回默认「直播」）。"""
        if self._current_mode == "性能":
            target = self._mode_before_perf or "直播"
        else:
            self._mode_before_perf = self._current_mode or "直播"
            target = "性能"
        self._apply_mode(target)

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
            4: ("Ctrl+Alt+G", ord('G'), self._toggle_perf_hotkey),
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
        """停止脚本写 .stop 文件后，在此优雅退出（关线程池/模型，再退 Qt）。

        不看 self.running：模型还在后台加载时（running 还没置 True）
        停止脚本也应该能把程序关掉，而不是干等 3 秒被强杀"""
        if self._closing:
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
        """启动应用：主线程立刻进 Qt 事件循环（窗口马上可见），
        模型/音频在后台线程加载完自行启动（_load_models）"""
        print("\n🚀 正在启动应用...")
        self._closing = False
        # 启动前清掉残留停止标记，避免立刻又退
        try:
            if os.path.exists(STOP_FLAG_FILE):
                os.remove(STOP_FLAG_FILE)
        except OSError:
            pass

        # 后台加载模型（daemon：用户加载期间退出时不挡进程结束）
        import threading
        threading.Thread(target=self._load_models, daemon=True, name="ModelLoader").start()

        # 尾句兜底定时器（跑在Qt主线程）。0.5秒一次：除了收尾 flush，它还负责
        # 放行"被扣留等下文"的句尾（config.SENTENCE_HOLD_SEC），粒度太粗会让
        # 一段话的最后一句多等半秒。忙的时候 request_flush 只是一次 early-return
        self._flush_timer = QTimer()
        self._flush_timer.timeout.connect(self._flush_check)
        self._flush_timer.start(500)

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
        """停止应用（模型加载中途退出时 translator/audio 可能还是 None）"""
        if getattr(self, '_closing', False):
            return
        self._closing = True

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
        if self.audio_capture is not None:
            try:
                self.audio_capture.stop()
            except Exception as e:
                print(f"⚠️  停止音频捕获时出错: {e}")

        # 优雅地关闭识别/翻译线程（translator内部先ASR后翻译）
        if self.translator is not None:
            print("   - 正在关闭识别与翻译线程...")
            self.translator.shutdown()

        # 清掉停止标记（若仍在）
        try:
            if os.path.exists(STOP_FLAG_FILE):
                os.remove(STOP_FLAG_FILE)
        except OSError:
            pass

        # 清掉 pid 文件：stop脚本会删，但❌按钮/.stop标记/Ctrl+C退出不经过
        # stop脚本——残留的旧PID被系统回收复用给别的进程后，start脚本会误判
        # "已经在运行中"拒绝启动（2026-07-17 实测撞上）。注意 pid 文件记的是
        # venv 启动器存根的 PID（我们的父进程），不是 os.getpid()，所以只能
        # 按路径删、不做内容比对
        try:
            pid_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "subtitle.pid")
            if os.path.exists(pid_file):
                os.remove(pid_file)
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
        print(f"   - 翻译语域: {getattr(config, 'TRANSLATION_STYLE', '（默认）')}")
        print(f"   - 快捷键: Ctrl+Alt+P 暂停/继续, Ctrl+Alt+L 切换源语言,")
        print(f"             Ctrl+Alt+M 鼠标穿透, Ctrl+Alt+G 跳「⚡性能」模式")
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
