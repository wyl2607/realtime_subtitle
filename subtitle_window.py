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
from PyQt5.QtWidgets import QLabel, QApplication, QWidget, QVBoxLayout, QHBoxLayout, QSlider, QPushButton, QGroupBox
from PyQt5.QtCore import Qt, pyqtSignal, QObject
import config

class SubtitleSignals(QObject):
    """信号对象（用于线程安全的UI更新）"""
    update = pyqtSignal(str)
    status = pyqtSignal(str)  # 状态提示（如暂停/继续），不进字幕历史
    live = pyqtSignal(str, str)  # live德语行更新 (committed, unstable)
    pair = pyqtSignal(str, str)  # 一段德语翻译完成 (german, chinese)


class DraggableWidget(QWidget):
    """可拖动的窗口容器"""
    
    def __init__(self):
        super().__init__()
        self.dragging = False
        self.drag_position = None
    
    def mousePressEvent(self, event):
        """鼠标按下 - 开始拖动"""
        if event.button() == Qt.LeftButton:
            self.dragging = True
            self.drag_position = event.globalPos() - self.frameGeometry().topLeft()
            event.accept()
    
    def mouseMoveEvent(self, event):
        """鼠标移动 - 拖动窗口"""
        if self.dragging and event.buttons() == Qt.LeftButton:
            self.move(event.globalPos() - self.drag_position)
            event.accept()
    
    def mouseReleaseEvent(self, event):
        """鼠标释放 - 结束拖动"""
        if event.button() == Qt.LeftButton:
            self.dragging = False
            event.accept()


class SettingsWindow(DraggableWidget):
    """参数调节窗口"""
    
    def __init__(self):
        super().__init__()
        self.setWindowTitle("⚙️ 参数调节（可拖动）")
        self.setWindowFlags(Qt.WindowStaysOnTopHint)
        self.setGeometry(100, 100, 500, 600)

        # 记录启动时的config默认值（用于"恢复默认值"）
        self._defaults = {
            'CHUNK_SUBMIT_SECONDS': config.CHUNK_SUBMIT_SECONDS,
            'BUFFER_TRIM_SEC': config.BUFFER_TRIM_SEC,
            'IDLE_FLUSH_SEC': config.IDLE_FLUSH_SEC,
            'ENERGY_THRESHOLD_SPEECH': config.ENERGY_THRESHOLD_SPEECH,
            'MAX_SUBTITLE_LENGTH': config.MAX_SUBTITLE_LENGTH,
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

        display_layout.addWidget(self.max_length_slider['widget'])
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
        ]
        for slider_info, key in pairs:
            slider_info['slider'].setValue(round(self._defaults[key] / slider_info['step']))

        print("🔄 参数已恢复默认值")

class SubtitleWindow:
    """字幕悬浮窗"""
    
    def __init__(self):
        """初始化字幕窗口"""
        # 创建QApplication实例
        self.app = QApplication(sys.argv)
        self.app.setQuitOnLastWindowClosed(False)
        
        # 创建信号对象
        self.signals = SubtitleSignals()
        
        # 德语先行双层显示状态
        self.sentence_pairs = []  # 已完成句对 [(german, chinese)]，最多 MAX_SENTENCE_PAIRS 条
        self.live_committed = ""  # live行：已提交未翻译的德语
        self.live_unstable = ""   # live行：未稳定尾部（灰色）
        self.status_line = ""     # 状态提示（暂停/切语言），下次内容更新时清掉
        
        # 创建主容器窗口（使用可拖动的容器）
        self.container = DraggableWidget()
        self.container.setWindowTitle("实时字幕")
        self.container.setWindowFlags(
            Qt.WindowStaysOnTopHint |
            Qt.FramelessWindowHint |
            Qt.Tool
        )
        self.container.setAttribute(Qt.WA_TranslucentBackground)
        
        # 创建布局
        container_layout = QVBoxLayout()
        container_layout.setContentsMargins(0, 0, 0, 0)
        container_layout.setSpacing(5)
        
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
        
        # 创建字幕标签
        self.window = QLabel()
        self.window.setStyleSheet(f"""
            QLabel {{
                background-color: {config.BACKGROUND_COLOR};
                color: {config.TEXT_COLOR};
                font-size: {config.FONT_SIZE}px;
                font-family: {config.FONT_FAMILY};
                padding: {config.PADDING};
                border-radius: {config.BORDER_RADIUS}px;
                border: 2px solid {config.BORDER_COLOR};
                line-height: 1.5;
            }}
        """)
        self.window.setAlignment(Qt.AlignLeft | Qt.AlignBottom)
        self.window.setWordWrap(True)
        self.window.setTextFormat(Qt.RichText)  # live行灰色尾部/双层显示需要富文本
        self.window.setText("🎬 等待音频输入...")
        
        # 添加到布局
        btn_layout = QHBoxLayout()
        btn_layout.addStretch()
        btn_layout.addWidget(self.minimize_btn)
        btn_layout.addWidget(self.settings_btn)
        btn_layout.addWidget(self.quit_btn)
        container_layout.addLayout(btn_layout)
        container_layout.addWidget(self.window)
        
        self.container.setLayout(container_layout)
        # 设置窗口大小，确保不超出屏幕范围
        screen = self.app.primaryScreen().geometry()
        max_width = min(config.WINDOW_WIDTH, screen.width() - 100)
        max_height = min(config.WINDOW_HEIGHT + 40, screen.height() - 100)
        
        self.container.setGeometry(
            min(config.WINDOW_X, screen.width() - max_width),
            min(config.WINDOW_Y, screen.height() - max_height),
            max_width,
            max_height
        )
        self.container.show()
        
        # 创建设置窗口（初始隐藏）
        self.settings_window = SettingsWindow()
        
        # 连接信号到槽（线程安全）
        self.signals.update.connect(self._update_text)
        self.signals.status.connect(self._show_status)
        self.signals.live.connect(self._update_live)
        self.signals.pair.connect(self._add_pair)
        
        print("✅ 字幕窗口已创建")
        print(f"   位置: ({config.WINDOW_X}, {config.WINDOW_Y})")
        print(f"   大小: {config.WINDOW_WIDTH}x{config.WINDOW_HEIGHT}")
        print("   💡 鼠标拖动窗口可移动位置")
        print("   💡 点击 ➖ 按钮可最小化字幕")
        print("   💡 点击 ⚙️ 按钮可打开参数调节面板")
        print("   💡 点击 ❌ 按钮可退出程序")
    
    # ------------------------------------------------------------------
    # 线程安全的对外接口（从ASR/翻译线程调用）
    # ------------------------------------------------------------------
    def update_live(self, committed, unstable):
        """更新live德语行：committed=已提交未翻译（白），unstable=未稳定尾部（灰）"""
        self.signals.live.emit(committed or "", unstable or "")

    def add_pair(self, german, chinese):
        """一段德语翻译完成，加入历史句对"""
        self.signals.pair.emit(german or "", chinese or "")

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
        self.live_committed = committed
        self.live_unstable = unstable
        self.status_line = ""
        self._render()

    def _add_pair(self, german, chinese):
        self.sentence_pairs.append((german, chinese))
        max_pairs = getattr(config, "MAX_SENTENCE_PAIRS", 2)
        while len(self.sentence_pairs) > max_pairs:
            self.sentence_pairs.pop(0)
        self.status_line = ""
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

    def _render(self):
        """把句对历史+live行渲染成富文本"""
        blocks = []
        for german, chinese in self.sentence_pairs:
            lines = []
            if german and getattr(config, "SHOW_BILINGUAL", True):
                lines.append(html.escape(self._clip(german)))
            if chinese:
                lines.append(f'<span style="color:#c8c8c8">{html.escape(self._clip(chinese))}</span>')
            if lines:
                blocks.append("<br>".join(lines))

        live_parts = []
        if self.live_committed:
            live_parts.append(html.escape(self._clip(self.live_committed)))
        if self.live_unstable:
            color = getattr(config, "UNSTABLE_TEXT_COLOR", "#999999")
            live_parts.append(f'<span style="color:{color}"><i>{html.escape(self._clip(self.live_unstable))}</i></span>')
        if live_parts:
            blocks.append(" ".join(live_parts))

        if self.status_line:
            blocks.append(html.escape(self.status_line))

        if not blocks:
            return  # 什么都没有就保持现状（避免闪空白）

        self.window.setText("<br>".join(blocks))
        self.window.show()

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
