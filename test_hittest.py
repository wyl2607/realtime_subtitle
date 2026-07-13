"""一次性验证脚本：ResizableFramelessWidget 的 WM_NCHITTEST 边缘命中测试。
真实创建窗口后用 SendMessage 合成命中测试消息，断言各边/角/内部返回码。
（torch必须先于PyQt5导入——见main.py的坑注释，这里import translator_queue太重，
直接import torch占位即可）"""
import torch  # noqa: F401 先于PyQt5加载，避免c10.dll WinError 1114
import sys
import ctypes
from ctypes import wintypes

sys.stdout.reconfigure(encoding="utf-8")
from PyQt5.QtWidgets import QApplication, QLabel
from subtitle_window import ResizableFramelessWidget
from PyQt5.QtCore import Qt

app = QApplication(sys.argv)
w = ResizableFramelessWidget()
w.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
w.setAttribute(Qt.WA_TranslucentBackground)  # 和真实字幕窗一致（曾导致鼠标穿透）
# 放到屏幕右上角：避开可能正在运行的字幕悬浮窗（同为置顶窗口，
# 会盖住测试窗口导致 WindowFromPoint 误判）
w.setGeometry(1750, 30, 400, 200)
# 挂上 drag_bar（主程序里挂在 container 上；nativeEvent 用 isVisible 门控 HTCAPTION）
w.drag_bar = QLabel("drag", w)
w.drag_bar.setGeometry(0, 0, 400, ResizableFramelessWidget.DRAG_BAR_HEIGHT)
w.drag_bar.show()
w.show()
app.processEvents()

hwnd = int(w.winId())
rect = wintypes.RECT()
ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

def hit(x, y):
    lparam = (y << 16) | (x & 0xFFFF)
    return ctypes.windll.user32.SendMessageW(hwnd, 0x0084, 0, lparam)

def make_msg(message, wparam, lparam):
    """构造 MSG，返回可供 nativeEvent 第二参使用的地址（int）。"""
    msg = wintypes.MSG()
    msg.hWnd = hwnd
    msg.message = message
    msg.wParam = wparam
    msg.lParam = lparam
    # 保持引用，避免地址失效
    make_msg._keep = msg  # noqa: store last
    return ctypes.addressof(msg)

cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
# 标题拖动条中心（避开边缘缩放带 RESIZE_MARGIN=10 和右上按钮保留区）
drag_y = rect.top + 18
# 右上 BTN_RESERVE 区内一点（避开右边缘 RESIZE_MARGIN=10）
btn_x = rect.right - 80

failed = 0

# --- 基础边缘/内部 + drag_bar 可见时的 HTCAPTION ---
cases = [
    ("右边缘", rect.right - 3, cy, 11),   # HTRIGHT
    ("左边缘", rect.left + 3, cy, 10),    # HTLEFT
    ("上边缘", cx, rect.top + 3, 12),     # HTTOP
    ("下边缘", cx, rect.bottom - 3, 15),  # HTBOTTOM
    ("右下角", rect.right - 3, rect.bottom - 3, 17),  # HTBOTTOMRIGHT
    ("左上角", rect.left + 3, rect.top + 3, 13),      # HTTOPLEFT
    ("标题拖动条(可见)", cx, drag_y, 2),  # HTCAPTION（drag_bar 可见）
    ("右上按钮保留区", btn_x, drag_y, 1), # BTN_RESERVE 不抢 HTCAPTION → HTCLIENT
    ("内部", cx, cy, 1),                  # HTCLIENT（交回Qt默认处理）
]
for name, x, y, expect in cases:
    got = hit(x, y)
    ok = got == expect
    failed += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: got={got} expect={expect}")

# --- drag_bar 隐藏时同一标题坐标应回落 HTCLIENT，不能留看不见的吸拖区 ---
w.drag_bar.hide()
app.processEvents()
got = hit(cx, drag_y)
ok = got == 1
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} 标题拖动条(隐藏): got={got} expect=1")

# 恢复可见
w.drag_bar.show()
app.processEvents()

# --- WM_NCLBUTTONDBLCLK：标题区必须吞掉，窗口不得最大化 ---
WM_NCLBUTTONDBLCLK = 0x00A3
HTCAPTION = 2
if ctypes.windll.user32.IsZoomed(hwnd):
    ctypes.windll.user32.ShowWindow(hwnd, 9)  # SW_RESTORE
    app.processEvents()

lparam = ((drag_y & 0xFFFF) << 16) | (cx & 0xFFFF)

# a) nativeEvent 直接断言吞掉（return True, 0）
addr = make_msg(WM_NCLBUTTONDBLCLK, HTCAPTION, lparam)
handled, result = w.nativeEvent(b"windows_generic_MSG", addr)
ok = (handled is True) and (result == 0)
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} nativeEvent吞双击: handled={handled} result={result}")

# b) SendMessage 后 IsZoomed 仍为假（系统路径兜底）
ctypes.windll.user32.SendMessageW(hwnd, WM_NCLBUTTONDBLCLK, HTCAPTION, lparam)
app.processEvents()
zoomed = bool(ctypes.windll.user32.IsZoomed(hwnd))
ok = not zoomed
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} 标题双击不最大化: IsZoomed={zoomed}")

# 半透明窗口鼠标穿透检查：alpha=0 的像素 WindowFromPoint 会落到下层窗口，
# 上/下边缘就抓不住了（第五版实际发生过）。alpha=2 底色应保证整个表面可命中
_WindowFromPoint = ctypes.windll.user32.WindowFromPoint
_WindowFromPoint.argtypes = [wintypes.POINT]
_WindowFromPoint.restype = wintypes.HWND
print(f"(本窗口 hwnd={hex(hwnd)})")
for name, x, y in [("穿透-顶边", cx, rect.top + 3),
                   ("穿透-底边", cx, rect.bottom - 3),
                   ("穿透-中心", cx, cy)]:
    at = _WindowFromPoint(wintypes.POINT(x, y))
    ok = at == hwnd
    failed += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: WindowFromPoint={'本窗口' if ok else hex(at or 0)}")
sys.exit(1 if failed else 0)
