"""一次性验证脚本：窗口垂直缩放自由度。

bug（第五版实测）：QLabel wordWrap 下最小高度=全部文本排版高度，而句对
自适应填充又总把窗口填满 → 布局最小高度≈当前高度 → 窗口只能拉大不能缩小
（棘轮）。左右能调是因为折行让最小宽度很小——和用户描述完全一致。
"""
import torch  # noqa: F401 先于PyQt5（见main.py坑注释）
import sys

sys.stdout.reconfigure(encoding="utf-8")

import config
config.SHOW_PERFORMANCE = False
from subtitle_window import SubtitleWindow

win = SubtitleWindow()
win.container.setGeometry(1750, 30, 700, 800)
win.app.processEvents()

# 填10条长句对，把窗口填满（触发棘轮的前提）
for i in range(10):
    win._add_pair(
        f"Der Bundestag hat heute Nummer {i} eine sehr wichtige und lange Entscheidung getroffen, die alle betrifft.",
        f"联邦议院今天第{i}号做出了一项非常重要而且很长的决定，涉及到所有人的利益。")
win.app.processEvents()

failed = 0

# 修复方案是"标签不进布局"——容器不该再有布局（布局会把wordWrap标签的
# height-for-width当最小高度，在事件循环里把窗口弹回去）
ok = win.container.layout() is None
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} 容器无布局: {win.container.layout()}")

# 实际尝试缩小到 700x250，并跑几轮事件循环确认不被弹回
win.container.resize(700, 250)
for _ in range(5):
    win.app.processEvents()
h = win.container.height()
ok = h <= 260
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} resize(700,250)+5轮事件循环后高度: {h}px（应≈250，之前会弹回731）")

# 标签应铺满缩小后的窗口
ok = win.window.geometry() == win.container.rect()
failed += not ok
print(f"{'OK ' if ok else 'FAIL'} 标签铺满窗口: label={win.window.geometry()} 容器={win.container.rect()}")

# 缩小后内容自适应：仍应显示至少1条句对且不报错
win._update_live("Noch ein Satz kommt", "vielleicht")
win.app.processEvents()
print("OK  缩小后渲染无异常")

sys.exit(1 if failed else 0)
