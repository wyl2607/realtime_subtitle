"""一次性验证脚本：ResizableFramelessWidget 的 WM_NCHITTEST 边缘命中测试。
真实创建窗口后用 SendMessage 合成命中测试消息，断言各边/角/内部返回码。
（torch必须先于PyQt5导入——见main.py的坑注释，这里import translator_queue太重，
直接import torch占位即可）"""
import torch  # noqa: F401 先于PyQt5加载，避免c10.dll WinError 1114
import sys
import ctypes

sys.stdout.reconfigure(encoding="utf-8")
from PyQt5.QtWidgets import QApplication
from subtitle_window import ResizableFramelessWidget
from PyQt5.QtCore import Qt

app = QApplication(sys.argv)
w = ResizableFramelessWidget()
w.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool | Qt.WindowStaysOnTopHint)
w.setAttribute(Qt.WA_TranslucentBackground)  # 和真实字幕窗一致（曾导致鼠标穿透）
# 放到屏幕右上角：避开可能正在运行的字幕悬浮窗（同为置顶窗口，
# 会盖住测试窗口导致 WindowFromPoint 误判）
w.setGeometry(1750, 30, 400, 200)
w.show()
app.processEvents()

hwnd = int(w.winId())
rect = ctypes.wintypes.RECT()
ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))

def hit(x, y):
    lparam = (y << 16) | (x & 0xFFFF)
    return ctypes.windll.user32.SendMessageW(hwnd, 0x0084, 0, lparam)

cx, cy = (rect.left + rect.right) // 2, (rect.top + rect.bottom) // 2
cases = [
    ("右边缘", rect.right - 3, cy, 11),   # HTRIGHT
    ("左边缘", rect.left + 3, cy, 10),    # HTLEFT
    ("上边缘", cx, rect.top + 3, 12),     # HTTOP
    ("下边缘", cx, rect.bottom - 3, 15),  # HTBOTTOM
    ("右下角", rect.right - 3, rect.bottom - 3, 17),  # HTBOTTOMRIGHT
    ("左上角", rect.left + 3, rect.top + 3, 13),      # HTTOPLEFT
    ("内部", cx, cy, 1),                  # HTCLIENT（交回Qt默认处理）
]
failed = 0
for name, x, y, expect in cases:
    got = hit(x, y)
    ok = got == expect
    failed += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: got={got} expect={expect}")

# 半透明窗口鼠标穿透检查：alpha=0 的像素 WindowFromPoint 会落到下层窗口，
# 上/下边缘就抓不住了（第五版实际发生过）。alpha=2 底色应保证整个表面可命中
_WindowFromPoint = ctypes.windll.user32.WindowFromPoint
_WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
_WindowFromPoint.restype = ctypes.wintypes.HWND
print(f"(本窗口 hwnd={hex(hwnd)})")
for name, x, y in [("穿透-顶边", cx, rect.top + 3),
                   ("穿透-底边", cx, rect.bottom - 3),
                   ("穿透-中心", cx, cy)]:
    at = _WindowFromPoint(ctypes.wintypes.POINT(x, y))
    ok = at == hwnd
    failed += not ok
    print(f"{'OK ' if ok else 'FAIL'} {name}: WindowFromPoint={'本窗口' if ok else hex(at or 0)}")
sys.exit(1 if failed else 0)
