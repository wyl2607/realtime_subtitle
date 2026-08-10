# AI 分析按钮 — 设计

2026-08-04，用户提出：想要一个"AI分析"入口——本地模型先给背景解释/总结，
不够深入时能一键跳转到更强的网页版 AI 追问。brainstorming 会话确认的范围。

## 目标

1. 悬浮窗新增 🤖 按钮：分析"最近几分钟"的直播内容，给出背景/上下文总结。
2. 现有点词查词弹窗（`WordPopup`）新增"深度解释"升级路径：从单词解释
   升级到整句背景/含义解释。
3. 两个场景都遵循同一个模式：**本地 Ollama 先出结果 → 弹窗里附一个
   "问更强的AI"链接跳转网页版**（默认 Grok，可配置换成 ChatGPT 等）。

## 明确不在这次范围内

- **查词本身变慢的问题**：用户在这次讨论中反馈现有点词查词已经感觉慢，
  这是既有功能的性能问题（大概率是 `_lookup_executor` 单线程池排队/
  GPU 争用），需要单独诊断，不在本次功能设计里顺带"顺手修"，避免把两件
  不同性质的事情（新功能 vs 性能bug）混进一次改动里说不清楚。
- 电视全屏模式（`tv_window.py`）不接入这个入口，它本来就是纯展示窗口。
- 不做"点句子"这种全新的鼠标交互（右键/长按等）；深度解释复用现有的
  点词弹窗，弹窗里加按钮即可，理由见下方"查词弹窗升级"一节。

## 架构改动

### 1. 配置项（`config.py`）

```python
AI_ANALYSIS_WEB_URL_TEMPLATE = "https://grok.com/?q={query}"  # {query} 会被 URL-encode 替换
AI_CONTEXT_WINDOW_MINUTES = 5      # 🤖按钮分析最近几分钟的内容
AI_CONTEXT_MAX_CHARS = 2000        # 喂给本地模型的原文上限，超出截断最旧的部分
```

`AI_ANALYSIS_WEB_URL_TEMPLATE` 是可配置项而不是硬编码 Grok——用户可能想换
成 ChatGPT（`https://chatgpt.com/?q={query}&hints=search`）或别的，两者都
已验证支持 URL 带参数直接跳转打开。

### 2. `sentence_pairs` 加时间戳

现状（`subtitle_window.py:114`）：`self.sentence_pairs = []`，每项是
`(german, chinese)` 二元组，没有时间信息，无法按"最近N分钟"过滤。

改为三元组 `(german, chinese, timestamp)`，`timestamp` 用 `time.time()`。
涉及的既有代码（已核实，只有这4处）：

- `subtitle_window.py:114` 初始化注释同步更新
- `subtitle_render.py:53` `_add_pair` 里 `append` 时多存一个时间戳
- `subtitle_render.py:188` `_render` 里 `for g, c in self.sentence_pairs`
  → `for g, c, _ in self.sentence_pairs`
- `subtitle_window.py:592` TV回填 `[zh for _, zh in self.sentence_pairs]`
  → `[zh for _, zh, _ in self.sentence_pairs]`

`test_ui_polish.py:262` 的 `win.sentence_pairs.clear()` 不受影响（清空操作
和元组形状无关）。

### 3. 🤖 工具栏按钮（背景总结场景）

和现有 `➖📜📺⚙️❌` 一排（`subtitle_window.py` 148-176行），新增：

```python
self.ai_btn = QPushButton("🤖")
```

`window_frame.py:96` 的 `BTN_RESERVE = 200`（"5 个 30px 按钮+间距"的注释）
要跟着改成 6 个按钮的宽度。**改完必须重跑 `test_hittest.py`**——项目自己在
`CLAUDE.md` 第4节踩坑清单里点名过这个规律（2026-07-19 加宽 BTN_RESERVE 到
200 时，400px 测试窗中心点压中保留区边界导致两个用例失败，当时把测试窗宽度
从 400 加到了 500；这次同理，需要检查测试窗宽度是否又不够了）。

点击流程：

1. 从 `self.sentence_pairs` 过滤 `timestamp >= now - AI_CONTEXT_WINDOW_MINUTES*60`
   的条目，只取 `german` 部分按时间顺序拼接；超过 `AI_CONTEXT_MAX_CHARS`
   从最旧的开始丢，保留最近的内容。
2. 过滤后为空（刚开播/静音太久没有句对）→ 直接在弹窗里提示"最近没有足够
   内容可分析"，不发 Ollama 请求。
3. 有内容 → 提交到 `_lookup_executor`（复用现有的单线程按需查询池，
   translator_queue.py 里已经存在，专门服务"用户主动点一下"这类请求，和
   持续跑的翻译队列 `_tx_executor` 物理隔离，不会互相卡）。调用
   `config.OLLAMA_MODEL`，`/api/generate` + `stream:false`（和查词一样的
   调用方式），prompt 让它用中文总结"这段时间在讲什么背景"，3-5句话，
   `keep_alive: "2h"` 沿用现有约定。
4. 新建 `AIAnalysisPopup` 类（`popups.py`）：复用 `WordPopup` 的视觉样式
   （深色半透明圆角框），显示总结文本 + 底部一行可点的"🌐 问 Grok 更多"。

### 4. 查词弹窗升级（深度解释场景）

`WordPopup`（`popups.py`）现状：整个 `mousePressEvent` 无差别 `self.hide()`，
点哪里都关闭，布局里只有一个 `QLabel`。

Qt 里子控件（`QPushButton`）的点击事件会被子控件自己吃掉，不会冒泡触发
父级 `QWidget.mousePressEvent`，所以往布局里加按钮不会破坏"点其它地方关闭"
的现有行为，不需要重新设计整个弹窗的输入处理。

具体改动：

- 查词结果显示完成后（`_show_lookup` 之后），弹窗底部出现一个
  "🔍 深度解释" 按钮。按钮记住这次点击时已经拿到的 `context`（`_on_label_click`
  里通过 `cursor.block().text()` 取到的整句原文，本来就已经在手）。
- 点击后：同样提交到 `_lookup_executor`，prompt 是"解释这句话的背景/含义/
  俚语双关"（比单词解释的 prompt 更长、允许展开），结果替换弹窗当前内容，
  底部同样出现"🌐 问 Grok 更多"。
- 点击"深度解释"或"问 Grok 更多"这两个按钮时，要 `stop()` 并重新 `start()`
  弹窗现有的自动隐藏计时器（`_hide_timer`，当前查词结果是15秒），防止
  分析还没跑完弹窗就先被计时器关掉。

### 5. 跳转网页版

```python
import webbrowser
from urllib.parse import quote
webbrowser.open(config.AI_ANALYSIS_WEB_URL_TEMPLATE.format(query=quote(prompt_text)))
```

`prompt_text` 是拼好的自然语言问题（比如"请帮我解释这句德语的背景：「...」"），
德语原文片段本身也要做长度截断（比如最多 300 字符）后再拼进问题里，避免整个
问题过长导致 URL 超长在某些浏览器/系统上出问题。`webbrowser.open` 包一层
`try/except`，失败时用现有的 `show_status()` 提一句，不让程序崩溃或弹阻塞框。

### 6. 错误处理

和现有查词一致的模式（`_lookup_worker` 的写法）：HTTP 非 200 / 异常
→ 弹窗内容改成"分析失败: ..."文字，不重试、不弹对话框打断直播观看体验；
失败只影响这一次点击的弹窗展示，不影响识别/翻译主链路（因为走的是隔离的
`_lookup_executor`，和主翻译队列没有共享状态）。

### 7. 测试

新增 `test_ai_analysis.py`：

- 时间窗口过滤函数（纯函数：给定 `sentence_pairs` 和当前时间戳，返回
  过滤+截断后的拼接文本）——不需要真实 Qt 窗口，好测。
- prompt 拼接（背景总结 prompt / 深度解释 prompt）内容正确性。
- URL 拼接：`quote()` 编码正确、模板替换正确。
- `AIAnalysisPopup` 的显示/隐藏（参考 `test_settings_sync.py` 的模块级
  `QApplication` 持有写法，`CLAUDE.md` 第16条踩过的坑）。

`test_hittest.py` 因为 `BTN_RESERVE` 改变必须重新跑（见上文第3节）。
不引入新依赖：`webbrowser` 和 `urllib.parse` 都是标准库。

## 实施方式

沿用项目在 2026-07-13 深夜验证过的"grok-4.5 headless 实现、Claude 审 diff"
模式：本设计文档整理成规格文件交给 `grok --prompt-file ... -m grok-4.5`
headless 执行大部分改动，Claude Code 复核 diff（尤其是 `sentence_pairs`
元组形状改动的 4 个改点是否都改全、`WordPopup` 的按钮点击是否真的不会
误触发整体隐藏）+ 跑全量 pytest + `test_hittest.py`。
