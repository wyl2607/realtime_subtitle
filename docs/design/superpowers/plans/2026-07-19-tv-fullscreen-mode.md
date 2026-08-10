# 电视全屏模式（📺 TV Mode）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增独立全屏黑底大字滚动窗口（只显中文翻译），供电视/副屏远距离阅读；顺手把主窗字号上限 36 放宽到 72。

**Architecture:** 新文件 `tv_window.py` 的 `TVWindow(QWidget)` 照 `popups.py::HistoryWindow` 的 QTextEdit 滚动流模式实现（黑底不透明全屏窗，**不用**主窗那套手动布局/WM_NCHITTEST/半透明穿透方案）。数据接线在 `subtitle_render.py` 的主线程槽 `_add_pair`/`_update_draft` 里各加一行调用；入口是主窗按钮条新 📺 按钮。字号/所在屏持久化进 `window_state.json` 的 `"tv"` 段。

**Tech Stack:** PyQt5（QTextEdit/QTextCursor/QScreen）、pytest。规格见 `docs/superpowers/specs/2026-07-19-tv-fullscreen-mode-design.md`。

**每个任务开始前必读的坑（来自 CLAUDE.md，违反必炸）：**
- 测试文件第一行 `import torch  # noqa: F401`，必须先于任何 PyQt5 import（否则 WinError 1114）。
- 测试必须持模块级 `_APP = QApplication` 引用（否则 GC → qFatal 秒退，退出码 127 无输出）。
- 不 import main.py（单实例 Mutex 会 sys.exit）。
- 用户可见文案中文；注释写"为什么"。
- 测试命令一律 `venv\Scripts\python -m pytest ...`（项目独立 venv）。

---

### Task 1: config 新增 TV 参数 + TVWindow 核心（追加/草稿/字号）

**Files:**
- Modify: `config.py`（`FONT_SIZE = 22` 附近，~99 行）
- Create: `tv_window.py`
- Create: `test_tv_window.py`

- [ ] **Step 1: config.py 加参数**

在 `config.py` 的 `FONT_SIZE = 22  # 字体大小` 一行后面加：

```python
# ============ 电视全屏模式（📺）============
TV_FONT_SIZE = 64       # 全屏大字字号（Ctrl+滚轮调节）
TV_FONT_SIZE_MIN = 24
TV_FONT_SIZE_MAX = 160
```

- [ ] **Step 2: 写失败测试 test_tv_window.py**

```python
"""📺 电视全屏模式单测：句对追加/草稿替换/字号钳制/屏幕索引解析。

运行: venv\\Scripts\\python.exe -m pytest test_tv_window.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114（见 main.py / test_hittest.py）。
⚠️ QApplication 必须持有模块级引用，否则会被立即 GC → 建 QWidget 时 qFatal 秒退。
"""
import torch  # noqa: F401  先于 PyQt5
import sys

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication

import config
from tv_window import TVWindow


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _shown_tv():
    """建一个普通 show（非全屏）的 TVWindow：append/draft 有 isVisible 门控。"""
    _app()
    win = TVWindow()
    win.resize(400, 300)
    win.show()
    return win


def test_append_pair_appends_blocks():
    win = _shown_tv()
    try:
        win.append_pair("第一句")
        win.append_pair("第二句")
        assert win.text.toPlainText() == "第一句\n第二句"
    finally:
        win.hide()


def test_empty_or_blank_chinese_is_ignored():
    win = _shown_tv()
    try:
        win.append_pair("")
        win.append_pair("   ")
        win.append_pair(None)
        assert win.text.toPlainText() == ""
    finally:
        win.hide()


def test_draft_occupies_last_block_and_gets_replaced_by_pair():
    win = _shown_tv()
    try:
        win.append_pair("正式一")
        win.update_draft("草稿中")
        assert win.text.toPlainText() == "正式一\n草稿中"
        # 草稿只更新不叠加
        win.update_draft("草稿更长了")
        assert win.text.toPlainText() == "正式一\n草稿更长了"
        # 正式句对到达：草稿退场，被正式文本替换
        win.append_pair("正式二")
        assert win.text.toPlainText() == "正式一\n正式二"
        # 空草稿 = 清掉草稿行
        win.update_draft("又一条草稿")
        win.update_draft("")
        assert win.text.toPlainText() == "正式一\n正式二"
    finally:
        win.hide()


def test_hidden_window_drops_content():
    _app()
    win = TVWindow()  # 不 show
    win.append_pair("看不见就别攒")
    win.update_draft("草稿也别攒")
    assert win.text.toPlainText() == ""


def test_backfill_replaces_document():
    win = _shown_tv()
    try:
        win.append_pair("旧的")
        win.backfill(["一", "二", "三"])
        assert win.text.toPlainText() == "一\n二\n三"
    finally:
        win.hide()


def test_adjust_font_clamps_and_syncs_config():
    _app()
    snap = config.TV_FONT_SIZE
    try:
        win = TVWindow()
        config.TV_FONT_SIZE = config.TV_FONT_SIZE_MAX - 2
        win._adjust_font(+1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MAX  # +4 被钳到上限
        win._adjust_font(+1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MAX  # 不越界
        config.TV_FONT_SIZE = config.TV_FONT_SIZE_MIN + 2
        win._adjust_font(-1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MIN
        win._adjust_font(-1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MIN
    finally:
        config.TV_FONT_SIZE = snap


def test_clamp_screen_index():
    _app()
    win = TVWindow()
    n = len(QApplication.screens())
    assert win._clamp_screen_index(0) == 0
    assert win._clamp_screen_index(-3) == 0
    assert win._clamp_screen_index(n + 5) == n - 1  # 拔了屏的持久化值钳回
    assert win._clamp_screen_index(None) == 0
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd C:\Users\wyl26\realtime_subtitle && venv\Scripts\python -m pytest test_tv_window.py -q`
Expected: FAIL/ERROR（`ModuleNotFoundError: No module named 'tv_window'`）

- [ ] **Step 4: 实现 tv_window.py**

```python
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
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd C:\Users\wyl26\realtime_subtitle && venv\Scripts\python -m pytest test_tv_window.py -q`
Expected: 7 passed

注意：`test_clamp_screen_index` 里 `n + 5` 钳到 `n-1`、单屏机器上 `cycle_screen` 直接 return——如果本机只有一块屏跑出意外结果，检查 `_clamp_screen_index` 而不是改测试。

- [ ] **Step 6: Commit**

```bash
cd C:\Users\wyl26\realtime_subtitle
git add config.py tv_window.py test_tv_window.py
git commit -m "feat: 电视全屏模式核心窗口（滚动大字/草稿替换/Ctrl+滚轮字号/多屏）"
```

---

### Task 2: SubtitleWindow 集成（📺按钮 / 数据接线 / 持久化）

**Files:**
- Modify: `subtitle_window.py`（imports ~39 行、`__init__` 按钮区 ~137 行与辅助窗区 ~297 行、`_save_state_if_changed` ~383 行、新增 `_toggle_tv` 方法）
- Modify: `subtitle_render.py:51`（`_add_pair`）与 `subtitle_render.py:47`（`_update_draft`）
- Modify: `window_frame.py:96`（`BTN_RESERVE`）
- Modify: `window_chrome.py`（`_position_chrome` 无需改——btn_bar 自适应宽度）
- Test: `test_tv_window.py`（追加集成用例）

- [ ] **Step 1: 追加失败的集成测试**

在 `test_tv_window.py` 末尾追加（模式照抄 test_ui_polish：STATE_FILE 指去 tempdir）：

```python
def test_subtitle_window_integration_state_and_wiring():
    """SubtitleWindow 建出 tv_window；state 含 tv 段；_add_pair/_update_draft 接线到位。"""
    import os
    import json
    import tempfile
    import subtitle_window as sw_mod
    from subtitle_window import SubtitleWindow

    _app()
    tmpdir = tempfile.mkdtemp()
    orig_state = sw_mod.STATE_FILE
    snap_font = config.TV_FONT_SIZE
    sw_mod.STATE_FILE = os.path.join(tmpdir, "window_state.json")
    try:
        win = SubtitleWindow()
        assert isinstance(win.tv_window, TVWindow)
        assert win.tv_btn.toolTip()  # 📺 按钮存在

        # 接线：句对/草稿都转发到 tv_window（tv 窗显示时）
        win.tv_window.resize(400, 300)
        win.tv_window.show()
        win._add_pair("Hallo", "你好")
        assert "你好" in win.tv_window.text.toPlainText()
        win._update_draft("草稿")
        assert "草稿" in win.tv_window.text.toPlainText()
        win.tv_window.hide()

        # 持久化：tv 段写进 state 文件
        config.TV_FONT_SIZE = 72
        win.tv_window.screen_index = 0
        win._save_state_if_changed()
        with open(sw_mod.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        assert state["tv"] == {"font_size": 72, "screen_index": 0}

        win.container.close()
    finally:
        sw_mod.STATE_FILE = orig_state
        config.TV_FONT_SIZE = snap_font


def test_state_restore_tv_font_size():
    """启动时 window_state.json 里的 tv.font_size 恢复进 config。"""
    import os
    import json
    import tempfile
    import subtitle_window as sw_mod
    from subtitle_window import SubtitleWindow

    _app()
    tmpdir = tempfile.mkdtemp()
    orig_state = sw_mod.STATE_FILE
    snap_font = config.TV_FONT_SIZE
    sw_mod.STATE_FILE = os.path.join(tmpdir, "window_state.json")
    try:
        with open(sw_mod.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"tv": {"font_size": 96, "screen_index": 99}}, f)
        win = SubtitleWindow()
        assert config.TV_FONT_SIZE == 96
        # 越界 screen_index 原样存着，_go_fullscreen 时才钳制（拔屏不崩）
        assert win.tv_window.screen_index == 99
        win.container.close()
    finally:
        sw_mod.STATE_FILE = orig_state
        config.TV_FONT_SIZE = snap_font
```

- [ ] **Step 2: 跑测试确认失败**

Run: `venv\Scripts\python -m pytest test_tv_window.py -q`
Expected: 2 failed（`AttributeError: ... 'tv_window'`），原 7 项仍 passed

- [ ] **Step 3: 实现集成**

`window_frame.py`：`BTN_RESERVE = 160` → `BTN_RESERVE = 200`，注释改成
`BTN_RESERVE = 200     # 右上角按钮区不抢 HTCAPTION，留给按钮点击（5 个 30px 按钮+间距）`

`subtitle_window.py`：

(a) import 行：`from popups import HistoryWindow, WordPopup` 下面加
```python
from tv_window import TVWindow
```

(b) `__init__` 里创建 📺 按钮（放在 history_btn 创建代码之后）：
```python
        # 创建电视全屏按钮
        self.tv_btn = QPushButton("📺")
        self.tv_btn.setFixedSize(30, 30)
        self.tv_btn.setStyleSheet(button_style)
        self.tv_btn.clicked.connect(self._toggle_tv)
        self.tv_btn.setToolTip("电视全屏模式（大字滚动，Esc 退出）")
```

(c) btn_bar 装配行改为（原来是 4 个按钮的 for）：
```python
        for b in (self.minimize_btn, self.history_btn, self.tv_btn,
                  self.settings_btn, self.quit_btn):
```

(d) `__init__` 里创建 tv_window（放在 `self.history_window = HistoryWindow()` 之后）：
```python
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
```

(e) 新方法 `_toggle_tv`（放在 `_toggle_history` 后面）：
```python
    def _toggle_tv(self):
        """切换电视全屏窗：打开时回填最近中文，开在主字幕窗不在的屏上"""
        if self.tv_window.isVisible():
            self.tv_window.hide()
        else:
            self.tv_window.backfill([zh for _, zh in self.sentence_pairs])
            self.tv_window.open_fullscreen(
                avoid_center=self.container.frameGeometry().center())
```

(f) `_save_state_if_changed` 里（`state["tuning"] = ...` 之前）加：
```python
        state["tv"] = {"font_size": int(config.TV_FONT_SIZE),
                       "screen_index": self.tv_window.screen_index}
```

`subtitle_render.py`：

(g) `_update_draft` 加转发：
```python
    def _update_draft(self, chinese):
        self.live_draft = chinese
        self.tv_window.update_draft(chinese)
        self._render()
```

(h) `_add_pair` 加转发（`self.history_window.append_pair(...)` 之后）：
```python
        self.tv_window.append_pair(chinese)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `venv\Scripts\python -m pytest test_tv_window.py -q`
Expected: 9 passed

- [ ] **Step 5: Commit**

```bash
git add subtitle_window.py subtitle_render.py window_frame.py test_tv_window.py
git commit -m "feat: 📺按钮/句对草稿接线/tv状态持久化，BTN_RESERVE 160→200"
```

---

### Task 3: 主窗字号上限 36 → 72

**Files:**
- Modify: `settings_window.py:241`（字号滑块范围）
- Modify: `window_chrome.py:84`（Ctrl+滚轮钳制）
- Test: `test_tv_window.py`（追加一个钳制用例）

- [ ] **Step 1: 追加失败测试**

```python
def test_main_window_font_cap_raised_to_72():
    """主窗字号上限放宽：滑块最大值 72，Ctrl+滚轮在 71 时还能 +1。"""
    from settings_window import SettingsWindow

    _app()
    snap = config.FONT_SIZE
    try:
        win = SettingsWindow()
        s = win.font_size_slider
        assert round(s['slider'].maximum() * s['step']) == 72
    finally:
        config.FONT_SIZE = snap
```

（Ctrl+滚轮路径依赖完整 SubtitleWindow + 事件合成，不好单测；window_chrome 的 `min(36,...)` 改动靠 Step 3 的 grep 核对，两处上限必须一致。）

- [ ] **Step 2: 跑测试确认失败**

Run: `venv\Scripts\python -m pytest test_tv_window.py::test_main_window_font_cap_raised_to_72 -q`
Expected: FAIL（maximum 是 36）

- [ ] **Step 3: 改两处上限**

`settings_window.py` 240-242 行：
```python
        self.font_size_slider = self._create_slider(
            "字体大小", 14, 72, config.FONT_SIZE, 1, set_font_size
        )
```

`window_chrome.py` `_on_wheel` 里：
```python
        size = max(14, min(72, config.FONT_SIZE + step))
```

核对没有第三处钳制：
Run: `grep -n "36" settings_window.py window_chrome.py subtitle_render.py`
Expected: 无字号相关的 36 残留（其它含义的 36 不动）

- [ ] **Step 4: 跑测试确认通过**

Run: `venv\Scripts\python -m pytest test_tv_window.py -q`
Expected: 10 passed

- [ ] **Step 5: Commit**

```bash
git add settings_window.py window_chrome.py test_tv_window.py
git commit -m "feat: 主窗字号上限 36→72（滑块+Ctrl+滚轮两处同步）"
```

---

### Task 4: 全量回归 + 独立脚本套件 + 文档

**Files:**
- Modify: `AI_README.md`（追加本轮记录）
- Run only: 全部测试

- [ ] **Step 1: 全量 pytest**

Run: `cd C:\Users\wyl26\realtime_subtitle && venv\Scripts\python -m pytest -q`
Expected: 全绿（50+ 项 + 新增 10 项）。已知：test_ui_polish 两个 fade 用例对动画计时敏感，偶发挂单独重跑即绿，别当回归追。

- [ ] **Step 2: 独立脚本套件（BTN_RESERVE 改了必须跑 hittest）**

Run: `venv\Scripts\python test_hittest.py`
Expected: 全 OK（`btn_x = rect.right - 80` 在 200px 保留区内，语义不变）
注意：跑的时候字幕程序若在运行，测试窗口位置要避开字幕窗（同为置顶会互相盖）。

Run: `venv\Scripts\python test_resize_freedom.py`
Expected: 全 OK

- [ ] **Step 3: AI_README.md 追加记录**

在 `## 2026-07-13 深夜` 小节**之前**插入：

```markdown
## 2026-07-19: 📺 电视全屏模式（副屏/电视大字滚动）

用户在电视上看直播、旁边屏幕全屏看中文翻译。新增 `tv_window.py::TVWindow`：
独立全屏黑底大字窗（照 HistoryWindow 的 QTextEdit 滚动流，刻意不用主窗
手动布局/命中测试那套），只显中文；正式句对逐段追加，草稿/流式中文
（浅蓝斜体）永远只占最后一个 block 原地更新、正式句到达即替换；
上翻回看时不自动跟底（at_bottom 判定同 HistoryWindow）。

- 入口：按钮条新 📺 按钮（BTN_RESERVE 160→200）；Esc/✖ 关闭；🖥 换屏轮换。
- 多屏默认开在主字幕窗**不在**的那块屏；`_go_fullscreen` 前先 showNormal
  （已全屏时直接 setGeometry 不跨屏）。
- 字号 Ctrl+滚轮一格 4px，钳 24–160（TV_FONT_SIZE*，config.py）；
  eventFilter 拦在 QTextEdit 自带 Ctrl+滚轮 zoom 之前（那个不走 config）。
- 持久化：window_state.json 新增 `"tv": {font_size, screen_index}`；
  越界 screen_index（拔屏）在 _go_fullscreen 时钳制，恢复时不动。
- 隐藏时 append/draft 直接丢（不攒），打开时从 sentence_pairs 回填。
- 顺手：主窗字号上限 36→72（settings_window 滑块 + window_chrome 滚轮，
  两处必须一致）。
- 测试：test_tv_window.py 10 项（pytest 收集）。
```

- [ ] **Step 4: Commit**

```bash
git add AI_README.md
git commit -m "docs: 电视全屏模式记录进 AI_README"
```

---

### Task 5: 真机验收（需要用户/真窗口环境）

- [ ] **Step 1: 启动程序实测**

双击桌面「启动字幕.bat」（或 `powershell -File start_subtitles.ps1`），放一段德语视频，验证：
1. 鼠标移入字幕窗 → 按钮条出现 📺；点击 → 另一块屏（或本屏）全屏黑底。
2. 中文大字逐句滚入；草稿浅蓝斜体先出、正式翻译到达替换。
3. Ctrl+滚轮字号变化流畅；普通滚轮上翻回看时新句不抢滚动位置。
4. 🖥 换屏轮换正常；Esc/✖ 关闭后主窗一切照旧；再开时回填最近句子。
5. 重启程序：字号/上次屏幕恢复。

- [ ] **Step 2: 结果反馈**

有问题回到对应 Task 修；全过则完成。
```
