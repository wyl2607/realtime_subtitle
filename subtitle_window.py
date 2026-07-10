"""
字幕悬浮窗UI模块
使用PyQt5实现置顶悬浮窗口

2026-07-06 德语先行双层显示：
- 历史区：最近几条已完成句对（德语行 + 中文行）
- live行：已提交但还没翻译完的德语（白色）+ 未稳定尾部（灰色，还可能变）
识别一提交德语就立即上屏，不用等Ollama翻译。
"""
import sys
import html
import time
import json
import os
from PyQt5.QtWidgets import QLabel, QApplication, QWidget, QVBoxLayout, QHBoxLayout, QSlider, QPushButton, QGroupBox, QTextEdit
from PyQt5.QtCore import Qt, pyqtSignal, QObject
import config

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

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


class DraggableWidget(QWidget):
    """可拖动的窗口容器；支持"原地单击"回调（按下到松开位移<6px算点击，
    拖动和点词查词互不干扰）"""

    def __init__(self):
        super().__init__()
        self.dragging = False
        self.drag_position = None
        self._press_global = None
        self.on_click = None  # 原地单击回调 (widget坐标QPoint) -> None

    def mousePressEvent(self, event):
        """鼠标按下 - 开始拖动"""
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            self._press_global = event.globalPos()
            event.accept()

    def mouseMoveEvent(self, event):
        """鼠标移动 - 拖动窗口（钳制在屏幕可用区域内。
        实测能把窗口拖出屏幕顶部，按钮行被切一半就再也够不着了）"""
        if self.dragging and event.buttons() == Qt.LeftButton:
            target = event.globalPos() - self.drag_position
            screen = QApplication.screenAt(event.globalPos())
            if screen:
                area = screen.availableGeometry()
                target.setX(max(area.left(), min(target.x(), area.right() - self.width() + 1)))
                target.setY(max(area.top(), min(target.y(), area.bottom() - self.height() + 1)))
            self.move(target)
            event.accept()

    def mouseReleaseEvent(self, event):
        """鼠标释放 - 结束拖动；没怎么动过就当作一次单击"""
        if event.button() == Qt.LeftButton:
            self.dragging = False
            if (self.on_click and self._press_global is not None
                    and (event.globalPos() - self._press_global).manhattanLength() < 6):
                self.on_click(event.pos())
            self._press_global = None
            event.accept()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        cb = getattr(self, "on_resize", None)
        if cb:
            cb()

    def wheelEvent(self, event):
        cb = getattr(self, "on_wheel", None)
        if cb and cb(event):
            event.accept()
            return
        super().wheelEvent(event)

    def enterEvent(self, event):
        cb = getattr(self, "on_hover", None)
        if cb:
            cb(True)
        super().enterEvent(event)

    def leaveEvent(self, event):
        cb = getattr(self, "on_hover", None)
        if cb:
            cb(False)
        super().leaveEvent(event)


class ResizableFramelessWidget(DraggableWidget):
    """无边框窗口 + Windows 原生边缘缩放。

    之前唯一的缩放入口是右下角一个透明的 QSizeGrip（看不见，而且窗口
    底边拖出屏幕后就彻底够不着了）。这里用 WM_NCHITTEST 命中测试把
    窗口四边/四角各 RESIZE_MARGIN 物理像素交给系统处理：鼠标移到边缘
    自动变双向箭头，拖拽即缩放，和普通窗口手感一致。
    """

    RESIZE_MARGIN = 10  # 物理像素（不受DPI缩放影响：坐标和窗口矩形都是物理值）

    def __init__(self):
        super().__init__()
        # WA_TranslucentBackground 下 alpha=0 的像素鼠标会直接穿透到下层窗口：
        # 顶部按钮行两侧、底部手柄行都是全透明的，导致上/下边缘抓不住
        # （左右边缘因为字幕标签是不透明黑色所以能抓）。
        # 注意：给顶层窗口本身设样式底色不生效（实测alpha=255也不上屏），
        # 必须用一个铺满窗口的子控件当底衬——alpha=2肉眼不可见，但整个
        # 窗口表面都能接住鼠标（test_hittest.py 有回归测试）
        self._underlay = QWidget(self)
        self._underlay.setObjectName("hitUnderlay")
        self._underlay.setAttribute(Qt.WA_StyledBackground, True)
        self._underlay.setStyleSheet("#hitUnderlay { background: rgba(0, 0, 0, 2); }")
        self._underlay.lower()

    def resizeEvent(self, event):
        self._underlay.setGeometry(self.rect())
        super().resizeEvent(event)  # 父类会触发 on_resize 回调（重排句对）

    # WM_NCHITTEST 返回码
    _HIT_CODES = {
        (True, False, True, False): 13,   # HTTOPLEFT
        (True, False, False, True): 14,   # HTTOPRIGHT
        (False, True, True, False): 16,   # HTBOTTOMLEFT
        (False, True, False, True): 17,   # HTBOTTOMRIGHT
        (True, False, False, False): 12,  # HTTOP
        (False, True, False, False): 15,  # HTBOTTOM
        (False, False, True, False): 10,  # HTLEFT
        (False, False, False, True): 11,  # HTRIGHT
    }

    def nativeEvent(self, eventType, message):
        if sys.platform != "win32" or eventType not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message != 0x0084:  # WM_NCHITTEST
            return False, 0
        # lParam 低/高16位是带符号的屏幕物理坐标（多屏/负坐标要按short解）
        x = ctypes.c_short(msg.lParam & 0xFFFF).value
        y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
        rect = wintypes.RECT()
        ctypes.windll.user32.GetWindowRect(int(self.winId()), ctypes.byref(rect))
        m = self.RESIZE_MARGIN
        edges = (y < rect.top + m, y > rect.bottom - m,
                 x < rect.left + m, x > rect.right - m)
        hit = self._HIT_CODES.get(edges, 0)
        if hit:
            return True, hit
        return False, 0


class SettingsWindow(DraggableWidget):
    """参数调节窗口"""

    def __init__(self, on_font_change=None):
        super().__init__()
        self._on_font_change = on_font_change  # 字号变了要让字幕/历史窗口重刷样式
        self.setWindowTitle("⚙️ 参数调节（可拖动）")
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.setGeometry(100, 100, 500, 640)

        # 记录启动时的config默认值（用于"恢复默认值"）
        self._defaults = {
            'CHUNK_SUBMIT_SECONDS': config.CHUNK_SUBMIT_SECONDS,
            'BUFFER_TRIM_SEC': config.BUFFER_TRIM_SEC,
            'IDLE_FLUSH_SEC': config.IDLE_FLUSH_SEC,
            'ENERGY_THRESHOLD_SPEECH': config.ENERGY_THRESHOLD_SPEECH,
            'MAX_SUBTITLE_LENGTH': config.MAX_SUBTITLE_LENGTH,
            'MAX_SENTENCE_PAIRS': config.MAX_SENTENCE_PAIRS,
            'FONT_SIZE': config.FONT_SIZE,
            'BACKGROUND_OPACITY': config.BACKGROUND_OPACITY,
        }

        layout = QVBoxLayout()

        # 流式识别设置
        duration_group = QGroupBox("流式识别设置")
        duration_layout = QVBoxLayout()

        self.chunk_submit_slider = self._create_slider(
            "提交节奏(秒)", 0.3, 2.0, config.CHUNK_SUBMIT_SECONDS, 0.1,
            lambda v: setattr(config, 'CHUNK_SUBMIT_SECONDS', v)
        )
        self.buffer_trim_slider = self._create_slider(
            "识别缓冲上限(秒)", 6.0, 20.0, config.BUFFER_TRIM_SEC, 1.0,
            lambda v: setattr(config, 'BUFFER_TRIM_SEC', v)
        )
        self.idle_flush_slider = self._create_slider(
            "收尾静音(秒)", 1.0, 5.0, config.IDLE_FLUSH_SEC, 0.5,
            lambda v: setattr(config, 'IDLE_FLUSH_SEC', v)
        )

        duration_layout.addWidget(self.chunk_submit_slider['widget'])
        duration_layout.addWidget(self.buffer_trim_slider['widget'])
        duration_layout.addWidget(self.idle_flush_slider['widget'])
        duration_group.setLayout(duration_layout)

        # 能量阈值设置
        energy_group = QGroupBox("静音门设置")
        energy_layout = QVBoxLayout()

        self.speech_threshold_slider = self._create_slider(
            "语音能量阈值", 0.005, 0.05, config.ENERGY_THRESHOLD_SPEECH, 0.001,
            lambda v: setattr(config, 'ENERGY_THRESHOLD_SPEECH', v)
        )

        energy_layout.addWidget(self.speech_threshold_slider['widget'])
        energy_group.setLayout(energy_layout)

        # 字幕显示设置
        display_group = QGroupBox("字幕显示设置")
        display_layout = QVBoxLayout()

        self.max_length_slider = self._create_slider(
            "最大字符数", 50, 600, config.MAX_SUBTITLE_LENGTH, 10,
            lambda v: setattr(config, 'MAX_SUBTITLE_LENGTH', int(v))
        )

        self.max_pairs_slider = self._create_slider(
            "句对条数上限", 1, 20, config.MAX_SENTENCE_PAIRS, 1,
            lambda v: setattr(config, 'MAX_SENTENCE_PAIRS', int(v))
        )

        def set_font_size(v):
            config.FONT_SIZE = int(v)
            if self._on_font_change:
                self._on_font_change()

        self.font_size_slider = self._create_slider(
            "字体大小", 14, 36, config.FONT_SIZE, 1, set_font_size
        )

        def set_bg_opacity(v):
            config.BACKGROUND_OPACITY = int(v)
            if self._on_font_change:
                self._on_font_change()  # 同一个回调：重刷字幕样式

        self.bg_opacity_slider = self._create_slider(
            "背景不透明度", 100, 255, config.BACKGROUND_OPACITY, 5, set_bg_opacity
        )

        display_layout.addWidget(self.max_length_slider['widget'])
        display_layout.addWidget(self.max_pairs_slider['widget'])
        display_layout.addWidget(self.font_size_slider['widget'])
        display_layout.addWidget(self.bg_opacity_slider['widget'])
        display_group.setLayout(display_layout)
        
        # 添加到主布局
        layout.addWidget(duration_group)
        layout.addWidget(energy_group)
        layout.addWidget(display_group)
        
        # 重置按钮
        reset_btn = QPushButton("🔄 恢复默认值")
        reset_btn.clicked.connect(self._reset_defaults)
        layout.addWidget(reset_btn)
        
        layout.addStretch()
        self.setLayout(layout)
        
    def _create_slider(self, label, min_val, max_val, current_val, step, callback):
        """创建滑块控件"""
        widget = QWidget()
        layout = QHBoxLayout()
        
        # 标签
        label_widget = QLabel(f"{label}:")
        label_widget.setMinimumWidth(120)
        
        # 滑块（用round避免浮点截断，如0.01/0.001=9.999...被int截成9）
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(round(min_val / step))
        slider.setMaximum(round(max_val / step))
        slider.setValue(round(current_val / step))
        
        # 数值显示
        value_label = QLabel(f"{current_val:.3f}")
        value_label.setMinimumWidth(60)
        
        # 滑块变化时更新
        def on_change(int_value):
            value = int_value * step
            value_label.setText(f"{value:.3f}")
            callback(value)
            print(f"📊 {label}: {value:.3f}")
        
        slider.valueChanged.connect(on_change)
        
        layout.addWidget(label_widget)
        layout.addWidget(slider)
        layout.addWidget(value_label)
        widget.setLayout(layout)
        
        return {'widget': widget, 'slider': slider, 'label': value_label, 'step': step}
    
    def _reset_defaults(self):
        """恢复默认值（恢复到config.py里的启动默认值）"""
        # 通过滑块setValue触发valueChanged回调，自动同步config和数值标签
        pairs = [
            (self.chunk_submit_slider, 'CHUNK_SUBMIT_SECONDS'),
            (self.buffer_trim_slider, 'BUFFER_TRIM_SEC'),
            (self.idle_flush_slider, 'IDLE_FLUSH_SEC'),
            (self.speech_threshold_slider, 'ENERGY_THRESHOLD_SPEECH'),
            (self.max_length_slider, 'MAX_SUBTITLE_LENGTH'),
            (self.max_pairs_slider, 'MAX_SENTENCE_PAIRS'),
            (self.font_size_slider, 'FONT_SIZE'),
            (self.bg_opacity_slider, 'BACKGROUND_OPACITY'),
        ]
        for slider_info, key in pairs:
            slider_info['slider'].setValue(round(self._defaults[key] / slider_info['step']))

        print("🔄 参数已恢复默认值")

class HistoryWindow(QWidget):
    """本场字幕历史（可滚动回看，精听时往上翻错过的句子）

    只存内存里的本场内容；跨天/跨场的完整记录在 transcripts/ 目录。
    """

    def __init__(self):
        super().__init__()
        self.setWindowTitle("📜 字幕历史（本场）")
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.setGeometry(150, 120, 720, 560)

        layout = QVBoxLayout()
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        # 封顶：几小时的session能攒上千句对，QTextEdit无限增长会越来越卡。
        # 超出自动丢最旧的（完整记录永远在 transcripts/ 里）
        self.text.document().setMaximumBlockCount(2000)
        self.text.setStyleSheet(f"""
            QTextEdit {{
                background-color: rgb(20, 20, 20);
                color: white;
                font-size: {config.FONT_SIZE - 4}px;
                font-family: {config.FONT_FAMILY};
                border: none;
                padding: 8px;
            }}
        """)
        layout.addWidget(self.text)

        hint = QLabel("往上滚动可回看；停在底部时新字幕自动跟进。完整记录在 transcripts\\ 目录。")
        hint.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(hint)
        self.setLayout(layout)

    def append_pair(self, german, chinese):
        sb = self.text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 10  # 用户没往上翻才自动跟进

        cursor = self.text.textCursor()
        cursor.movePosition(cursor.End)
        stamp = time.strftime("%H:%M:%S")
        block = f'<span style="color:#777">[{stamp}]</span> {html.escape(german)}<br>' if german else ""
        block += f'<span style="color:#9ad0ff">{html.escape(chinese)}</span><br><br>'
        cursor.insertHtml(block)

        if at_bottom:
            sb.setValue(sb.maximum())


class WordPopup(QWidget):
    """点词查词的小弹窗：跟着鼠标位置出现，自动消失，点一下也消失"""

    def __init__(self):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.label.setStyleSheet("""
            QLabel {
                background-color: rgba(25, 30, 40, 240);
                color: white;
                font-size: 16px;
                font-family: Microsoft YaHei, Arial;
                padding: 10px 14px;
                border-radius: 8px;
                border: 1px solid rgba(143, 184, 224, 0.6);
            }
        """)
        layout.addWidget(self.label)
        self.setLayout(layout)
        self.setMaximumWidth(420)

        from PyQt5.QtCore import QTimer
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_at(self, global_pos, html_text, timeout_ms=15000):
        self.label.setText(html_text)
        self.adjustSize()
        # 往上偏移显示，避免挡住刚点的词；不出屏
        screen = QApplication.primaryScreen().availableGeometry()
        x = min(max(screen.left(), global_pos.x() - 40), screen.right() - self.width())
        y = global_pos.y() - self.height() - 12
        if y < screen.top():
            y = global_pos.y() + 20
        self.move(x, y)
        self.show()
        self.raise_()
        self._hide_timer.start(timeout_ms)

    def mousePressEvent(self, event):
        self.hide()


class SubtitleWindow:
    """字幕悬浮窗"""
    
    def __init__(self):
        """初始化字幕窗口"""
        # 创建QApplication实例
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)

        # 恢复上次的窗口布局（字号/不透明度要在建字幕标签之前生效）
        self._state = self._load_state()
        if "font_size" in self._state:
            config.FONT_SIZE = int(self._state["font_size"])
        if "bg_opacity" in self._state:
            config.BACKGROUND_OPACITY = int(self._state["bg_opacity"])
        
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
        self.status_line = ""     # 状态提示（暂停/切语言），下次内容更新时清掉

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
        self.container.setMinimumSize(360, 120)

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

        # 窗口大小变化：重摆工具条 + 重新计算能放下几条句对
        self.container.on_resize = self._on_container_resize
        # 鼠标移入显示工具条，移出隐藏
        self.container.on_hover = self._set_controls_visible
        # Ctrl+滚轮直接调字号（不用开设置面板）
        self.container.on_wheel = self._on_wheel
        # 窗口几何：优先用上次退出时保存的（用户拖过/缩放过就记住），没有才用config默认。
        # 用availableGeometry（去掉任务栏）并把窗口完整钳回屏幕内——
        # 之前底边可以停在屏幕外，唯一的缩放手柄跟着出屏，窗口就再也调不了了
        state = self._state
        screen = self.app.primaryScreen().availableGeometry()
        w = min(state.get("w", config.WINDOW_WIDTH), screen.width())
        h = min(state.get("h", config.WINDOW_HEIGHT + 40), screen.height())
        x = max(screen.left(), min(state.get("x", config.WINDOW_X), screen.right() - w))
        y = max(screen.top(), min(state.get("y", config.WINDOW_Y), screen.bottom() - h))
        self.container.setGeometry(x, y, w, h)
        # 布局持久化：停止脚本是Stop-Process强杀，aboutToQuit不可靠，
        # 用定时器检查——布局变了才写盘（15秒一次，无变化零开销）
        self._last_saved_state = None
        from PyQt5.QtCore import QTimer
        self._state_timer = QTimer()
        self._state_timer.timeout.connect(self._save_state_if_changed)
        self._state_timer.start(15000)
        self.app.aboutToQuit.connect(self._save_state_if_changed)
        self.container.show()
        # 工具条先亮4秒让人知道在哪，之后只在鼠标移入时出现
        self._position_btn_bar()
        self.btn_bar.show()
        self.btn_bar.raise_()
        QTimer.singleShot(4000, lambda: self._set_controls_visible(self.container.underMouse()))
        
        # 创建设置窗口（初始隐藏）；字号滑块变化时重刷字幕/历史窗口样式
        self.settings_window = SettingsWindow(on_font_change=self._apply_styles)

        # 创建历史窗口（初始隐藏）
        self.history_window = HistoryWindow()

        # 点词查词：单击字幕里的德语词 → 弹小窗显示词典解释
        self.word_popup = WordPopup()
        self.container.on_click = self._on_label_click
        self.on_lookup = None  # (word, context) -> None，由main.py接到translator
        self._last_html = ""   # 最近一次渲染的富文本（点击命中测试要按它重建排版）

        # 鼠标穿透模式状态（Ctrl+Alt+M切换）
        self._click_through = False

        # 连接信号到槽（线程安全）
        self.signals.update.connect(self._update_text)
        self.signals.status.connect(self._show_status)
        self.signals.live.connect(self._update_live)
        self.signals.pair.connect(self._add_pair)
        self.signals.draft.connect(self._update_draft)
        self.signals.lookup.connect(self._show_lookup)
        self.signals.toggle_ct.connect(self._toggle_click_through)
        
        print("✅ 字幕窗口已创建")
        print(f"   位置: ({x}, {y})  大小: {w}x{h}")
        print("   💡 鼠标拖动窗口可移动位置")
        print("   💡 鼠标移到窗口边缘/四角可拖拽缩放（窗口越大显示的历史越多）")
        print("   💡 按钮工具条平时隐藏，鼠标移入窗口右上角出现")
        print("   💡 点击 ➖ 按钮可最小化字幕")
        print("   💡 点击 ⚙️ 按钮可打开参数调节面板")
        print("   💡 点击 ❌ 按钮可退出程序")
    
    # ------------------------------------------------------------------
    # 悬浮工具条
    # ------------------------------------------------------------------
    def _position_btn_bar(self):
        """工具条贴右上角（不进布局，避免显示/隐藏引起文字重排）"""
        self.btn_bar.adjustSize()
        self.btn_bar.move(self.container.width() - self.btn_bar.width() - 10, 8)

    def _set_controls_visible(self, visible):
        self.btn_bar.setVisible(visible)
        if visible:
            self.btn_bar.raise_()

    def _on_container_resize(self):
        self.window.setGeometry(self.container.rect())  # 标签手动铺满（无布局）
        self._position_btn_bar()
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

    def _save_state_if_changed(self):
        g = self.container.geometry()
        state = {"x": g.x(), "y": g.y(), "w": g.width(), "h": g.height(),
                 "font_size": config.FONT_SIZE,
                 "bg_opacity": config.BACKGROUND_OPACITY}
        if state == self._last_saved_state:
            return
        try:
            with open(STATE_FILE, "w", encoding="utf-8") as f:
                json.dump(state, f)
            self._last_saved_state = state
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
        self.history_window.append_pair(german, chinese)
        self._render()
        if config.SHOW_PERFORMANCE:
            print(f"💬 字幕: {chinese[:50]}{'...' if len(chinese) > 50 else ''}")

    def _update_text(self, text):
        """旧接口的槽：当作一条无原文的句对处理"""
        if text and text.strip():
            self._add_pair("", text)

    def _show_status(self, text):
        """状态提示：追加在当前内容底部"""
        self.status_line = text
        self._render()

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
            lines.append(f'<span style="color:#c8c8c8">{html.escape(self._clip(chinese))}</span>')
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
        """按 QLabel 相同的字体/宽度建一个 QTextDocument（测高/点击命中测试共用）"""
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

    def _render(self):
        """把句对历史+live行渲染成富文本。

        显示几条句对由真实排版高度决定：用 QTextDocument 按 QLabel 相同的
        字体/宽度排版测高，从最新往旧塞，塞到窗口装不下为止——窗口拉多大
        就填多满（瀑布式），不再靠折行估算（之前估得保守，顶部留大片空黑）。
        """
        live_block = self._live_block_html()
        status = html.escape(self.status_line) if self.status_line else ""
        fixed = [b for b in (live_block, status) if b]

        # 和 QLabel 一致的排版环境（padding 15px 20px + 2px边框）
        doc = self._build_doc()
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
            self.show_status("👻 鼠标穿透已开启：字幕窗点不到了（Ctrl+Alt+M 恢复）")
            print("👻 [热键] 鼠标穿透开启")
        else:
            ctypes.windll.user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex & ~WS_EX_TRANSPARENT)
            self.show_status("🖱️ 鼠标穿透已关闭，字幕窗恢复可点击")
            print("🖱️ [热键] 鼠标穿透关闭")

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
            self.settings_window.show()

    def _toggle_history(self):
        """切换历史窗口显示"""
        if self.history_window.isVisible():
            self.history_window.hide()
        else:
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
