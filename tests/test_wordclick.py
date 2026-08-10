"""一次性验证脚本：点词查词的坐标命中数学。
真实创建 SubtitleWindow，喂字幕内容，在标签内合成点击，
断言 on_lookup 收到的是点击位置附近的德语词。"""
import torch  # noqa: F401 先于PyQt5加载（见main.py坑注释）
import sys

sys.stdout.reconfigure(encoding="utf-8")

import config
config.SHOW_PERFORMANCE = False
from subtitle_window import SubtitleWindow
from PyQt5.QtCore import QPoint, QPointF, Qt
from PyQt5.QtGui import QTextCursor

win = SubtitleWindow()
win.container.setGeometry(1750, 30, 700, 260)  # 避开可能在跑的字幕窗
win.app.processEvents()

# 喂内容：一条句对 + live行
win._add_pair("Der Bundestag hat heute entschieden.", "联邦议院今天做出了决定。")
win._update_live("Aber morgen wird es", "vielleicht anders")
win.app.processEvents()

captured = []
win.on_lookup = lambda word, ctx: captured.append((word, ctx))

# 用与点击处理相同的文档反推每个词的中心坐标，然后走完整点击路径验证
doc = win._build_doc(win._last_html)
content_x0, content_y0 = 22, 17
content_h = win.window.height() - 34
doc_y0 = content_y0 + max(0, content_h - doc.size().height())

failed = 0
for target in ("Bundestag", "entschieden", "morgen"):
    plain = doc.toPlainText()
    idx = plain.find(target)
    assert idx >= 0, f"文档里找不到 {target}"
    cursor = QTextCursor(doc)
    cursor.setPosition(idx + len(target) // 2)
    block = cursor.block()
    line = block.layout().lineForTextPosition(cursor.position() - block.position())
    x = line.cursorToX(cursor.position() - block.position())[0] + 3
    y = block.layout().position().y() + line.y() + line.height() / 2
    # 文档坐标 → 标签坐标 → 容器坐标（点击入口用的是容器坐标）
    label_pt = QPoint(int(x) + content_x0, int(y + doc_y0))
    click_pt = label_pt + win.window.pos()

    captured.clear()
    win._on_label_click(click_pt)
    got = captured[0][0] if captured else "(没触发)"
    ok = got == target
    failed += not ok
    print(f"{'OK ' if ok else 'FAIL'} 点击 {target}: 识别为 {got}")

# 点空白处不应触发
captured.clear()
win._on_label_click(win.window.pos() + QPoint(5, 5))  # 左上角空白（内容沉底）
ok = not captured
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} 点击空白: {'未触发' if ok else '误触发 ' + captured[0][0]}")

# 点中文不应触发
plain = doc.toPlainText()
idx = plain.find("联邦议院")
cursor = QTextCursor(doc)
cursor.setPosition(idx + 1)
block = cursor.block()
line = block.layout().lineForTextPosition(cursor.position() - block.position())
x = line.cursorToX(cursor.position() - block.position())[0]
y = block.layout().position().y() + line.y() + line.height() / 2
captured.clear()
win._on_label_click(QPoint(int(x) + content_x0, int(y + doc_y0)) + win.window.pos())
ok = not captured
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} 点击中文: {'未触发' if ok else '误触发 ' + captured[0][0]}")

sys.exit(1 if failed else 0)
