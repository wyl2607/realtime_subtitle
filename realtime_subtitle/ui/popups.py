"""
轻量弹窗：字幕历史回看窗 + 点词查词小窗 + AI 分析弹窗。
"""
import html
import time
from PyQt5.QtWidgets import QLabel, QWidget, QVBoxLayout, QTextEdit, QPushButton
from PyQt5.QtCore import Qt
import realtime_subtitle.config as config
from realtime_subtitle.ui.window_geometry import _screen_area_at


# 弹窗内操作按钮共用样式（深色半透明，压在圆角框里不抢戏）
_POPUP_BTN_STYLE = """
    QPushButton {
        background-color: rgba(45, 55, 70, 230);
        color: #d0e4ff;
        font-size: 13px;
        font-family: Microsoft YaHei, Arial;
        padding: 6px 10px;
        border-radius: 6px;
        border: 1px solid rgba(143, 184, 224, 0.45);
        text-align: left;
    }
    QPushButton:hover {
        background-color: rgba(60, 75, 95, 240);
        border: 1px solid rgba(143, 184, 224, 0.8);
    }
    QPushButton:disabled {
        color: #888;
        border: 1px solid rgba(120, 120, 120, 0.4);
    }
"""

_POPUP_LABEL_STYLE = """
    QLabel {
        background-color: rgba(25, 30, 40, 240);
        color: white;
        font-size: 16px;
        font-family: Microsoft YaHei, Arial;
        padding: 10px 14px;
        border-radius: 8px;
        border: 1px solid rgba(143, 184, 224, 0.6);
    }
"""


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


class _AnalysisPopupBase(QWidget):
    """WordPopup / AIAnalysisPopup 共用：深色圆角框 + 自动隐藏计时器 + 可选操作按钮。

    子控件 QPushButton 会自己吃掉点击，不会冒泡触发本窗 mousePressEvent——
    所以点按钮不会误关；点标签/空白仍关闭。
    """

    def __init__(self, max_width=420):
        super().__init__()
        self.setWindowFlags(Qt.WindowStaysOnTopHint | Qt.FramelessWindowHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.RichText)
        self.label.setStyleSheet(_POPUP_LABEL_STYLE)
        layout.addWidget(self.label)

        self.deep_btn = QPushButton("🔍 深度解释")
        self.deep_btn.setStyleSheet(_POPUP_BTN_STYLE)
        self.deep_btn.setCursor(Qt.PointingHandCursor)
        self.deep_btn.hide()
        self.deep_btn.clicked.connect(self._on_deep_clicked)
        layout.addWidget(self.deep_btn)

        self.web_btn = QPushButton("🌐 问更强的AI")
        self.web_btn.setStyleSheet(_POPUP_BTN_STYLE)
        self.web_btn.setCursor(Qt.PointingHandCursor)
        self.web_btn.hide()
        self.web_btn.clicked.connect(self._on_web_clicked)
        layout.addWidget(self.web_btn)

        self.setLayout(layout)
        self.setMaximumWidth(max_width)

        from PyQt5.QtCore import QTimer
        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)
        self._default_timeout_ms = 15000

        # 回调由 SubtitleWindow 接线；弹窗自己不碰 translator / webbrowser
        self.on_deep_explain = None   # () -> None，用弹窗已记住的 context
        self.on_open_web = None       # () -> None，用弹窗已记住的 web 问句
        self._context = ""            # 点词时的整句，给深度解释
        self._web_query = ""          # 跳网页用的自然语言问题

    def _restart_hide_timer(self, timeout_ms=None):
        """内容变化或用户点了操作按钮时重置——防止分析还在跑弹窗先被关掉。"""
        ms = self._default_timeout_ms if timeout_ms is None else timeout_ms
        self._hide_timer.stop()
        self._hide_timer.start(ms)

    def _place_and_show(self, global_pos, timeout_ms):
        self.adjustSize()
        screen = _screen_area_at(global_pos)
        if screen is None:
            self.move(global_pos.x() - 40, global_pos.y() - self.height() - 12)
            self.show()
            self.raise_()
            self._restart_hide_timer(timeout_ms)
            return
        x = min(max(screen.left(), global_pos.x() - 40), screen.right() - self.width())
        y = global_pos.y() - self.height() - 12
        if y < screen.top():
            y = global_pos.y() + 20
        self.move(x, y)
        self.show()
        self.raise_()
        self._restart_hide_timer(timeout_ms)

    def show_at(self, global_pos, html_text, timeout_ms=15000,
                show_deep=False, show_web=False, context="", web_query=""):
        """通用显示。show_deep/show_web 控制底部操作按钮；content 变了会重置计时器。"""
        self._context = context or ""
        self._web_query = web_query or ""
        self.label.setText(html_text)
        self.deep_btn.setVisible(bool(show_deep))
        self.deep_btn.setEnabled(True)
        self.web_btn.setVisible(bool(show_web))
        self._default_timeout_ms = timeout_ms
        self._place_and_show(global_pos, timeout_ms)

    def update_content(self, html_text, show_deep=False, show_web=False,
                       context=None, web_query=None, timeout_ms=None):
        """已显示时只换内容/按钮（不挪位置），并重置隐藏计时器。"""
        if context is not None:
            self._context = context
        if web_query is not None:
            self._web_query = web_query
        self.label.setText(html_text)
        self.deep_btn.setVisible(bool(show_deep))
        self.deep_btn.setEnabled(True)
        self.web_btn.setVisible(bool(show_web))
        self.adjustSize()
        ms = self._default_timeout_ms if timeout_ms is None else timeout_ms
        self._default_timeout_ms = ms
        self._restart_hide_timer(ms)
        if not self.isVisible():
            self.show()
        self.raise_()

    def _on_deep_clicked(self):
        # 按钮自己吃事件，不会走到 mousePressEvent；这里重置计时器等分析结果
        self._restart_hide_timer()
        self.deep_btn.setEnabled(False)
        if self.on_deep_explain:
            self.on_deep_explain(self._context)

    def _on_web_clicked(self):
        self._restart_hide_timer()
        if self.on_open_web:
            self.on_open_web(self._web_query)

    def mousePressEvent(self, event):
        # 点标签/空白关闭；子按钮不会冒泡到这里
        self.hide()


class WordPopup(_AnalysisPopupBase):
    """点词查词的小弹窗：跟着鼠标位置出现，自动消失，点空白也消失。

    查词结果出来后底部多一个「深度解释」；深度解释完成后再出「问更强的AI」。
    """

    def __init__(self):
        super().__init__(max_width=420)


class AIAnalysisPopup(_AnalysisPopupBase):
    """🤖 背景总结弹窗：样式与 WordPopup 一致，底部固定「问更强的AI」。"""

    def __init__(self):
        super().__init__(max_width=480)
        self.web_btn.setText("🌐 问更强的AI")
