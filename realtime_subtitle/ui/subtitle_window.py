"""
字幕悬浮窗UI模块
使用PyQt5实现置顶悬浮窗口

2026-07-06 德语先行双层显示：
- 历史区：最近几条已完成句对（德语行 + 中文行）
- live行：已提交但还没翻译完的德语（白色）+ 未稳定尾部（灰色，还可能变）
识别一提交德语就立即上屏，不用等Ollama翻译。

2026-07-14 拆分：DraggableWidget/ResizableFramelessWidget -> window_frame.py，
屏幕几何工具 -> window_geometry.py，SettingsWindow+tuning辅助 -> settings_window.py，
HistoryWindow/WordPopup -> popups.py。

2026-07-14 二轮：SubtitleWindow 主类内部再拆——悬浮工具条/resize/字号/样式表
-> window_chrome.py（WindowChromeMixin），渲染引擎+点词查词 -> subtitle_render.py
（LiveTextRenderMixin），都以mixin并入。窗口状态持久化（_load_state等+
STATE_FILE）刻意留在这——test_tuning.py monkeypatch的是本模块的STATE_FILE属性，
搬到别的文件会让函数读到那个文件自己的全局，monkeypatch失效。
"""
import sys
import json
import os
from PyQt5.QtWidgets import (
    QLabel, QApplication, QWidget, QHBoxLayout, QPushButton,
    QGraphicsOpacityEffect,
)
from PyQt5.QtCore import (
    Qt, pyqtSignal, QObject, QTimer, QPropertyAnimation, QEasingCurve,
)
import realtime_subtitle.config as config
from realtime_subtitle.paths import repo_path
from realtime_subtitle.ui.window_geometry import (
    _screen_area_at, _clamp_geo_to_area, _clamp_geo_to_any_screen,
    default_geometry, screen_scale_factor,
)
from realtime_subtitle.ui.window_frame import ResizableFramelessWidget
from realtime_subtitle.ui.settings_window import (
    SettingsWindow, MODE_ICONS,
    apply_tuning, collect_tuning, snapshot_defaults,
)
# ☠️ 这两个本模块自己不用，但 tests/test_presets.py 和 tests/test_tuning.py
# 是从**这里**拿的——有意的 re-export，别当成「没用的 import」删掉。
# ruff 的 F401 分不出 re-export 和死 import，所以要显式 noqa 标注意图；
# 判断一个名字是不是 re-export 别用 grep（跨行 import 会漏，实测栽过），
# 用 ast 扫 ImportFrom。
from realtime_subtitle.ui.settings_window import (  # noqa: F401
    TUNING_KEYS, apply_text_color,
)
from realtime_subtitle.ui.popups import HistoryWindow, WordPopup, AIAnalysisPopup
from realtime_subtitle.ui.tv_window import TVWindow
from realtime_subtitle.ui.window_chrome import WindowChromeMixin
from realtime_subtitle.ui.subtitle_render import LiveTextRenderMixin

if sys.platform == "win32":
    import ctypes

# 窗口位置/大小/字号的持久化文件（重启后恢复用户调好的布局）。
# 仓库根：uninstall.ps1 / update_subtitles.ps1 都按这个位置跟用户讲
STATE_FILE = repo_path("window_state.json")


def _atomic_write_json(path, data):
    """原子写 JSON：先写 .tmp，把当前文件挪成 .bak，最后 os.replace 替换。

    直接覆盖的话，强杀/崩溃正好撞上这次写入就会留下半截 JSON——下次启动
    布局/字号/tuning 全部退回出厂值。同盘 os.replace 是原子的；替换前那一
    瞬主文件不存在，_load_state 会读 .bak 兜底。
    """
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f)
    try:
        if os.path.exists(path):
            os.replace(path, path + ".bak")
    except OSError:
        pass  # 备份失败不该挡住正常保存
    os.replace(tmp_path, path)


def _ai_web_enabled():
    """「🌐 问更强的AI」按钮是否显示（config 里模板设空 = 关掉这个出网入口）。

    延迟 import translate.lookup，沿用本模块其它 AI 相关调用点的惯例
    （UI 模块不在导入期把翻译侧整条依赖链拉起来）。
    """
    from realtime_subtitle.translate.lookup import ai_web_enabled
    return ai_web_enabled()


class SubtitleSignals(QObject):
    """信号对象（用于线程安全的UI更新）"""
    # 注：曾有一个 update = pyqtSignal(str) + update_subtitle()/_update_text()，
    # 是 2026-07-06 双层显示重写之前的旧接口，此后零调用方，已删
    status = pyqtSignal(str)  # 状态提示（如暂停/继续），不进字幕历史
    live = pyqtSignal(str, str)  # live德语行更新 (committed, unstable)
    pair = pyqtSignal(str, str)  # 一段德语翻译完成 (german, chinese)
    draft = pyqtSignal(str)  # live德语的草稿中文（正式句对完成后被替换）
    lookup = pyqtSignal(str, str, bool)  # 点词查词结果 (word, 词典文本, 是否流式中间态)
    ai_analysis = pyqtSignal(str, int)  # 🤖 背景总结结果 (text, seq)
    deep_explain = pyqtSignal(str, int)  # 点词「深度解释」结果 (text, seq)
    toggle_ct = pyqtSignal()  # 切换鼠标穿透模式（热键线程→主线程）
    mode = pyqtSignal(object)  # 当前模式名(str)或 None(自定义)：热键线程→主线程

class SubtitleWindow(WindowChromeMixin, LiveTextRenderMixin):
    """字幕悬浮窗"""
    
    def __init__(self):
        """初始化字幕窗口"""
        # 复用已有 QApplication（测试会先建并持模块级引用，防 GC 触发 qFatal）
        self.app = QApplication.instance() or QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # 恢复上次的窗口布局（字号/不透明度/tuning 要在建字幕标签之前生效）
        self._state = self._load_state()
        # 真默认快照必须在应用 state/tuning 之前拍下，否则"恢复默认值"会记成已持久化的值
        self._defaults_snapshot = snapshot_defaults()
        if "font_size" in self._state:
            try:
                config.FONT_SIZE = int(self._state["font_size"])
            except (TypeError, ValueError):
                pass
        else:
            # 首次运行：按屏幕缩放倍率放大默认字号（笔记本几乎都是 125%/150%，
            # 像素单位的字号在那上面会小一大截，见 screen_scale_factor 的注释）
            scale = screen_scale_factor()
            if scale > 1.0:
                config.FONT_SIZE = int(config.FONT_SIZE * scale)
                config.TV_FONT_SIZE = min(int(config.TV_FONT_SIZE * scale),
                                          config.TV_FONT_SIZE_MAX)
        if "bg_opacity" in self._state:
            try:
                config.BACKGROUND_OPACITY = int(self._state["bg_opacity"])
            except (TypeError, ValueError):
                pass
        apply_tuning(self._state.get("tuning") or {})
        # 基线：启动应用后的 tuning（之后每次成功保存会刷新）
        self._state["tuning"] = collect_tuning()

        # 创建信号对象
        self.signals = SubtitleSignals()
        
        # 德语先行双层显示状态
        # 内存里多留一些句对（HISTORY_KEEP条），实际显示条数由窗口高度自适应决定：
        # 窗口拉大 → 自动多显示历史；拉小 → 只留最新的
        self.HISTORY_KEEP = 50
        self.sentence_pairs = []  # 已完成句对 [(german, chinese, timestamp)]
        self.live_committed = ""  # live行：已提交未翻译的德语
        self.live_unstable = ""   # live行：未稳定尾部（灰色）
        self.live_draft = ""      # live行的草稿中文（等正式翻译时先显示）
        self.status_line = ""     # 状态提示（暂停/切语言），内容更新或 5s 超时后清掉
        # _render 测高用的文档缓存（font/size/width 不变则复用；点词路径不用）
        self._doc_cache = None
        self._doc_cache_key = None  # (font_family, font_size, text_width)

        # 创建主容器窗口（可拖动 + 边缘拖拽缩放）
        self.container = ResizableFramelessWidget()
        self.container.setWindowTitle("实时字幕")
        self.container.setWindowFlags(
            Qt.WindowStaysOnTopHint |
            Qt.FramelessWindowHint |
            Qt.Tool
        )
        self.container.setAttribute(Qt.WA_TranslucentBackground)

        # 创建按钮样式
        button_style = """
            QPushButton {
                background-color: rgba(50, 50, 50, 180);
                color: white;
                border: 1px solid rgba(255, 255, 255, 0.3);
                border-radius: 15px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: rgba(80, 80, 80, 200);
            }
        """
        
        # 创建最小化按钮
        self.minimize_btn = QPushButton("➖")
        self.minimize_btn.setFixedSize(30, 30)
        self.minimize_btn.setStyleSheet(button_style)
        self.minimize_btn.clicked.connect(self._minimize_window)
        self.minimize_btn.setToolTip("最小化字幕")
        
        # 创建历史按钮
        self.history_btn = QPushButton("📜")
        self.history_btn.setFixedSize(30, 30)
        self.history_btn.setStyleSheet(button_style)
        self.history_btn.clicked.connect(self._toggle_history)
        self.history_btn.setToolTip("字幕历史（可滚动回看）")

        # 创建电视全屏按钮
        self.tv_btn = QPushButton("📺")
        self.tv_btn.setFixedSize(30, 30)
        self.tv_btn.setStyleSheet(button_style)
        self.tv_btn.clicked.connect(self._toggle_tv)
        self.tv_btn.setToolTip("电视全屏模式（大字滚动，Esc 退出）")

        # AI 分析：最近几分钟背景总结（本地 Ollama → 可跳网页版追问）
        self.ai_btn = QPushButton("🤖")
        self.ai_btn.setFixedSize(30, 30)
        self.ai_btn.setStyleSheet(button_style)
        self.ai_btn.clicked.connect(self._on_ai_analysis_clicked)
        self.ai_btn.setToolTip("AI 分析最近几分钟内容（本地模型 + 可跳网页版）")

        # 创建设置按钮
        self.settings_btn = QPushButton("⚙️")
        self.settings_btn.setFixedSize(30, 30)
        self.settings_btn.setStyleSheet(button_style)
        self.settings_btn.clicked.connect(self._toggle_settings)
        self.settings_btn.setToolTip("参数设置")
        
        # 创建退出按钮
        self.quit_btn = QPushButton("❌")
        self.quit_btn.setFixedSize(30, 30)
        self.quit_btn.setStyleSheet("""
            QPushButton {
                background-color: rgba(180, 50, 50, 180);
                color: white;
                border: 1px solid rgba(255, 255, 255, 0.3);
                border-radius: 15px;
                font-size: 16px;
            }
            QPushButton:hover {
                background-color: rgba(220, 60, 60, 200);
            }
        """)
        self.quit_btn.clicked.connect(self._quit_application)
        self.quit_btn.setToolTip("退出程序")
        
        # 创建字幕标签（手动铺满整个窗口，见下）
        # ☠️ 核心修复：这个窗口不能用QLayout——wordWrap的QLabel带
        # height-for-width，Qt会在事件循环里把顶层窗口强制弹回
        # "当前宽度下全部文本的排版高度"（实测resize(250)瞬间生效后被弹回
        # 731px；setSizePolicy(Ignored)也压不住hfw）。而句对自适应总把窗口
        # 填满 → 窗口只能拉大不能缩小（棘轮）。所以标签不进布局，
        # 在 _on_container_resize 里手动 setGeometry 铺满，窗口大小完全归用户
        self.window = QLabel(self.container)
        self._apply_styles()
        self.window.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.window.setWordWrap(True)
        self.window.setTextFormat(Qt.RichText)  # live行灰色尾部/双层显示需要富文本
        self.window.setText("🎬 等待音频输入...")
        # 字幕标签铺满后会挡住容器的 mouse*Event → 拖动失灵。
        # 设为鼠标穿透后：拖动/点词/滚轮都由容器统一收；点词仍用容器坐标映射回标签
        self.window.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.container.setMinimumSize(360, 140)

        # 顶部拖动条：视觉上像系统标题栏；真正拖动由 WM_NCHITTEST→HTCAPTION 完成
        # （条本身也穿透鼠标，避免再挡一层事件）
        # 属性同时挂到 container，供 nativeEvent 读 isVisible（HTCAPTION 门控）
        self.drag_bar = QLabel("⠿  实时字幕  ·  拖这里移动", self.container)
        self.container.drag_bar = self.drag_bar
        self.drag_bar.setFixedHeight(ResizableFramelessWidget.DRAG_BAR_HEIGHT)
        self.drag_bar.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.drag_bar.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.drag_bar.setStyleSheet("""
            QLabel {
                background-color: rgba(28, 28, 28, 210);
                color: rgba(220, 220, 220, 200);
                font-size: 12px;
                font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
                padding-left: 12px;
                border-top-left-radius: 8px;
                border-top-right-radius: 8px;
                border-bottom: 1px solid rgba(255, 255, 255, 0.12);
            }
        """)

        # 模式常驻指示器（左上角）：不接 _set_controls_visible 的 hover 淡入淡出，
        # 一直显示当前模式——用户要能随时一眼看出现在是哪套参数在跑
        self.mode_indicator = QLabel("⚙️ 自定义", self.container)
        self.mode_indicator.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.mode_indicator.setStyleSheet("""
            QLabel {
                background-color: rgba(20, 20, 20, 210);
                color: rgba(235, 235, 235, 230);
                font-size: 11px;
                font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
                padding: 3px 8px;
                border-radius: 4px;
                border: 1px solid rgba(255, 255, 255, 0.15);
            }
        """)

        # 鼠标穿透常驻指示器：穿透时 hover 全失效，必须无条件常显（不走 _set_controls_visible）
        self.ct_indicator = QLabel("👻 Ctrl+Alt+M 恢复", self.container)
        self.ct_indicator.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        self.ct_indicator.setStyleSheet("""
            QLabel {
                background-color: rgba(20, 20, 20, 210);
                color: rgba(235, 235, 235, 230);
                font-size: 11px;
                font-family: "Segoe UI", "Microsoft YaHei UI", sans-serif;
                padding: 3px 8px;
                border-radius: 4px;
                border: 1px solid rgba(255, 255, 255, 0.15);
            }
        """)
        self.ct_indicator.hide()

        # 按钮改成悬浮工具条：平时隐藏（看剧零遮挡），鼠标移入窗口才出现。
        # 不进布局（绝对定位在右上角），显示/隐藏不会引起文字重排
        self.btn_bar = QWidget(self.container)
        bar_layout = QHBoxLayout()
        bar_layout.setContentsMargins(0, 0, 0, 0)
        bar_layout.setSpacing(6)
        for b in (self.minimize_btn, self.history_btn, self.tv_btn,
                  self.ai_btn, self.settings_btn, self.quit_btn):
            b.setParent(self.btn_bar)
            bar_layout.addWidget(b)
        self.btn_bar.setLayout(bar_layout)
        self.btn_bar.adjustSize()

        # chrome 淡入淡出：效果/动画对象只建一次，hover 反复复用（禁每次 new）
        self._chrome_want_visible = False
        self._drag_opacity = QGraphicsOpacityEffect(self.drag_bar)
        self.drag_bar.setGraphicsEffect(self._drag_opacity)
        self._btn_opacity = QGraphicsOpacityEffect(self.btn_bar)
        self.btn_bar.setGraphicsEffect(self._btn_opacity)
        self._drag_opacity.setOpacity(0.0)
        self._btn_opacity.setOpacity(0.0)
        self._drag_fade = QPropertyAnimation(self._drag_opacity, b"opacity", self.container)
        self._btn_fade = QPropertyAnimation(self._btn_opacity, b"opacity", self.container)
        for anim in (self._drag_fade, self._btn_fade):
            anim.setEasingCurve(QEasingCurve.OutCubic)
        # 只连一条 finished：两动画时长相同，idempotent 隐藏即可
        self._btn_fade.finished.connect(self._on_chrome_fade_finished)

        # status 提示 5 秒后自动清空（单发定时器，新 status 重置）
        self._status_clear_timer = QTimer(self.container)
        self._status_clear_timer.setSingleShot(True)
        self._status_clear_timer.timeout.connect(self._clear_status)

        # 窗口大小变化：重摆工具条 + 重新计算能放下几条句对
        self.container.on_resize = self._on_container_resize
        # 鼠标移入显示工具条，移出隐藏
        self.container.on_hover = self._set_controls_visible
        # Ctrl+滚轮直接调字号（不用开设置面板）
        self.container.on_wheel = self._on_wheel
        # 窗口几何：优先用上次退出时保存的（用户拖过/缩放过就记住），没有才用config默认。
        # 按中心点落屏钳制（多屏副屏恢复不被主屏尺寸错误约束；拔显示器也能钳回）
        state = self._state
        # 首次运行没有任何保存的坐标：config 里那组绝对坐标是按开发机屏幕写的，
        # 换台机器（尤其小屏笔记本）会落到屏幕外，改成按当前屏幕现算贴底居中
        x, y, w, h = self._restore_main_geo(state)
        x, y, w, h = _clamp_geo_to_any_screen(x, y, w, h)
        self.container.setGeometry(x, y, w, h)
        # 布局持久化：停止.bat 现已优先优雅退出（aboutToQuit 会写盘）；
        # 仍用定时器兜底强杀/崩溃场景——布局变了才写盘（15秒一次）
        self._last_saved_state = None
        self._state_timer = QTimer(self.container)
        self._state_timer.timeout.connect(self._save_state_if_changed)
        self._state_timer.start(15000)
        self.app.aboutToQuit.connect(self._save_state_if_changed)
        # Alt+F4 / 任务栏关闭 → 与 ❌ 相同，走 app.quit → aboutToQuit → stop()
        # （setQuitOnLastWindowClosed=False，不接管会只关窗留僵尸进程）
        self.container.on_system_close = self._on_system_close
        self.container.show()
        # 鼠标穿透模式状态（Ctrl+Alt+M切换；须在 chrome 显隐之前初始化）
        self._click_through = False
        # 拖动条与按钮工具条同步：先亮4秒让人知道在哪，之后只在鼠标移入时出现
        self._position_chrome()
        self._set_controls_visible(True)
        QTimer.singleShot(4000, lambda: self._set_controls_visible(self.container.underMouse()))
        
        # 创建设置窗口（初始隐藏）；显示相关改动时重刷样式并重渲染字幕
        self.settings_window = SettingsWindow(
            on_font_change=self._on_display_settings_change,
            defaults=self._defaults_snapshot,
        )
        # 恢复模式高亮（不重新 apply；具体值已在 tuning 里）。
        # 指示器直接读刚恢复好的值，不重复读一遍 self._state（防两处数据源不同步）
        self.settings_window.restore_active_preset(self._state.get("active_preset"))
        self._update_mode_indicator(self.settings_window._active_preset)

        # 创建历史窗口（初始隐藏）
        self.history_window = HistoryWindow()

        # 电视全屏窗（初始隐藏）：字号/所在屏从 state 的 "tv" 段恢复
        tv_state = self._state.get("tv") or {}
        try:
            config.TV_FONT_SIZE = int(tv_state["font_size"])
        except (KeyError, TypeError, ValueError):
            pass
        self.tv_window = TVWindow()
        self.tv_window._apply_font()  # 恢复的字号要落到样式表
        si = tv_state.get("screen_index")
        self.tv_window.screen_index = si if isinstance(si, int) else None
        # 开机预热首次全屏转场（opacity=0 用户看不到）：本进程第一次
        # showFullScreen() 无论何时调用都要吃 400-500ms，挪到这里比用户点📺
        # 时才付这笔账好得多——之后 whisper/Ollama 加载本来就要几秒，
        # 这半秒钟混在启动过程里感觉不到。
        self.tv_window.warm_up()

        # ⚙️/📜 几何：有持久化就恢复（钳进当前某屏）；否则首次显示时贴字幕窗所在屏
        self._settings_ever_shown = False
        self._history_ever_shown = False
        self._settings_positioned = self._restore_aux_geo(
            self.settings_window, self._state.get("settings_geo"))
        self._history_positioned = self._restore_aux_geo(
            self.history_window, self._state.get("history_geo"))

        # 点词查词：单击字幕里的德语词 → 弹小窗显示词典解释
        self.word_popup = WordPopup()
        self.ai_analysis_popup = AIAnalysisPopup()
        self.container.on_click = self._on_label_click
        self.on_lookup = None  # (word, context) -> None，由main.py接到translator
        # AI 分析/深度解释：同样由 main 接到 translator 的 _lookup_executor 路径
        self.on_ai_analysis = None   # (german_text, callback) -> None
        self.on_deep_explain = None  # (sentence, callback) -> None
        self._last_html = ""   # 最近一次渲染的富文本（点击命中测试要按它重建排版）
        self._ai_context_text = ""  # 最近一次 🤖 请求的德文，给"问更强的AI"拼问句
        # 代数门控：连点 🤖 / 深度解释时只认最后一次结果（各自计数，弹窗不互相覆盖）
        self._ai_analysis_seq = 0
        self._deep_explain_seq = 0
        self.word_popup.on_deep_explain = self._on_word_deep_explain
        self.word_popup.on_open_web = self._open_ai_web
        self.ai_analysis_popup.on_open_web = self._open_ai_web

        # 连接信号到槽（线程安全）
        self.signals.status.connect(self._show_status)
        self.signals.live.connect(self._update_live)
        self.signals.pair.connect(self._add_pair)
        self.signals.draft.connect(self._update_draft)
        self.signals.lookup.connect(self._show_lookup)
        self.signals.ai_analysis.connect(self._show_ai_analysis)
        self.signals.deep_explain.connect(self._show_deep_explain)
        self.signals.toggle_ct.connect(self._toggle_click_through)
        self.signals.mode.connect(self._on_mode_applied)
        
        print("✅ 字幕窗口已创建")
        print(f"   位置: ({x}, {y})  大小: {w}x{h}")
        print("   💡 鼠标移入窗口时顶部出现拖动条（和普通窗口一样拖）；字幕区按住拖也可移动")
        print("   💡 鼠标移到窗口边缘/四角可拖拽缩放（窗口越大显示的历史越多）")
        print("   💡 拖动条/按钮工具条平时隐藏，鼠标移入才出现（看剧零遮挡）")
        print("   💡 若完全点不到窗口：按 Ctrl+Alt+M 关闭「鼠标穿透」（右上角会有幽灵提示）")
        print("   💡 点击 ➖ 按钮可最小化字幕")
        print("   💡 点击 ⚙️ 按钮可打开参数调节面板")
        print("   💡 点击 ❌ 按钮可退出程序")
    
    # ------------------------------------------------------------------
    # 悬浮工具条 / 标题拖动条 / 穿透指示器
    # ------------------------------------------------------------------

    @staticmethod
    def _load_state():
        """读上次的窗口布局；主文件损坏时回落 .bak。

        主文件会坏是有真实路径的：停止脚本超时会强杀进程，正好撞上 15 秒
        定时器写盘就留下半截 JSON——那时布局/字号/tuning 会全部退回出厂值。
        """
        for path in (STATE_FILE, STATE_FILE + ".bak"):
            try:
                with open(path, encoding="utf-8") as f:
                    data = json.load(f)
            except (OSError, ValueError):
                continue
            # ☠️ 只有 dict 才算数：null / [] / "x" / 123 都是【合法】JSON，
            # json.load 不会抛 ValueError，上面那个 except 接不住。以前会把
            # 非 dict 原样返回，__init__ 紧接着 self._state.get(...) 就抛
            # AttributeError，main.py 捕获后 sys.exit(1) —— 悬浮窗根本不出现，
            # 而且【绕过了 .bak 兜底】（这套兜底的全部意义就是扛住 state 损坏）。
            # 当成损坏继续找下一份即可。
            if not isinstance(data, dict):
                continue
            if path != STATE_FILE:
                print("⚠️  window_state.json 损坏，已从 .bak 恢复上一份布局")
            return data
        return {}

    @staticmethod
    def _default_main_geo():
        """首次运行/状态损坏时的兜底几何：按当前屏幕现算。

        不用 config 里那组 WINDOW_X/Y——它们是按开发机屏幕写死的绝对坐标，
        1366x768 的小笔记本上 y=750 整窗掉出屏幕（避坑清单第 24 条）。
        拿不到屏幕信息时才退回 config（无头/异常环境，聊胜于无）。
        """
        screen = QApplication.primaryScreen()
        area = screen.availableGeometry() if screen else None
        if area is not None:
            return default_geometry(area.x(), area.y(),
                                    area.width(), area.height(),
                                    screen_scale_factor())
        return (config.WINDOW_X, config.WINDOW_Y,
                config.WINDOW_WIDTH, config.WINDOW_HEIGHT + 40)

    @classmethod
    def _restore_main_geo(cls, state):
        """从持久化状态恢复主窗口几何，坏值一律退回默认布局。

        ☠️ 这四个值必须 int() 容错：同函数里的 font_size / bg_opacity 一直都有
        这层保护，唯独窗口坐标没有——window_state.json 被手改坏（用户或 AI
        助手改配置时顺手动到它）时，"abc" / null / 甚至浮点数都会让
        _clamp_geo_to_any_screen 抛 TypeError，__init__ 直接失败，main.py
        捕获后 sys.exit(1)，悬浮窗根本不出现。
        注意 .bak 兜底在这里帮不上忙：文件本身是合法 dict，_load_state 正常
        返回，坏的是里面某一个值。
        """
        if not all(k in state for k in ("x", "y", "w", "h")):
            return cls._default_main_geo()
        try:
            return (int(state["x"]), int(state["y"]),
                    int(state["w"]), int(state["h"]))
        except (TypeError, ValueError):
            print("⚠️  window_state.json 里的窗口坐标不可用，改用默认布局")
            return cls._default_main_geo()

    @staticmethod
    def _restore_aux_geo(win, geo):
        """从 [x,y,w,h] 恢复辅助窗口几何；成功返回 True（已定位）。"""
        if not geo or len(geo) != 4:
            return False
        try:
            x, y, w, h = (int(geo[0]), int(geo[1]), int(geo[2]), int(geo[3]))
        except (TypeError, ValueError):
            return False
        if w < 50 or h < 50:
            return False
        x, y, w, h = _clamp_geo_to_any_screen(x, y, w, h)
        win.setGeometry(x, y, w, h)
        return True

    def _place_window_near_container(self, win):
        """首次打开⚙️/📜：贴字幕容器所在屏，优先在其上方，整窗钳进 availableGeometry。"""
        cg = self.container.frameGeometry()
        area = _screen_area_at(cg.center())
        if area is None:
            return
        ww = max(win.width(), 200)
        wh = max(win.height(), 160)
        # 优先容器正上方
        x = cg.x()
        y = cg.y() - wh - 12
        if y < area.top():
            # 上方不够 → 右侧；右侧也不够 → 左侧；再不行左上角
            y = max(area.top(), cg.y())
            x = cg.right() + 12
            if x + ww > area.right():
                x = cg.x() - ww - 12
            if x < area.left():
                x = area.left() + 16
                y = area.top() + 16
        x, y, ww, wh = _clamp_geo_to_area(x, y, ww, wh, area)
        win.setGeometry(x, y, ww, wh)

    def _save_state_if_changed(self):
        g = self.container.geometry()
        state = {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(),
                 "font_size": config.FONT_SIZE,
                 "bg_opacity": config.BACKGROUND_OPACITY}
        # 辅助窗：本会话显示过则写当前几何；否则保留上次文件里的值（从未显示过不写新值）
        if self._settings_ever_shown:
            sg = self.settings_window.geometry()
            state["settings_geo"] = [sg.x(), sg.y(), sg.width(), sg.height()]
        elif self._state.get("settings_geo"):
            state["settings_geo"] = self._state["settings_geo"]
        if self._history_ever_shown:
            hg = self.history_window.geometry()
            state["history_geo"] = [hg.x(), hg.y(), hg.width(), hg.height()]
        elif self._state.get("history_geo"):
            state["history_geo"] = self._state["history_geo"]
        state["tv"] = {"font_size": int(config.TV_FONT_SIZE),
                       "screen_index": self.tv_window.screen_index}
        state["tuning"] = collect_tuning()
        # 当前模式名（None → JSON null）；仅显示态，重启不重放模型/beam 副作用
        active = getattr(self.settings_window, "_active_preset", None)
        state["active_preset"] = active if active else None
        if state == self._last_saved_state:
            return
        try:
            _atomic_write_json(STATE_FILE, state)
            self._last_saved_state = state
            # 同步内存中的上次值，便于后续「未再显示」时继续保留
            if "settings_geo" in state:
                self._state["settings_geo"] = state["settings_geo"]
            if "history_geo" in state:
                self._state["history_geo"] = state["history_geo"]
            if "tuning" in state:
                self._state["tuning"] = state["tuning"]
            self._state["active_preset"] = state.get("active_preset")
        except OSError:
            pass  # 存不上就下次用默认布局，不值得报错

    # ------------------------------------------------------------------
    # 线程安全的对外接口（从ASR/翻译线程调用）
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 鼠标穿透模式（Ctrl+Alt+M）
    # ------------------------------------------------------------------
    def toggle_click_through(self):
        """切换鼠标穿透（线程安全，热键线程调用）"""
        self.signals.toggle_ct.emit()

    def notify_mode_applied(self, name):
        """模式已应用（线程安全，热键线程/主线程都调这个）→ 主线程同步 UI"""
        self.signals.mode.emit(name)

    def _on_mode_applied(self, name):
        """主线程槽：真实模式名才需要把面板控件从 config 重读一遍 + 刷高亮；
        None（自定义）时 config 没变，只更新指示器文字。"""
        if name is not None:
            self.settings_window.refresh_from_config()
            self.settings_window.restore_active_preset(name)
        self._update_mode_indicator(name)

    def _update_mode_indicator(self, name):
        """悬浮窗左上角的常驻模式指示器（不随 hover 淡入淡出）。"""
        text = f"{MODE_ICONS.get(name, '⚙️')} {name}" if name else "⚙️ 自定义"
        self.mode_indicator.setText(text)
        self.mode_indicator.adjustSize()
        self._position_chrome()

    def _toggle_click_through(self):
        """穿透开：字幕窗对鼠标完全隐形（点击/滚轮全落到下面的视频/游戏上），
        适合全屏看剧不挡操作。用原生 WS_EX_TRANSPARENT——窗口本来就是
        WS_EX_LAYERED（半透明窗），加这个标志即可，不用重建窗口"""
        if sys.platform != "win32":
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x20
        hwnd = int(self.container.winId())
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        self._click_through = not self._click_through
        if self._click_through:
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT)
            # 穿透后 hover 全失效：藏掉 chrome，只留右上角常驻指示器
            self._set_controls_visible(False)
            self.ct_indicator.show()
            self._position_chrome()
            self.show_status("👻 鼠标穿透已开启：字幕窗点不到了（Ctrl+Alt+M 恢复）")
            print("👻 [热键] 鼠标穿透开启")
        else:
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_TRANSPARENT)
            self.ct_indicator.hide()
            # 恢复后按当前 hover 状态决定是否显示 chrome
            self._set_controls_visible(self.container.underMouse())
            self.show_status("🖱️ 鼠标穿透已关闭，字幕窗恢复可点击")
            print("🖱️ [热键] 鼠标穿透关闭")
        # 改扩展样式后通知系统重算：部分Windows/合成路径下不发
        # SWP_FRAMECHANGED样式会延迟生效甚至不生效
        SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020  # NOSIZE|NOMOVE|NOZORDER|NOACTIVATE|FRAMECHANGED
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)

    def _minimize_window(self):
        """最小化/恢复字幕窗口（之前点一次就永久隐藏且窗口缩成一条，改成可切换）"""
        if self.window.isVisible():
            self.window.hide()
            if config.SHOW_PERFORMANCE:
                print("   ➖ 字幕已最小化（再点一次➖恢复）")
        else:
            self.window.show()
            if config.SHOW_PERFORMANCE:
                print("   ➕ 字幕已恢复")
    
    def _toggle_settings(self):
        """切换设置窗口显示"""
        if self.settings_window.isVisible():
            self.settings_window.hide()
        else:
            if not self._settings_positioned:
                self._place_window_near_container(self.settings_window)
                self._settings_positioned = True
            self._settings_ever_shown = True
            self.settings_window.show()

    def _toggle_history(self):
        """切换历史窗口显示"""
        if self.history_window.isVisible():
            self.history_window.hide()
        else:
            if not self._history_positioned:
                self._place_window_near_container(self.history_window)
                self._history_positioned = True
            self._history_ever_shown = True
            self.history_window.show()
            sb = self.history_window.text.verticalScrollBar()
            sb.setValue(sb.maximum())

    def _toggle_tv(self):
        """切换电视全屏窗：打开时回填最近中文，开在主字幕窗不在的屏上"""
        if self.tv_window.isVisible():
            self.tv_window.hide()
        else:
            self.tv_window.backfill([zh for _, zh, _ in self.sentence_pairs])
            self.tv_window.open_fullscreen(
                avoid_center=self.container.frameGeometry().center())

    def _on_ai_analysis_clicked(self):
        """🤖：过滤最近 N 分钟德文 → 本地总结；空内容直接提示不打 Ollama。"""
        from realtime_subtitle.translate.lookup import (
            filter_recent_german_context,
            build_web_query_for_background,
        )
        german_text = filter_recent_german_context(self.sentence_pairs)
        # 弹窗锚在主字幕窗中心偏上，避免挡死最新字幕
        anchor = self.container.frameGeometry().center()
        from PyQt5.QtCore import QPoint
        anchor = QPoint(anchor.x(), self.container.frameGeometry().top() + 40)
        if not german_text:
            self.ai_analysis_popup.show_at(
                anchor,
                "🤖 最近没有足够内容可分析",
                timeout_ms=8000,
                show_deep=False,
                show_web=False,
            )
            return
        # 连点只认最后一次：旧请求回来时 _show_ai_analysis 按 seq 丢弃
        self._ai_analysis_seq += 1
        seq = self._ai_analysis_seq
        self._ai_context_text = german_text
        web_q = build_web_query_for_background(german_text)
        self.ai_analysis_popup.show_at(
            anchor,
            "🤖 <b>正在分析最近内容…</b>",
            timeout_ms=60000,  # 分析可能比查词慢，等结果期间别提前关
            show_deep=False,
            # 等本地结果时也能提前跳网页，不必干等到 30s 超时
            show_web=_ai_web_enabled(),
            web_query=web_q,
        )
        if self.on_ai_analysis:
            self.on_ai_analysis(
                german_text,
                lambda text, s=seq: self.show_ai_analysis_result(text, s),
            )
        else:
            self.show_ai_analysis_result("分析功能未接线（translator 未就绪）", seq)

    def show_ai_analysis_result(self, text, seq=None):
        """背景总结完成（线程安全）。seq 缺省时用当前代数（兼容旧接线）。"""
        if seq is None:
            seq = self._ai_analysis_seq
        self.signals.ai_analysis.emit(text or "", int(seq))

    def _show_ai_analysis(self, text, seq):
        # 过时请求：用户已发起更新的分析/上下文，别盖掉当前弹窗
        if seq != self._ai_analysis_seq:
            return
        import html as _html
        from realtime_subtitle.translate.lookup import build_web_query_for_background
        body = _html.escape(text).replace("\n", "<br>")
        web_q = build_web_query_for_background(self._ai_context_text)
        self.ai_analysis_popup.update_content(
            f"🤖 <b>背景总结</b><br>{body}",
            show_deep=False,
            show_web=_ai_web_enabled(),
            web_query=web_q,
            timeout_ms=30000,
        )

    def _on_word_deep_explain(self, context):
        """WordPopup「深度解释」按钮：先改成加载态，再丢给 translator。"""
        import html as _html
        from realtime_subtitle.translate.lookup import build_web_query_for_sentence
        ctx = (context or "").strip()
        if not ctx:
            self.word_popup.update_content(
                "🔍 没有整句上下文，无法深度解释",
                show_deep=False, show_web=False, timeout_ms=8000)
            return
        # 连点 / 换词后再点深度解释：旧结果按 seq 丢弃
        self._deep_explain_seq += 1
        seq = self._deep_explain_seq
        web_q = build_web_query_for_sentence(ctx)
        self.word_popup.update_content(
            f"🔍 <b>深度解释中…</b><br>"
            f"<span style='color:#aaa'>{_html.escape(ctx[:120])}"
            f"{'…' if len(ctx) > 120 else ''}</span>",
            show_deep=False,
            # 等本地结果时也能提前跳网页
            show_web=_ai_web_enabled(),
            context=ctx,
            web_query=web_q,
            timeout_ms=60000,
        )
        if self.on_deep_explain:
            self.on_deep_explain(
                ctx,
                lambda text, s=seq: self.show_deep_explain_result(text, s),
            )
        else:
            self.show_deep_explain_result("深度解释未接线（translator 未就绪）", seq)

    def show_deep_explain_result(self, text, seq=None):
        """深度解释完成（线程安全）。seq 缺省时用当前代数（兼容旧接线）。"""
        if seq is None:
            seq = self._deep_explain_seq
        self.signals.deep_explain.emit(text or "", int(seq))

    def _show_deep_explain(self, text, seq):
        if seq != self._deep_explain_seq:
            return
        import html as _html
        from realtime_subtitle.translate.lookup import build_web_query_for_sentence
        body = _html.escape(text).replace("\n", "<br>")
        ctx = getattr(self.word_popup, "_context", "") or ""
        self.word_popup.update_content(
            f"🔍 <b>深度解释</b><br>{body}",
            show_deep=False,
            show_web=_ai_web_enabled(),
            web_query=build_web_query_for_sentence(ctx),
            timeout_ms=30000,
        )

    def _open_ai_web(self, prompt_text):
        """跳网页版更强 AI；失败只 status 提一句，不弹阻塞框。"""
        import webbrowser
        from realtime_subtitle.translate.lookup import build_ai_web_url
        q = (prompt_text or "").strip()
        if not q:
            self.show_status("没有可追问的内容")
            return
        url = build_ai_web_url(q)
        if not url:
            # 模板被清空 = 用户主动关掉了这个唯一的出网入口，别打开浏览器主页；
            # 模板写坏了也走这条（build_ai_web_url 会在日志里说清是哪种）
            self.show_status("「问更强的AI」不可用：config 里的模板为空或格式不对（详见 subtitle.log）")
            return
        try:
            webbrowser.open(url)
        except Exception as e:
            self.show_status(f"无法打开网页: {e}")

    def _quit_application(self):
        """退出程序"""
        from PyQt5.QtWidgets import QMessageBox
        
        # 确认对话框
        reply = QMessageBox.question(
            self.container,
            '退出确认',
            '确定要退出实时字幕程序吗？',
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No
        )
        
        if reply == QMessageBox.Yes:
            print("\n👋 用户点击退出按钮")
            print("   正在关闭程序...")
            self.app.quit()

    def _on_system_close(self):
        """Alt+F4 / 任务栏关闭主字幕窗：不弹确认（系统关窗手势），直接优雅退出。"""
        print("\n👋 收到系统关窗（Alt+F4/任务栏），正在退出...")
        self.app.quit()
    
    def run(self):
        """
        运行事件循环（阻塞）
        必须在主线程调用
        """
        print("🎬 字幕窗口事件循环启动")
        sys.exit(self.app.exec_())
