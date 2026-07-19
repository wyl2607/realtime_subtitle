"""
📺 电视全屏模式：独立全屏黑底大字窗口，只滚动显示中文翻译。

照 HistoryWindow 的 QTextEdit 滚动流模式——黑底不透明全屏窗没有
半透明悬浮窗那些坑（穿透/手动布局/命中测试），不要往那套方案上靠。
调用约定：append_pair/update_draft 都在主线程槽里被调（subtitle_render），
本窗不需要自己的信号层。
"""
import html
from PyQt5.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QTextEdit, QPushButton, QShortcut,
)
from PyQt5.QtCore import Qt, QEvent, QTimer
from PyQt5.QtGui import QTextCursor, QTextBlockFormat, QKeySequence
import config


class TVWindow(QWidget):
    """全屏滚动大字窗：新句从底部进入自动上滚；上翻回看时不打扰。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("📺 电视全屏字幕")
        # 置顶：电视/副屏上不该被任务栏或别的窗口盖住
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint)
        # 持久化恢复用；None = 首开走默认选屏（非主字幕窗所在屏）
        self.screen_index = None
        self._has_draft = False  # 草稿永远只占文档最后一个 block

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        self.text = QTextEdit()
        self.text.setReadOnly(True)
        self.text.setFrameStyle(0)
        # 长场封顶：几小时能攒上千句，无限增长会越来越卡（同 HistoryWindow）
        self.text.document().setMaximumBlockCount(2000)
        layout.addWidget(self.text)
        self.setLayout(layout)
        self._apply_font()

        # Ctrl+滚轮调字号要抢在 QTextEdit 自带的 zoom 之前拦下来，
        # 普通滚轮放行给 QTextEdit 原生滚动（回看）
        self.text.viewport().installEventFilter(self)

        # Esc 关闭（WindowShortcut 上下文：焦点在 QTextEdit 里也生效）
        QShortcut(QKeySequence(Qt.Key_Escape), self, activated=self.hide)

        # 角落操作钮：全屏无边框窗没有系统按钮，鼠标用户需要出口
        btn_style = """
            QPushButton {
                background-color: rgba(40, 40, 40, 200);
                color: rgba(220, 220, 220, 220);
                border: 1px solid rgba(255, 255, 255, 0.25);
                border-radius: 14px;
                font-size: 14px;
            }
            QPushButton:hover { background-color: rgba(80, 80, 80, 230); }
        """
        self.screen_btn = QPushButton("🖥", self)
        self.screen_btn.setFixedSize(28, 28)
        self.screen_btn.setStyleSheet(btn_style)
        self.screen_btn.setToolTip("移到下一块屏幕")
        self.screen_btn.clicked.connect(self.cycle_screen)
        self.close_btn = QPushButton("✖", self)
        self.close_btn.setFixedSize(28, 28)
        self.close_btn.setStyleSheet(btn_style)
        self.close_btn.setToolTip("关闭全屏字幕（Esc）")
        self.close_btn.clicked.connect(self.hide)

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def append_pair(self, chinese):
        """正式中文句子追加为新段；替换掉当前草稿行（草稿已被它取代）。"""
        if not self.isVisible():
            return  # 藏着就不攒：打开时由 backfill 回填最近内容
        if not chinese or not chinese.strip():
            return
        sb = self.text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 10
        self._remove_draft()
        self._append_block(html.escape(chinese.strip()), draft=False)
        if at_bottom:
            sb.setValue(sb.maximum())

    def update_draft(self, chinese):
        """草稿/流式中文只占最后一个 block，原地更新；空串 = 撤掉草稿行。"""
        if not self.isVisible():
            return
        sb = self.text.verticalScrollBar()
        at_bottom = sb.value() >= sb.maximum() - 10
        self._remove_draft()
        chinese = (chinese or "").strip()
        if chinese:
            self._append_block(html.escape(chinese), draft=True)
        if at_bottom:
            sb.setValue(sb.maximum())

    def backfill(self, chinese_list):
        """打开时回填最近句子（清空重建；隐藏期间内容不做增量维护）。"""
        self.text.clear()
        self._has_draft = False
        for zh in chinese_list:
            if zh and zh.strip():
                self._append_block(html.escape(zh.strip()), draft=False)
        sb = self.text.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _append_block(self, escaped, draft):
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        if not (cursor.atStart() and cursor.atEnd()):  # 文档非空才另起一段
            fmt = QTextBlockFormat()
            # 段间距跟字号走，远看才分得清句子
            fmt.setTopMargin(int(config.TV_FONT_SIZE * 0.35))
            cursor.insertBlock(fmt)
        if draft:
            color = getattr(config, "DRAFT_TEXT_COLOR", "#8fb8e0")
            cursor.insertHtml(f'<i style="color:{color}">{escaped}</i>')
        else:
            cursor.insertHtml(f'<span style="color:#f0f0f0">{escaped}</span>')
        self._has_draft = bool(draft)

    def _remove_draft(self):
        if not self._has_draft:
            return
        cursor = QTextCursor(self.text.document())
        cursor.movePosition(QTextCursor.End)
        # BlockUnderCursor 连同前面的段分隔符一起选中删除
        cursor.select(QTextCursor.BlockUnderCursor)
        cursor.removeSelectedText()
        self._has_draft = False

    # ------------------------------------------------------------------
    # 字号
    # ------------------------------------------------------------------
    def eventFilter(self, obj, event):
        if event.type() == QEvent.Wheel and event.modifiers() & Qt.ControlModifier:
            self._adjust_font(+1 if event.angleDelta().y() > 0 else -1)
            return True  # 拦掉 QTextEdit 自带 Ctrl+滚轮 zoom（不走 config 不持久化）
        return super().eventFilter(obj, event)

    def _adjust_font(self, direction):
        """一格 4px：24–160 的量程用 1px 步进要滚一百多格。"""
        size = int(config.TV_FONT_SIZE) + 4 * (1 if direction > 0 else -1)
        size = max(config.TV_FONT_SIZE_MIN, min(config.TV_FONT_SIZE_MAX, size))
        if size != config.TV_FONT_SIZE:
            config.TV_FONT_SIZE = size
            self._apply_font()

    def _apply_font(self):
        # 已有内容里的 span 没写死 font-size，改样式表即全文生效（同 HistoryWindow）
        self.text.setStyleSheet(f"""
            QTextEdit {{
                background-color: rgb(0, 0, 0);
                color: #f0f0f0;
                font-size: {int(config.TV_FONT_SIZE)}px;
                font-family: {config.FONT_FAMILY};
                border: none;
                padding: 24px 48px;
            }}
        """)

    # ------------------------------------------------------------------
    # 屏幕
    # ------------------------------------------------------------------
    @staticmethod
    def _clamp_screen_index(idx):
        """持久化的屏幕号可能越界（拔了显示器）；None/负数一律回 0。"""
        n = len(QApplication.screens())
        if not isinstance(idx, int) or idx < 0:
            return 0
        return min(idx, n - 1)

    @staticmethod
    def _default_screen_index(avoid_center):
        """默认开在主字幕窗不在的那块屏（电视/副屏场景）；单屏就本屏。"""
        screens = QApplication.screens()
        if avoid_center is not None and len(screens) > 1:
            for i, s in enumerate(screens):
                if not s.geometry().contains(avoid_center):
                    return i
        return 0

    def open_fullscreen(self, avoid_center=None):
        idx = self.screen_index
        if idx is None:
            idx = self._default_screen_index(avoid_center)
        self._go_fullscreen(idx)

    def cycle_screen(self):
        screens = QApplication.screens()
        if len(screens) < 2:
            return
        self._go_fullscreen((self._clamp_screen_index(self.screen_index) + 1)
                            % len(screens))

    def _go_fullscreen(self, idx):
        idx = self._clamp_screen_index(idx)
        self.screen_index = idx
        geo = QApplication.screens()[idx].geometry()
        # 已全屏时直接 setGeometry 不会跨屏：先退回普通态再全屏到目标屏
        self.showNormal()
        self.setGeometry(geo)
        self.showFullScreen()
        # 全屏几何生效在事件循环里，滚到底要排在它之后
        QTimer.singleShot(0, lambda: self.text.verticalScrollBar().setValue(
            self.text.verticalScrollBar().maximum()))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        # 角落按钮绝对定位右上角（QTextEdit 铺满，按钮浮在上面）
        x = self.width() - self.close_btn.width() - 16
        self.close_btn.move(x, 12)
        self.screen_btn.move(x - self.screen_btn.width() - 8, 12)
        self.close_btn.raise_()
        self.screen_btn.raise_()
