"""
轻量弹窗：字幕历史回看窗 + 点词查词小窗 + AI 分析弹窗。
"""
import html
import time
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QWidget, QVBoxLayout,
    QTextEdit, QPushButton,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QTextCursor
import realtime_subtitle.config as config
import realtime_subtitle.ui.design_tokens as design_tokens
from realtime_subtitle.ui.window_geometry import _screen_area_at
from realtime_subtitle.ui.history_store import HistoryEvent, HistoryRow, SubtitleHistoryStore


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

    def __init__(self, store: SubtitleHistoryStore):
        super().__init__()
        self.store = store
        self.setWindowTitle("📜 字幕历史（本场）")
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint)
        self.setGeometry(150, 120, 720, 560)

        layout = QVBoxLayout()
        controls = QHBoxLayout()
        self.search_box = QLineEdit()
        self.search_box.setPlaceholderText("搜索原文或译文")
        self.search_box.textChanged.connect(self._rebuild)
        self.copy_btn = QPushButton("复制全文")
        self.copy_btn.clicked.connect(self._copy_all)
        self.export_btn = QPushButton("导出 SRT")
        self.export_btn.clicked.connect(self._export_srt)
        controls.addWidget(self.search_box)
        controls.addWidget(self.copy_btn)
        controls.addWidget(self.export_btn)
        layout.addLayout(controls)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
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

        self.hint = hint = QLabel("往上滚动可回看；停在底部时新字幕自动跟进。完整记录在 transcripts\\ 目录。")
        hint.setStyleSheet("color: #888; font-size: 12px;")
        layout.addWidget(hint)
        self.setLayout(layout)
        self._dirty = True
        self.store.subscribe(self._on_history_event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._dirty:
            self._rebuild()

    def _on_history_event(self, event: HistoryEvent):
        if not self.isVisible():
            # 藏着不重建（2000 条 insertHtml 不便宜，而 update 每句都可能来），打开时补
            self._dirty = True
            return
        if event.kind == "insert" and event.index == len(self.store) - 1 and not self.search_box.text().strip():
            rows = self.store.rows()
            self._append_row(self.store.get(event.keys[0]), rows[-2] if len(rows) > 1 else None)
            return
        self._rebuild()

    def _rows(self):
        return self.store.search(self.search_box.text())

    def _append_row(self, row: HistoryRow | None, previous: HistoryRow | None = None):
        if row is None:
            return
        sb = self.text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 10
        cursor = self.text.textCursor()
        # ☠️ 写成 `cursor.End` 会漏过第 1 步那道枚举守卫（它只看 `Q…` 开头的
        # 前缀），Qt6 下这里是当场 AttributeError。见 test_qt_enum_style。
        cursor.movePosition(QTextCursor.MoveOperation.End)
        if previous is not None and previous.revision != row.revision:
            label = self.store.language_label(row.revision)
            divider = f"── 切换到 {label} ──" if label else "── 切换语言 ──"
            cursor.insertHtml(f'<span style="color:#777">{html.escape(divider)}</span><br>')
        stamp = time.strftime("%H:%M:%S", time.localtime(row.wall))
        source = "远端" if row.source == "remote" else design_tokens.TEXT_STATUS_LOCAL
        color = design_tokens.COLOR_UNTRANSLATED if row.status != "ok" else "#9ad0ff"
        block = (
            f'<span style="color:#777">[{stamp}] [{source}]</span> {html.escape(row.src)}<br>'
            f'<span style="color:{color}">{html.escape(row.dst)}</span><br><br>'
        )
        cursor.insertHtml(block)
        if at_bottom:
            sb.setValue(sb.maximum())

    def _rebuild(self, *_):
        self._dirty = False
        sb = self.text.verticalScrollBar()
        val = sb.value()
        at_bottom = val >= sb.maximum() - 10
        self.text.clear()
        cursor = self.text.textCursor()
        cursor.movePosition(QTextCursor.MoveOperation.End)
        previous = None
        for row in self._rows():
            self._append_row(row, previous)
            previous = row
        if at_bottom:
            sb.setValue(sb.maximum())
        else:
            sb.setValue(val)

    def _copy_all(self):
        QApplication.clipboard().setText(self.store.to_text(self._rows()))

    def _export_srt(self):
        default = f"subtitles_{time.strftime('%Y%m%d_%H%M%S')}.srt"
        path, _ = QFileDialog.getSaveFileName(self, "导出 SRT", default, "SRT 文件 (*.srt)")
        if not path:
            return
        try:
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.store.to_srt(self._rows()))
        except OSError as e:
            # 槽里抛出去会 abort 整个进程；写不进去（权限/盘满）只提示
            self.hint.setText(f"⚠️ 导出失败：{e}")
            return
        self.hint.setText(f"已导出 {path}")


class _AnalysisPopupBase(QWidget):
    """WordPopup / AIAnalysisPopup 共用：深色圆角框 + 自动隐藏计时器 + 可选操作按钮。

    子控件 QPushButton 会自己吃掉点击，不会冒泡触发本窗 mousePressEvent——
    所以点按钮不会误关；点标签/空白仍关闭。
    """

    def __init__(self, max_width=420):
        super().__init__()
        self.setWindowFlags(Qt.WindowType.WindowStaysOnTopHint | Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.label = QLabel()
        self.label.setWordWrap(True)
        self.label.setTextFormat(Qt.TextFormat.RichText)
        self.label.setStyleSheet(_POPUP_LABEL_STYLE)
        layout.addWidget(self.label)

        self.deep_btn = QPushButton("🔍 深度解释")
        self.deep_btn.setStyleSheet(_POPUP_BTN_STYLE)
        self.deep_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.deep_btn.hide()
        self.deep_btn.clicked.connect(self._on_deep_clicked)
        layout.addWidget(self.deep_btn)

        self.web_btn = QPushButton("🌐 问更强的AI")
        self.web_btn.setStyleSheet(_POPUP_BTN_STYLE)
        self.web_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self.web_btn.hide()
        self.web_btn.clicked.connect(self._on_web_clicked)
        layout.addWidget(self.web_btn)

        self.setLayout(layout)
        self.setMaximumWidth(max_width)

        from PyQt6.QtCore import QTimer
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
