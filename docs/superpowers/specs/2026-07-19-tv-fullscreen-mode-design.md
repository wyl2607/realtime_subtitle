# 电视全屏模式（📺 TV Mode）设计

日期：2026-07-19
状态：用户已确认（形态/内容/滚动三项均选推荐方案，设计整体批准）

## 背景与目标

用户在电视上看德语直播，想在旁边的屏幕上全屏显示大号中文翻译。
现有字幕悬浮窗字号钳制在 14–36px（settings_window.py 滑块 +
window_chrome.py Ctrl+滚轮两处），远距离看不清，且悬浮窗形态不适合
整屏阅读。

目标：新增一个独立的全屏滚动大字窗口（TVWindow），小字幕悬浮窗照常
工作，两窗并存，互不影响。

## 需求（用户已确认）

1. **形态**：独立全屏窗口，可指定显示在哪块屏幕；开关不影响主窗。
2. **内容**：只显中文翻译（不显德语原文）。
3. **滚动**：新句从底部进入自动上滚（历史流）；往上滚可回看，停回
   底部自动跟进。字号滚轮直接调，上限放大到 160px，设置持久化。

## 设计

### 新文件 `tv_window.py` — `TVWindow(QWidget)`

照 `popups.py::HistoryWindow` 的成熟模式实现（QTextEdit 滚动流），
**不用**主窗那套手动 setGeometry 布局 / WM_NCHITTEST / 半透明窗方案
——黑底不透明全屏窗没有那些坑，QTextEdit 白送滚动条和回看。

- 无边框（FramelessWindowHint）黑底，`showFullScreen()` 到目标 QScreen。
- 多屏时默认打开在**主字幕窗不在的那块屏**；单屏就本屏。
- QTextEdit 只读；每条中文一段；`document().setMaximumBlockCount(2000)`
  防长场变卡。
- **at_bottom 判定**沿用 HistoryWindow：`sb.value() >= sb.maximum() - 10`
  时新内容自动滚到底，用户上翻回看时不打扰。
- **草稿中文**：流式/草稿翻译（浅蓝斜体）实时显示在文末，正式句对到达
  后替换该行。实现方式：草稿永远只占"最后一段"，正式句对插在草稿段
  之前（或先删草稿段再追加正式段+新草稿段）。
- **字号**：`config.TV_FONT_SIZE`（默认 64），Ctrl+滚轮调节，钳制
  24–160。普通滚轮留给 QTextEdit 原生滚动回看。
- **交互**：Esc 关闭；右上角悬浮「🖥」按钮轮换到下一块屏幕（鼠标移入
  显示，平时隐藏或半透明）。
- **持久化**：`window_state.json` 新增 `"tv"` 段：
  `{"font_size": int, "screen_index": int}`。读写走 subtitle_window
  现有的 state 保存/恢复链路。screen_index 越界（拔了屏）回退默认逻辑。

### 接线（subtitle_render.py / subtitle_window.py）

- `SubtitleWindow` 持有 `self.tv_window`（懒创建或启动即建，随主窗
  state 恢复）。
- `_add_pair(german, chinese)` 里追加 `self.tv_window.append_pair(chinese)`
  （只传中文；chinese 为空的句对跳过）。
- `_update_draft(chinese)` 里追加 `self.tv_window.update_draft(chinese)`。
- 两个调用都发生在主线程槽函数里，TVWindow 不需要自己的信号层。
- TVWindow 隐藏时这两个调用直接 return（不攒不渲染，省事；打开时
  可从 `sentence_pairs` 回填最近 HISTORY_KEEP 条的中文）。

### 入口（subtitle_window.py 按钮条）

- 按钮条新增 📺 按钮（在 📜 旁），点击开/关 TVWindow。
- `window_frame.py` 的 `BTN_RESERVE` 160 → 200（按钮保留区加宽，
  避免 HTCAPTION 拖动区吃掉新按钮）。

### 顺手修主窗字号上限

- `settings_window.py` 字号滑块 14–36 → 14–72。
- `window_chrome.py` `_on_wheel` 钳制 max 36 → 72。
- 二者必须一致（滑块 setValue 同步链路依赖范围）。

### config.py 新增

```python
TV_FONT_SIZE = 64        # 电视全屏模式字号（Ctrl+滚轮 24–160）
TV_FONT_SIZE_MIN = 24
TV_FONT_SIZE_MAX = 160
```

## 错误处理

- screen_index 持久化值越界/为 None → 走默认屏选择逻辑。
- 草稿到达但 TVWindow 未显示 → 直接丢弃（主窗自己有草稿显示）。
- 全屏窗关闭（Esc/📺 再点）只 hide 不销毁，重开保滚动位置无所谓
  （重开时滚到底）。

## 测试

新增 `test_tv_window.py`（pytest 收集；照 test_settings_sync 的
`_app()`+`_APP` 模块级 QApplication 写法）：

1. 字号 Ctrl+滚轮钳制在 24–160；改变后 config.TV_FONT_SIZE 同步。
2. append_pair 追加正式段、update_draft 只更新末尾草稿段、正式句对
   到达后草稿段被替换（断言 document 段数与文本）。
3. state 持久化：save 输出含 "tv" 段；restore 越界 screen_index 不崩。
4. 空中文句对不产生新段。
5. at_bottom 跟随：模拟上翻后 append 不动滚动条。

回归：`venv\Scripts\python -m pytest` 全量绿（50+ 项）；
test_hittest.py 独立套件跑一遍（BTN_RESERVE 改了，按钮保留区用例
若硬编码 160 需同步更新）。

## 明确不做（YAGNI）

- 全屏窗不做双语切换（用户已选只中文；以后要了再加开关）。
- 不做全屏窗独立设置面板；字号即唯一可调项，滚轮解决。
- 不做点词查词/鼠标穿透（那是悬浮窗的场景）。
- 不动翻译管线/识别管线任何逻辑。
