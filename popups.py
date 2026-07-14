"""
轻量弹窗：字幕历史回看窗 + 点词查词小窗。
"""
import html
import time
from PyQt5.QtWidgets import QLabel, QWidget, QVBoxLayout, QTextEdit
from PyQt5.QtCore import Qt
import config

from window_geometry import _screen_area_at


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
        # 往上偏移显示，避免挡住刚点的词；钳在点击位置所在屏（副屏点词不能飞到主屏）
        screen = _screen_area_at(global_pos)
        if screen is None:
            self.move(global_pos.x() - 40, global_pos.y() - self.height() - 12)
            self.show()
            self.raise_()
            self._hide_timer.start(timeout_ms)
            return
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


