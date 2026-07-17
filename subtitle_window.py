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
    QLabel, QApplication, QWidget, QHBoxLayout, QPushButton, QTextEdit,
    QGraphicsOpacityEffect,
)
from PyQt5.QtCore import (
    Qt, pyqtSignal, QObject, QTimer, QPropertyAnimation, QEasingCurve,
)
from PyQt5.QtGui import QFont
import config

from window_geometry import _screen_area_at, _clamp_geo_to_area, _clamp_geo_to_any_screen
from window_frame import ResizableFramelessWidget
from settings_window import (
    SettingsWindow, TUNING_KEYS, PRESET_CONTROL_ATTRS,
    apply_tuning, collect_tuning, apply_text_color, snapshot_defaults,
)
from popups import HistoryWindow, WordPopup
from window_chrome import WindowChromeMixin
from subtitle_render import LiveTextRenderMixin

if sys.platform == "win32":
    import ctypes

# 窗口位置/大小/字号的持久化文件（重启后恢复用户调好的布局）
STATE_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "window_state.json")


class SubtitleSignals(QObject):
    """信号对象（用于线程安全的UI更新）"""
    update = pyqtSignal(str)
    status = pyqtSignal(str)  # 状态提示（如暂停/继续），不进字幕历史
    live = pyqtSignal(str, str)  # live德语行更新 (committed, unstable)
    pair = pyqtSignal(str, str)  # 一段德语翻译完成 (german, chinese)
    draft = pyqtSignal(str)  # live德语的草稿中文（正式句对完成后被替换）
    lookup = pyqtSignal(str, str)  # 点词查词结果 (word, 词典文本)
    toggle_ct = pyqtSignal()  # 切换鼠标穿透模式（热键线程→主线程）
    game_mode = pyqtSignal(bool)  # 游戏模式开关（热键线程→主线程，同步设置面板）

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
        if "bg_opacity" in self._state:
            try:
                config.BACKGROUND_OPACITY = int(self._state["bg_opacity"])
            except (TypeError, ValueError):
                pass
        apply_tuning(self._state.get("tuning") or {})
        # 基线：启动应用后的 tuning（游戏模式豁免用；之后每次成功保存会刷新）
        self._state["tuning"] = collect_tuning()
        self._game_mode_active = False

        # 创建信号对象
        self.signals = SubtitleSignals()
        
        # 德语先行双层显示状态
        # 内存里多留一些句对（HISTORY_KEEP条），实际显示条数由窗口高度自适应决定：
        # 窗口拉大 → 自动多显示历史；拉小 → 只留最新的
        self.HISTORY_KEEP = 50
        self.sentence_pairs = []  # 已完成句对 [(german, chinese)]
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
        for b in (self.minimize_btn, self.history_btn, self.settings_btn, self.quit_btn):
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
        w = state.get("w", config.WINDOW_WIDTH)
        h = state.get("h", config.WINDOW_HEIGHT + 40)
        x = state.get("x", config.WINDOW_X)
        y = state.get("y", config.WINDOW_Y)
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
        # 恢复预设高亮（不重新 apply；具体值已在 tuning 里）
        self.settings_window.restore_active_preset(self._state.get("active_preset"))

        # 创建历史窗口（初始隐藏）
        self.history_window = HistoryWindow()

        # ⚙️/📜 几何：有持久化就恢复（钳进当前某屏）；否则首次显示时贴字幕窗所在屏
        self._settings_ever_shown = False
        self._history_ever_shown = False
        self._settings_positioned = self._restore_aux_geo(
            self.settings_window, self._state.get("settings_geo"))
        self._history_positioned = self._restore_aux_geo(
            self.history_window, self._state.get("history_geo"))

        # 点词查词：单击字幕里的德语词 → 弹小窗显示词典解释
        self.word_popup = WordPopup()
        self.container.on_click = self._on_label_click
        self.on_lookup = None  # (word, context) -> None，由main.py接到translator
        self._last_html = ""   # 最近一次渲染的富文本（点击命中测试要按它重建排版）

        # 连接信号到槽（线程安全）
        self.signals.update.connect(self._update_text)
        self.signals.status.connect(self._show_status)
        self.signals.live.connect(self._update_live)
        self.signals.pair.connect(self._add_pair)
        self.signals.draft.connect(self._update_draft)
        self.signals.lookup.connect(self._show_lookup)
        self.signals.toggle_ct.connect(self._toggle_click_through)
        self.signals.game_mode.connect(self._on_game_mode)
        
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
        try:
            with open(STATE_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

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
        # 面板参数：游戏模式期间 CHUNK_SUBMIT_SECONDS / DRAFT_TRANSLATION 是临时值，豁免
        state["tuning"] = collect_tuning(
            game_mode_active=bool(getattr(self, "_game_mode_active", False)),
            previous_tuning=(self._state.get("tuning") or {}),
        )
        # 场景预设高亮名（None → JSON null）；仅显示态，重启不重新 apply
        active = getattr(self.settings_window, "_active_preset", None)
        state["active_preset"] = active if active else None
        if state == self._last_saved_state:
            return
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
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

    def notify_game_mode(self, active):
        """游戏模式开关通知（线程安全，热键线程调用）→ 主线程同步设置面板"""
        self.signals.game_mode.emit(bool(active))

    def _on_game_mode(self, active):
        """主线程槽：刷新滑块显示 + 禁用/恢复被游戏模式接管的控件"""
        self._game_mode_active = bool(active)
        self.settings_window.refresh_from_config()
        self.settings_window.set_game_mode(bool(active))

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
