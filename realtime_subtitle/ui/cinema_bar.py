"""
🎞 影院字幕条：叠在全屏视频最下方的 Netflix/YouTube 式字幕。

☠️ **和 📺 电视全屏（tv_window.py）是两个东西，别互相抄实现**：
    📺 TVWindow    不透明黑底**全屏**窗，副屏/电视场景。盖在视频上=画面全黑。
    🎞 CinemaBar   **透明覆盖层**，只在屏幕底部占一条，视频照常看得见。
两者可以同时开（一块屏放视频、另一块当电视），所以是两个独立窗口而不是一个
窗口两种模式。

三条设计约束，都是"叠在别人画面上"这件事逼出来的：

1. **天生鼠标穿透，没有关掉的开关**。悬浮主窗的穿透是可切换的（Ctrl+Alt+M），
   因为那个窗要能拖动/点词。本条**没有任何可交互元素**，全屏看剧时它压在
   播放器进度条和控制栏上方——只要有一帧能吃到点击，用户就会以为播放器坏了。
   所以 WS_EX_TRANSPARENT 在 show() 里无条件设，不提供关闭路径。
2. **不抢焦点**。WindowDoesNotAcceptFocus + ShowWithoutActivating：全屏视频
   一旦丢焦点，很多播放器（含浏览器全屏）会退出全屏或弹出控制栏。
3. **文字自带底衬，不给整条加背景**。整条铺一个半透明黑会在亮场景里像块补丁；
   YouTube 的做法是每行文字**各自**贴一个刚好包住自己的圆角黑底，行宽跟着
   文字走。所以底衬做在 QLabel 上（padding + border-radius），窗口本身
   WA_TranslucentBackground 全透。

调用约定和 TVWindow 一致：append_pair/update_draft 都在主线程槽里被调
（subtitle_render），本窗不需要自己的信号层。
"""
import sys
import ctypes

from PyQt5.QtWidgets import QApplication, QWidget, QVBoxLayout, QLabel
from PyQt5.QtCore import Qt, QTimer

import realtime_subtitle.config as config


class CinemaBar(QWidget):
    """屏幕底部的透明字幕条：只显示最新一句，静默一段时间后自动淡出。"""

    def __init__(self):
        super().__init__()
        self.setWindowTitle("🎞 影院字幕条")
        # Tool 而不是 Window：不进任务栏/Alt+Tab（它是覆盖层不是应用窗口）
        self.setWindowFlags(
            Qt.FramelessWindowHint
            | Qt.WindowStaysOnTopHint
            | Qt.Tool
            | Qt.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WA_ShowWithoutActivating, True)
        # Qt 层的穿透（跨平台兜底）；Windows 上真正生效的是 show() 里那道
        # WS_EX_TRANSPARENT，两道都设是因为 Qt 这条在部分合成路径下不可靠
        self.setAttribute(Qt.WA_TransparentForMouseEvents, True)

        self.screen_index = None  # None = 跟随主字幕窗所在屏
        self._ct_applied = False  # 原生穿透标志是否已经打上（只需一次）

        layout = QVBoxLayout()
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(6)
        layout.addStretch(1)  # 内容贴底：上方留白吸收高度差

        self.german_label = self._make_label(german=True)
        self.chinese_label = self._make_label(german=False)
        layout.addWidget(self.german_label, 0, Qt.AlignHCenter | Qt.AlignBottom)
        layout.addWidget(self.chinese_label, 0, Qt.AlignHCenter | Qt.AlignBottom)
        self.setLayout(layout)

        # 静默淡出计时器：每来一句就重置，超时把两行都清掉
        self._hold_timer = QTimer(self)
        self._hold_timer.setSingleShot(True)
        self._hold_timer.timeout.connect(self.clear_text)

        self._apply_font()

    @staticmethod
    def _make_label(german):
        lb = QLabel("")
        lb.setAlignment(Qt.AlignCenter)
        lb.setWordWrap(True)
        lb.setTextFormat(Qt.PlainText)  # 字幕是纯文本，别让 < > 被当标签解析
        lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lb.hide()  # 没内容时整行不占位，也不显示空底衬
        return lb

    # ------------------------------------------------------------------
    # 内容
    # ------------------------------------------------------------------
    def append_pair(self, german, chinese):
        """新句对上屏。影院条只显示**最新一句**，不滚动、不留历史。

        要回看历史请用 📜 历史窗或 📺 电视窗——在视频上叠多行会挡画面，
        而挡画面正是这个条要解决的问题。
        """
        if not self.isVisible():
            return
        chinese = (chinese or "").strip()
        if not chinese:
            return
        self._set_line(self.german_label, (german or "").strip()
                       if getattr(config, "CINEMA_SHOW_GERMAN", True) else "")
        self._set_line(self.chinese_label, chinese, draft=False)
        self._restart_hold()

    def update_draft(self, chinese):
        """草稿中文：原地替换中文行，德语行保持不动（它已经是定稿原文）。

        空串 = 撤掉草稿。☠️ 撤稿时**不能连德语行一起清**：草稿退场的正常路径
        是"正式翻译到了"，紧接着 append_pair 会把两行都重写；中间那一拍如果
        把德语也清掉，画面上会闪一下。
        """
        if not self.isVisible():
            return
        chinese = (chinese or "").strip()
        if not chinese:
            return
        self._set_line(self.chinese_label, chinese, draft=True)
        self._restart_hold()

    def clear_text(self):
        """两行都清掉（静默超时/切换语言/关闭时）。"""
        self._set_line(self.german_label, "")
        self._set_line(self.chinese_label, "")

    @staticmethod
    def _set_line(label, text, draft=False):
        if not text:
            label.setText("")
            label.hide()
            return
        label.setText(text)
        # 草稿用斜体蓝，和悬浮窗/电视窗同一套语义
        label.setProperty("draft", bool(draft))
        label.style().unpolish(label)
        label.style().polish(label)
        label.show()

    def _restart_hold(self):
        hold = float(getattr(config, "CINEMA_HOLD_SEC", 6.0) or 0)
        self._hold_timer.stop()
        if hold > 0:
            self._hold_timer.start(int(hold * 1000))

    # ------------------------------------------------------------------
    # 字号 / 样式
    # ------------------------------------------------------------------
    def adjust_font(self, direction):
        """一格 2px。

        ☠️ 本窗鼠标穿透，**收不到自己的滚轮事件**，所以不能像 TVWindow 那样
        装 eventFilter 自己处理 Ctrl+滚轮。调用方是 ⚙️ 面板里的字号控件。
        """
        size = int(config.CINEMA_FONT_SIZE) + 2 * (1 if direction > 0 else -1)
        size = max(config.CINEMA_FONT_SIZE_MIN,
                   min(config.CINEMA_FONT_SIZE_MAX, size))
        if size != config.CINEMA_FONT_SIZE:
            config.CINEMA_FONT_SIZE = size
            self._apply_font()
            self._relayout()

    def _apply_font(self):
        zh = int(config.CINEMA_FONT_SIZE)
        de = max(12, int(zh * float(getattr(config, "CINEMA_GERMAN_RATIO", 0.62))))
        alpha = int(getattr(config, "CINEMA_BG_ALPHA", 150))
        fam = config.FONT_FAMILY
        draft_color = getattr(config, "DRAFT_TEXT_COLOR", "#8fb8e0")

        def block(size, color, italic=False):
            return (f"background-color: rgba(0,0,0,{alpha});"
                    f"color: {color};"
                    f"font-family: {fam};"
                    f"font-size: {size}px;"
                    f"{'font-style: italic;' if italic else ''}"
                    f"border-radius: 6px;"
                    f"padding: 2px 14px;")

        # 德语行：偏灰，它是参考不是主角
        self.german_label.setStyleSheet(f"QLabel {{{block(de, '#d0d0d0')}}}")
        # 中文行：白；draft 属性为真时转成斜体蓝（草稿）
        self.chinese_label.setStyleSheet(
            f"QLabel {{{block(zh, '#ffffff')}}}"
            f"QLabel[draft=\"true\"] {{{block(zh, draft_color, italic=True)}}}")

    # ------------------------------------------------------------------
    # 几何 / 显示
    # ------------------------------------------------------------------
    @staticmethod
    def _clamp_screen_index(idx):
        n = len(QApplication.screens())
        if not isinstance(idx, int) or idx < 0:
            return 0
        return min(idx, n - 1)

    def _target_screen_index(self, prefer_center=None):
        """默认开在**主字幕窗所在**那块屏——和 📺 正好相反。

        📺 是"另一块屏当电视"，所以它避开主窗；影院条是叠在你正在看的那个
        视频上，那个视频就在你放字幕窗的这块屏上。
        """
        if self.screen_index is not None:
            return self._clamp_screen_index(self.screen_index)
        if prefer_center is not None:
            for i, s in enumerate(QApplication.screens()):
                if s.geometry().contains(prefer_center):
                    return i
        return 0

    def _relayout(self):
        """按当前屏幕算几何：占满屏宽、贴底，高度给到屏高的 40%（长句换行余量）。"""
        screens = QApplication.screens()
        if not screens:
            return
        geo = screens[self._clamp_screen_index(self.screen_index or 0)].geometry()
        margin = int(getattr(config, "CINEMA_BOTTOM_MARGIN", 64))
        h = int(geo.height() * 0.4)
        self.setGeometry(geo.x(), geo.y() + geo.height() - h - margin, geo.width(), h)
        # 文字最大宽度：太宽读起来要转头，也更容易压到画面主体
        ratio = float(getattr(config, "CINEMA_MAX_WIDTH_RATIO", 0.8))
        maxw = max(200, int(geo.width() * ratio))
        self.german_label.setMaximumWidth(maxw)
        self.chinese_label.setMaximumWidth(maxw)

    def open_on(self, prefer_center=None):
        self.screen_index = self._target_screen_index(prefer_center)
        self._apply_font()
        self._relayout()
        self.show()
        self._apply_click_through()
        self.raise_()

    def cycle_screen(self):
        screens = QApplication.screens()
        if len(screens) < 2:
            return
        self.screen_index = (self._clamp_screen_index(self.screen_index) + 1) % len(screens)
        self._relayout()
        self.raise_()

    def _apply_click_through(self):
        """原生 WS_EX_TRANSPARENT：让点击**穿过**字幕条落到下面的播放器上。

        ☠️ 必须在 show() **之后**打——窗口句柄要真正创建出来才有 EXSTYLE 可改。
        照 subtitle_window._toggle_click_through 的同一套写法，区别是本窗只
        开不关（理由见模块 docstring 第 1 条）。
        """
        if sys.platform != "win32" or self._ct_applied:
            return
        GWL_EXSTYLE = -20
        WS_EX_TRANSPARENT = 0x20
        WS_EX_LAYERED = 0x80000
        hwnd = int(self.winId())
        ex = ctypes.windll.user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        ctypes.windll.user32.SetWindowLongW(
            hwnd, GWL_EXSTYLE, ex | WS_EX_TRANSPARENT | WS_EX_LAYERED)
        SWP_FLAGS = 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020
        ctypes.windll.user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, SWP_FLAGS)
        self._ct_applied = True

    def hideEvent(self, event):
        self._hold_timer.stop()
        super().hideEvent(event)

    def showEvent(self, event):
        """每次显示都重算几何：两次打开之间用户可能改了分辨率/插拔了显示器。

        ☠️ 这里**不要**改成监听屏幕变更事件。PyQt5 没有暴露任何 screen 相关的
        QEvent（`hasattr(QEvent, "ScreenChangeInternal")` 是 False，`dir(QEvent)`
        里一个带 screen 的都没有），写了就是在 changeEvent 里访问不存在的属性
        抛 AttributeError——而 **PyQt5 对虚函数重写里的未捕获异常是直接
        abort() 整个进程**，不是往上抛。表现为退出码 127、连 traceback 都没有，
        本项目 2026-08-28 加这个窗时就这么崩过一次，测试跑到 90% 直接消失。
        真要跟随热插拔，用 QApplication.screenAdded/screenRemoved 信号。
        """
        self._relayout()
        super().showEvent(event)
