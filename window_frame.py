"""
无边框窗口基础部件：拖动 + Windows 原生边缘缩放/标题栏拖动命中测试。
"""
import sys
from PyQt5.QtWidgets import QWidget, QApplication
from PyQt5.QtCore import Qt

if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes


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
    """无边框窗口 + Windows 原生边缘缩放 + 顶部标题条原生拖动。

    之前唯一的缩放入口是右下角一个透明的 QSizeGrip（看不见，而且窗口
    底边拖出屏幕后就彻底够不着了）。这里用 WM_NCHITTEST 命中测试把
    窗口四边/四角各 RESIZE_MARGIN 物理像素交给系统处理：鼠标移到边缘
    自动变双向箭头，拖拽即缩放，和普通窗口手感一致。

    顶部 DRAG_BAR_HEIGHT 区域（右侧按钮区除外）返回 HTCAPTION，由
    Windows 原生处理拖动——不依赖 Qt 子控件事件冒泡，和资源管理器
    标题栏一样稳。字幕正文区仍走 Qt 拖动（可与点词查词共存）。
    """

    RESIZE_MARGIN = 10  # 物理像素（不受DPI缩放影响：坐标和窗口矩形都是物理值）
    DRAG_BAR_HEIGHT = 30  # 顶部原生拖动条高度（物理像素，与 GetWindowRect 一致）
    BTN_RESERVE = 200     # 右上角按钮区不抢 HTCAPTION，留给按钮点击（5 个 30px 按钮+间距）

    def __init__(self):
        super().__init__()
        # 系统关窗（Alt+F4 / 任务栏关闭）回调；字幕主窗接到后应 app.quit()，
        # 设置/历史等附属窗不设，保持默认只关自己。
        self.on_system_close = None
        # WA_TranslucentBackground 下 alpha=0 的像素鼠标会直接穿透到下层窗口：
        # 顶部按钮行两侧、底部手柄行都是全透明的，导致上/下边缘抓不住
        # （左右边缘因为字幕标签是不透明黑色所以能抓）。
        # 注意：给顶层窗口本身设样式底色不生效（实测alpha=255也不上屏），
        # 必须用一个铺满窗口的子控件当底衬——alpha=2肉眼不可见，但整个
        # 窗口表面都能接住鼠标（test_hittest.py 有回归测试）
        self._underlay = QWidget(self)
        self._underlay.setObjectName("hitUnderlay")
        self._underlay.setAttribute(Qt.WA_StyledBackground, True)
        # 底衬只负责"可命中"，不抢走拖动/点击：事件交给顶层容器处理
        self._underlay.setAttribute(Qt.WA_TransparentForMouseEvents, True)
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
    _HTCAPTION = 2  # 系统标题栏：原生拖动

    def _drag_bar_is_visible(self):
        """HTCAPTION 门控：仅 drag_bar 可见时顶部才当标题条。
        drag_bar 挂在 SubtitleWindow.container 上；Settings/History 等
        同继承链窗口没有此属性，getattr 防御。"""
        bar = getattr(self, "drag_bar", None)
        if bar is None:
            return False
        try:
            return bar.isVisible()
        except RuntimeError:
            return False

    def _in_title_bar_region(self, x, y, rect):
        """物理像素坐标是否落在标题拖动条（非边缘缩放带、非右上按钮保留区）。"""
        m = self.RESIZE_MARGIN
        return (rect.top + m <= y < rect.top + self.DRAG_BAR_HEIGHT
                and rect.left + m <= x < rect.right - self.BTN_RESERVE)

    def nativeEvent(self, eventType, message):
        if sys.platform != "win32" or eventType not in (b"windows_generic_MSG", b"windows_dispatcher_MSG"):
            return False, 0
        msg = wintypes.MSG.from_address(int(message))

        # 无边框窗 HTCAPTION 区双击会触发最大化/还原——覆盖层绝不能被最大化
        if msg.message == 0x00A3:  # WM_NCLBUTTONDBLCLK
            x = ctypes.c_short(msg.lParam & 0xFFFF).value
            y = ctypes.c_short((msg.lParam >> 16) & 0xFFFF).value
            rect = wintypes.RECT()
            ctypes.windll.user32.GetWindowRect(int(self.winId()), ctypes.byref(rect))
            if self._in_title_bar_region(x, y, rect):
                return True, 0
            return False, 0

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
        # 顶部拖动条仅在 drag_bar 可见时返回 HTCAPTION，否则走 HTCLIENT
        # （隐藏时不能留一条看不见的吸拖区挡住点词查词）
        if self._drag_bar_is_visible() and self._in_title_bar_region(x, y, rect):
            return True, self._HTCAPTION
        return False, 0

    def closeEvent(self, event):
        """Alt+F4 / 任务栏关闭：有 on_system_close 就走应用退出，否则只关本窗。

        主字幕窗设了 setQuitOnLastWindowClosed(False)（设置/历史附属窗关了
        不能拖垮进程），若不接管 closeEvent，Alt+F4 只会藏掉悬浮窗，进程
        继续占 Mutex/GPU/pid——用户再启会提示已在运行。
        """
        cb = getattr(self, "on_system_close", None)
        if cb is not None:
            try:
                cb()
            except Exception:
                pass
            event.accept()
            return
        super().closeEvent(event)


