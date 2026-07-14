"""
字幕悬浮窗UI模块
使用PyQt5实现置顶悬浮窗口

2026-07-06 德语先行双层显示：
- 历史区：最近几条已完成句对（德语行 + 中文行）
- live行：已提交但还没翻译完的德语（白色）+ 未稳定尾部（灰色，还可能变）
识别一提交德语就立即上屏，不用等Ollama翻译。

2026-07-14 拆分：DraggableWidget/ResizableFramelessWidget -> window_frame.py，
屏幕几何工具 -> window_geometry.py，SettingsWindow+tuning辅助 -> settings_window.py，
HistoryWindow/WordPopup -> popups.py。这里只留 SubtitleWindow 主类。
"""
import sys
import html
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

class SubtitleWindow:
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
    def _position_chrome(self):
        """绝对定位标题条 + 右上角按钮 + 穿透指示器（不进布局）。"""
        w = self.container.width()
        h = ResizableFramelessWidget.DRAG_BAR_HEIGHT
        self.drag_bar.setGeometry(0, 0, w, h)
        self.btn_bar.adjustSize()
        self.btn_bar.move(w - self.btn_bar.width() - 10, max(0, (h - self.btn_bar.height()) // 2))
        self.ct_indicator.adjustSize()
        # 右上角常驻；穿透时 chrome 已藏，不会和 drag_bar/btn_bar 重叠
        self.ct_indicator.move(w - self.ct_indicator.width() - 8, 6)
        if self.drag_bar.isVisible():
            self.drag_bar.raise_()
        if self.btn_bar.isVisible():
            self.btn_bar.raise_()
        if self.ct_indicator.isVisible():
            self.ct_indicator.raise_()

    def _position_btn_bar(self):
        """兼容旧调用点：与 _position_chrome 等价"""
        self._position_chrome()

    def _set_controls_visible(self, visible):
        """drag_bar / btn_bar 淡入淡出。ct_indicator 不走这里（穿透可靠性优先）。

        HTCAPTION 门控读 drag_bar.isVisible()：淡出过程中 bar 仍 visible 可拖；
        动画结束后才 setVisible(False) 关门控。淡入前先 setVisible(True)。
        """
        # 穿透时窗口收不到鼠标，hover 失效；chrome 强制隐藏，只留穿透指示器
        if getattr(self, "_click_through", False):
            visible = False
        self._chrome_want_visible = bool(visible)
        if visible:
            # 淡入前先露出（opacity 可能仍是上次的中间值，从当前值继续）
            self.drag_bar.setVisible(True)
            self.btn_bar.setVisible(True)
            self.drag_bar.raise_()
            self.btn_bar.raise_()
            self._animate_chrome(1.0, 150)
        else:
            self._animate_chrome(0.0, 250)

    def _animate_chrome(self, target_opacity, duration_ms):
        """停掉旧动画，从当前 opacity 播到目标（对象复用，不 new）。"""
        pairs = (
            (self._drag_fade, self._drag_opacity),
            (self._btn_fade, self._btn_opacity),
        )
        for anim, effect in pairs:
            anim.stop()
            anim.setStartValue(effect.opacity())
            anim.setEndValue(target_opacity)
            anim.setDuration(duration_ms)
            anim.start()

    def _on_chrome_fade_finished(self):
        """淡出结束才真正隐藏，避免透明控件仍 isVisible 吃掉 HTCAPTION 门控。"""
        if self._chrome_want_visible:
            return
        self.drag_bar.setVisible(False)
        self.btn_bar.setVisible(False)

    def _on_container_resize(self):
        self.window.setGeometry(self.container.rect())  # 标签手动铺满（无布局）
        self._position_chrome()
        self._render()

    def _on_wheel(self, event):
        """Ctrl+滚轮调字号（高频操作，不该每次都开设置面板拖滑块）"""
        if not event.modifiers() & Qt.ControlModifier:
            return False
        step = 1 if event.angleDelta().y() > 0 else -1
        size = max(14, min(36, config.FONT_SIZE + step))
        if size != config.FONT_SIZE:
            # 通过设置面板滑块走，数值标签/config/样式一次同步
            s = self.settings_window.font_size_slider
            s['slider'].setValue(round(size / s['step']))
            self._render()
            self.show_status(f"🔠 字号 {size}（Ctrl+滚轮调节）")
        return True

    # ------------------------------------------------------------------
    # 样式与布局持久化
    # ------------------------------------------------------------------
    def _on_display_settings_change(self):
        """设置面板显示相关改动：刷样式 + 重渲染（颜色/双语/字体都落到 HTML）。"""
        self._apply_styles()
        self._render()

    def _apply_styles(self):
        """按当前config刷新字幕标签/历史窗口的样式（字号滑块实时调用）"""
        self.window.setStyleSheet(f"""
            QLabel {{
                background-color: rgba(0, 0, 0, {int(config.BACKGROUND_OPACITY)});
                color: {config.TEXT_COLOR};
                font-size: {config.FONT_SIZE}px;
                font-family: {config.FONT_FAMILY};
                padding: {config.PADDING};
                border-radius: {config.BORDER_RADIUS}px;
                border: 1px solid {config.BORDER_COLOR};
            }}
        """)
        if hasattr(self, "history_window"):
            self.history_window.text.setStyleSheet(f"""
                QTextEdit {{
                    background-color: rgb(20, 20, 20);
                    color: white;
                    font-size: {config.FONT_SIZE - 4}px;
                    font-family: {config.FONT_FAMILY};
                    border: none;
                    padding: 8px;
                }}
            """)

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
    def update_live(self, committed, unstable):
        """更新live德语行：committed=已提交未翻译（白），unstable=未稳定尾部（灰）"""
        self.signals.live.emit(committed or "", unstable or "")

    def add_pair(self, german, chinese):
        """一段德语翻译完成，加入历史句对"""
        self.signals.pair.emit(german or "", chinese or "")

    def update_draft(self, chinese):
        """live德语的草稿中文（线程安全）。正式句对完成后自动清掉"""
        self.signals.draft.emit(chinese or "")

    def update_subtitle(self, text):
        """旧接口：当一条完成句对显示（兼容保留）"""
        self.signals.update.emit(text)

    def show_status(self, text):
        """显示一条状态提示（线程安全）。不进句对历史，下次内容更新时自然消失"""
        self.signals.status.emit(text)

    # ------------------------------------------------------------------
    # 主线程槽函数
    # ------------------------------------------------------------------
    def _update_live(self, committed, unstable):
        if (committed == self.live_committed and unstable == self.live_unstable
                and not self.status_line):
            return  # 内容没变不重排——识别端每0.5秒来一次"无新提交"的空转
        self.live_committed = committed
        self.live_unstable = unstable
        if not committed:
            self.live_draft = ""  # live德语没了，草稿跟着失效
        self.status_line = ""
        self._status_clear_timer.stop()
        self._render()

    def _update_draft(self, chinese):
        self.live_draft = chinese
        self._render()

    def _add_pair(self, german, chinese):
        self.sentence_pairs.append((german, chinese))
        while len(self.sentence_pairs) > self.HISTORY_KEEP:
            self.sentence_pairs.pop(0)
        self.live_draft = ""  # 正式翻译到了，草稿退场
        self.status_line = ""
        self._status_clear_timer.stop()
        self.history_window.append_pair(german, chinese)
        self._render()
        if config.SHOW_PERFORMANCE:
            print(f"💬 字幕: {chinese[:50]}{'...' if len(chinese) > 50 else ''}")

    def _update_text(self, text):
        """旧接口的槽：当作一条无原文的句对处理"""
        if text and text.strip():
            self._add_pair("", text)

    def _show_status(self, text):
        """状态提示：追加在当前内容底部；5 秒后自动清空（新 status 重置计时）。"""
        self.status_line = text
        self._render()
        self._status_clear_timer.start(5000)

    def _clear_status(self):
        """status 超时回调：置空并重绘（避免安静段提示挂几分钟）。

        _render 在「无句对/无 live」时会 early-return 保底不闪空白；
        若上次屏上只有 status，这里主动摘掉，避免 status_line 空了字还挂着。
        """
        if not self.status_line:
            return
        cleared = self.status_line
        self.status_line = ""
        self._render()
        esc = html.escape(cleared)
        if not esc or not self._last_html or esc not in self._last_html:
            return  # _render 已用新内容覆盖
        parts = [p for p in self._last_html.split("<br>") if p != esc]
        if parts:
            self._last_html = "<br>".join(parts)
            self.window.setText(self._last_html)
        else:
            self._last_html = ""
            self.window.setText("🎬 等待音频输入...")

    @staticmethod
    def _clip(text):
        if len(text) > config.MAX_SUBTITLE_LENGTH:
            return text[:config.MAX_SUBTITLE_LENGTH] + "..."
        return text

    def _pair_html(self, german, chinese):
        """一条已完成句对的富文本块"""
        lines = []
        if german and getattr(config, "SHOW_BILINGUAL", True):
            lines.append(html.escape(self._clip(german)))
        if chinese:
            color = getattr(config, "CHINESE_TEXT_COLOR", "#c8c8c8")
            lines.append(f'<span style="color:{color}">{html.escape(self._clip(chinese))}</span>')
        return "<br>".join(lines)

    def _live_block_html(self):
        """live行的富文本块：德语（白+灰色未稳定尾部）+ 草稿中文"""
        live_parts = []
        if self.live_committed:
            live_parts.append(html.escape(self._clip(self.live_committed)))
        if self.live_unstable:
            color = getattr(config, "UNSTABLE_TEXT_COLOR", "#999999")
            live_parts.append(f'<span style="color:{color}"><i>{html.escape(self._clip(self.live_unstable))}</i></span>')
        live_block = " ".join(live_parts)
        if self.live_draft:
            # 草稿中文：正式翻译还没到之前先给一版（浅蓝斜体和正式中文区分）
            draft_color = getattr(config, "DRAFT_TEXT_COLOR", "#8fb8e0")
            live_block += ('<br>' if live_block else '') + \
                f'<span style="color:{draft_color}"><i>{html.escape(self._clip(self.live_draft))}</i></span>'
        return live_block

    def _build_doc(self, html_str=""):
        """按 QLabel 相同的字体/宽度新建 QTextDocument。

        点词命中测试必须用独立文档（勿共享 _doc_cache：命中中途 _render
        可能 setHtml 改掉内容）。排版参数（margin 0 / width-46 / pixelSize）
        与瀑布填充、点词坐标强绑定，勿改。
        """
        from PyQt5.QtGui import QTextDocument, QFont
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        font = QFont(config.FONT_FAMILY.split(",")[0].strip())
        font.setPixelSize(config.FONT_SIZE)
        doc.setDefaultFont(font)
        doc.setTextWidth(max(100, self.window.width() - 46))
        if html_str:
            doc.setHtml(html_str)
        return doc

    def _doc_for_render(self):
        """_render 测高用：font/size/width 未变则复用同一 QTextDocument。"""
        from PyQt5.QtGui import QTextDocument, QFont
        family = config.FONT_FAMILY.split(",")[0].strip()
        size = config.FONT_SIZE
        text_width = max(100, self.window.width() - 46)
        key = (family, size, text_width)
        if self._doc_cache is not None and self._doc_cache_key == key:
            return self._doc_cache
        doc = QTextDocument()
        doc.setDocumentMargin(0)
        font = QFont(family)
        font.setPixelSize(size)
        doc.setDefaultFont(font)
        doc.setTextWidth(text_width)
        self._doc_cache = doc
        self._doc_cache_key = key
        return doc

    def _render(self):
        """把句对历史+live行渲染成富文本。

        显示几条句对由真实排版高度决定：用 QTextDocument 按 QLabel 相同的
        字体/宽度排版测高，从最新往旧塞，塞到窗口装不下为止——窗口拉多大
        就填多满（瀑布式），不再靠折行估算（之前估得保守，顶部留大片空黑）。
        """
        live_block = self._live_block_html()
        status = html.escape(self.status_line) if self.status_line else ""
        fixed = [b for b in (live_block, status) if b]

        # 和 QLabel 一致的排版环境（padding 15px 20px + 2px边框）；缓存复用
        doc = self._doc_for_render()
        avail_h = self.window.height() - 40

        pair_blocks = [b for b in (self._pair_html(g, c) for g, c in self.sentence_pairs) if b]
        cap = min(len(pair_blocks), getattr(config, "MAX_SENTENCE_PAIRS", 20))

        def fits(count):
            doc.setHtml("<br>".join(pair_blocks[len(pair_blocks) - count:] + fixed))
            return doc.size().height() <= avail_h

        # 二分找能装下的最多句对数（fits随count单调递减；每次渲染最多
        # ~5次排版测量，之前线性试装最多20次，UI线程上省一截）。
        # 最新一条保底：窗口再小也至少显示1条
        shown = 0
        if cap:
            shown = 1
            lo, hi = 2, cap
            while lo <= hi:
                mid = (lo + hi) // 2
                if fits(mid):
                    shown = mid
                    lo = mid + 1
                else:
                    hi = mid - 1

        blocks = (pair_blocks[len(pair_blocks) - shown:] if shown else []) + fixed
        if not blocks:
            return  # 什么都没有就保持现状（避免闪空白）

        self._last_html = "<br>".join(blocks)
        self.window.setText(self._last_html)
        self.window.show()

    # ------------------------------------------------------------------
    # 点词查词
    # ------------------------------------------------------------------
    def _on_label_click(self, pos):
        """容器上的原地单击：命中字幕文字里的德语词就发起词典查询。

        QLabel 的富文本没有词级点击API，这里用和渲染完全相同的
        QTextDocument 重建排版，documentLayout().hitTest 找到字符位置，
        再取词。注意 QLabel 是 AlignBottom：文档从内容区底部往上排。
        """
        if not self.on_lookup or not self._last_html:
            return
        lp = pos - self.window.pos()  # 容器坐标 → 字幕标签坐标
        if not self.window.rect().contains(lp):
            return

        from PyQt5.QtCore import QPointF
        from PyQt5.QtGui import QTextCursor
        doc = self._build_doc(self._last_html)
        # 内容区 = 标签减 padding(15px 20px) 和 2px 边框
        content_x0, content_y0 = 22, 17
        content_h = self.window.height() - 34
        doc_y0 = content_y0 + max(0, content_h - doc.size().height())  # AlignBottom
        hit = doc.documentLayout().hitTest(
            QPointF(lp.x() - content_x0, lp.y() - doc_y0), Qt.ExactHit)
        if hit < 0:
            return  # 点在空白处

        cursor = QTextCursor(doc)
        cursor.setPosition(hit)
        cursor.select(QTextCursor.WordUnderCursor)
        word = cursor.selectedText().strip(".,!?…:;\"'«»()")
        # 只查拉丁字母词（德语/英语）；点到中文/数字/空白不弹窗
        if len(word) < 2 or not all(c.isalpha() and ord(c) < 0x2E80 for c in word):
            return
        context = cursor.block().text()  # 该词所在行做上下文

        from PyQt5.QtGui import QCursor
        self._lookup_anchor = QCursor.pos()
        self.word_popup.show_at(self._lookup_anchor,
                                f"🔍 <b>{html.escape(word)}</b> 查询中…", 8000)
        self.on_lookup(word, context)

    def show_lookup_result(self, word, text):
        """词典查询完成（线程安全，从翻译线程调）"""
        self.signals.lookup.emit(word or "", text or "")

    def _show_lookup(self, word, text):
        body = html.escape(text).replace("\n", "<br>")
        self.word_popup.show_at(
            getattr(self, "_lookup_anchor", self.container.pos()),
            f"📖 <b>{html.escape(word)}</b><br>{body}")

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
    
    def run(self):
        """
        运行事件循环（阻塞）
        必须在主线程调用
        """
        print("🎬 字幕窗口事件循环启动")
        sys.exit(self.app.exec_())
