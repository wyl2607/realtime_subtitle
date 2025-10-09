"""
字幕悬浮窗UI模块
使用PyQt5实现置顶悬浮窗口
"""
import sys
from PyQt5.QtWidgets import QLabel, QApplication, QWidget, QVBoxLayout, QHBoxLayout, QSlider, QPushButton, QGroupBox
from PyQt5.QtCore import Qt, pyqtSignal, QObject
import config

class SubtitleSignals(QObject):
    """信号对象（用于线程安全的UI更新）"""
    update = pyqtSignal(str)


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
        
        layout = QVBoxLayout()
        
        # 音频时长设置
        duration_group = QGroupBox("音频时长设置")
        duration_layout = QVBoxLayout()
        
        self.min_duration_slider = self._create_slider(
            "最小音频时长", 0.5, 3.0, config.MIN_AUDIO_DURATION, 0.1,
            lambda v: setattr(config, 'MIN_AUDIO_DURATION', v)
        )
        self.max_duration_slider = self._create_slider(
            "最大音频时长", 1.0, 5.0, config.MAX_AUDIO_DURATION, 0.5,
            lambda v: setattr(config, 'MAX_AUDIO_DURATION', v)
        )
        self.silence_slider = self._create_slider(
            "静音检测时长", 0.3, 3.0, config.SILENCE_DURATION, 0.1,
            lambda v: setattr(config, 'SILENCE_DURATION', v)
        )
        
        duration_layout.addWidget(self.min_duration_slider['widget'])
        duration_layout.addWidget(self.max_duration_slider['widget'])
        duration_layout.addWidget(self.silence_slider['widget'])
        duration_group.setLayout(duration_layout)
        
        # 能量阈值设置
        energy_group = QGroupBox("能量阈值设置")
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
            "最大字符数", 50, 300, config.MAX_SUBTITLE_LENGTH, 10,
            lambda v: setattr(config, 'MAX_SUBTITLE_LENGTH', int(v))
        )
        
        self.audio_context_slider = self._create_slider(
            "识别音频窗口", 1, 10, config.AUDIO_CONTEXT_WINDOW, 1,
            lambda v: setattr(config, 'AUDIO_CONTEXT_WINDOW', int(v))
        )
        
        display_layout.addWidget(self.max_length_slider['widget'])
        display_layout.addWidget(self.audio_context_slider['widget'])
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
        
        # 滑块
        slider = QSlider(Qt.Horizontal)
        slider.setMinimum(int(min_val / step))
        slider.setMaximum(int(max_val / step))
        slider.setValue(int(current_val / step))
        
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
        """恢复默认值"""
        config.MIN_AUDIO_DURATION = 1.0
        config.MAX_AUDIO_DURATION = 2.0
        config.SILENCE_DURATION = 0.6
        config.ENERGY_THRESHOLD_SPEECH = 0.01
        config.MAX_SUBTITLE_LENGTH = 150
        config.AUDIO_CONTEXT_WINDOW = 5
        
        # 更新滑块位置
        self.min_duration_slider['slider'].setValue(int(1.0 / self.min_duration_slider['step']))
        self.max_duration_slider['slider'].setValue(int(2.0 / self.max_duration_slider['step']))
        self.silence_slider['slider'].setValue(int(0.6 / self.silence_slider['step']))
        self.speech_threshold_slider['slider'].setValue(int(0.01 / self.speech_threshold_slider['step']))
        self.max_length_slider['slider'].setValue(int(150 / self.max_length_slider['step']))
        self.audio_context_slider['slider'].setValue(int(5 / self.audio_context_slider['step']))
        
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
        
        # 字幕历史（保存最近3行）
        self.subtitle_history = []
        self.max_lines = 3
        
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
        
        print("✅ 字幕窗口已创建")
        print(f"   位置: ({config.WINDOW_X}, {config.WINDOW_Y})")
        print(f"   大小: {config.WINDOW_WIDTH}x{config.WINDOW_HEIGHT}")
        print("   💡 鼠标拖动窗口可移动位置")
        print("   💡 点击 ➖ 按钮可最小化字幕")
        print("   💡 点击 ⚙️ 按钮可打开参数调节面板")
        print("   💡 点击 ❌ 按钮可退出程序")
    
    def update_subtitle(self, text):
        """
        更新字幕（线程安全）
        可以从任何线程调用
        
        Args:
            text: 字幕文本
        """
        self.signals.update.emit(text)
    
    def _update_text(self, text):
        """
        内部更新方法（在主线程执行）
        
        Args:
            text: 字幕文本
        """
        if text and text.strip():
            # 限制字幕长度
            display_text = text
            if len(display_text) > config.MAX_SUBTITLE_LENGTH:
                display_text = display_text[:config.MAX_SUBTITLE_LENGTH] + "..."
            
            # 添加到历史（新内容显示在最下面）
            self.subtitle_history.append(display_text)
            
            # 保持最多3行
            if len(self.subtitle_history) > self.max_lines:
                self.subtitle_history.pop(0)  # 删除最旧的（最上面的）
            
            # 组合显示（第1行在最上，第3行在最下）
            combined_text = "\n".join(self.subtitle_history)
            
            # 更新显示
            self.window.setText(combined_text)
            self.window.show()
            
            if config.SHOW_PERFORMANCE:
                print(f"💬 字幕: {display_text[:50]}{'...' if len(display_text) > 50 else ''}")
        else:
            # 空文本不更新显示
            pass
    
    def _minimize_window(self):
        """最小化字幕窗口"""
        self.window.hide()
        if config.SHOW_PERFORMANCE:
            print("   ➖ 字幕已最小化")
    
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
