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
w.setWindowFlags(Qt.FramelessWindowHint | Qt.Tool)
w.setGeometry(300, 300, 400, 200)
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
sys.exit(1 if failed else 0)
