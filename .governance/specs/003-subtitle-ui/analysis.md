# 字幕「历史」「失败句」与「中文译文出现体验」分析报告（Windows PyQt6 + Mac rslite）

## 核心结论
1. **多视图割裂**：Windows 历史弹窗、电视窗、影院条与主悬浮窗数据未解耦，P5 远端替换仅修改了主窗 `sentence_pairs`，辅助视图全量脱节（漏删、重复、漏更）。
2. **失败句静默蒸发**：翻译超时或被 `_trim_tx_queue` 丢弃后，系统将译文置空；若用户开启「仅显示译文」，整句在屏幕上完全隐形，辅助窗口直接丢弃，缺乏失败感知与重试通道。
3. **视觉抖动根源**：Windows 端每次更新均通过 `QTextDocument` 二分测高并全量重设 HTML，导致底部对齐整体跳动；Mac 端每次清空 `NSStackView` 导致高度伸缩闪烁。
4. **个性化断层**：Windows 具备基础配置与持久化，但缺乏文本描边与外发光；Mac rslite 样式全硬编码，无配置与持久化。

---

## A. 历史：多视图同步与历史弹窗升级

### 1. 现状分析 (file:line 证据)
- **P5 远端替换不同步**：`app.py:1280-1320` 中的 `_ui_show_line`、`_ui_drop_keys`、`_ui_update_dst` 仅就地修改 `subtitle_window.sentence_pairs` 并触发 `window._render()`。
  - `HistoryWindow` (`popups.py:83-98`) 仅有 `append_pair`，按收到顺序追加 HTML，无 key/drop 机制。P5 替换后，历史弹窗内保留了被废弃的本地粗糙句，又追加了远端精修句，同一句话出现两次。
  - `TVWindow` (`tv_window.py:72-85`) 仅在打开瞬间通过 `backfill` 读取当前 `sentence_pairs` (`subtitle_window.py:762`)；打开期间发生 P5 替换时，无法收到行删除与文本更新。
  - `CinemaBar` (`cinema_bar.py:88-103`) 仅保留最新一条，若远端句替换的是较早历史句（`_reinsert_pair_by_t0` 插入中间），影院条完全收不到事件。
- **语言切换后不同步**：`translator_queue.py:1863-1882` 在 ASR 批边界递增 `_lang_revision`，`subtitle_render.py:59-67` 仅拦截旧代数事件。主窗 `sentence_pairs` 保留旧语言，`HistoryWindow` 亦未插入语言切换标记，新旧语种内容混杂；`CinemaBar` 未调用 `clear_text()`，旧语言字幕残留至 6s 超时。
- **暂停期间**：`app.py:1322-1335` 创建 `.paused` 标志，ASR 停止采集，但主窗 live 行残句未清空，历史窗与电视窗状态无感知。
- **草稿替换瞬间**：主窗正式译文到达后清空 `live_draft` (`subtitle_render.py:92`)；`HistoryWindow` 不接收草稿；`TVWindow` (`tv_window.py:80`) 在正式句到达前频繁执行 `_remove_draft` 与 `_append_block`，引发文本跳动。

### 2. 用户感受到的问题
- 历史窗口出现两遍同一内容（前一句识别粗糙，后一句精修），时间线颠倒。
- 语言切换后历史窗没有分界线；暂停后主屏残留半句悬空。

### 3. 建议改法
- **重构数据总线**：将 `sentence_pairs` 抽象为统一的 `SubtitleHistoryStore`（行对象包含 `key, t0, t1, src, dst, source, revision, status`）。主窗、历史窗、电视窗、影院条均订阅此数据源事件（`on_insert, on_update, on_delete`）。
- **历史弹窗能力增强**：
  - 改用 `QTableView` 或 `QListWidget`，按 `t0` 绝对时间排序；
  - 增加来源标识（badge 显示 `[本机] / [节点·xxx] / [云端·Groq]`）；
  - 增加顶部检索栏（`QLineEdit` 实时过滤）与一键「复制全文 / 导出为 SRT」。

### 4. 改动量与风险
- **改动量**：M（解耦数据层并改造 3 个辅助窗口）。
- **风险**：多线程更新 UI 模型需保证在 Qt 主线程安全派发；大量条目滚动时的内存与渲染开销。

---

## B. 失败句：异常表现、缺失风险与状态机

### 1. 现状分析 (file:line 证据)
- **超时与 HTTP 错误**：`translator_queue.py:1520-1618` 经 2 次超时重试后，捕获 `ReadTimeout` 或状态码异常，返回 `sentence` 原文；在 `_translation_worker` (`:1275-1280`) 中判定 `translation == german`，将译文赋值为空串 `""`，调用 `_emit_pair(german, "", ...)`。
- **队列截断丢弃**：`translator_queue.py:889, 897-913` 当队列超 3000 字符时，`_trim_tx_queue_locked` 直接从队头 `pop(0)` 丢弃句子。这些句子**从未**进入翻译工作线程，且随着 live 行更新被彻底抹去，不上屏、不入库、不写 `transcripts/`。
- **节点 translate_failed**：`app.py:806-826` 节点报错后降级本机 Ollama 翻译；若本机仍失败，则 `dst` 为 `""`。
- **Mac rslite 表现**：`Pipeline.swift:501-512` 系统翻译异常时仅回调 `onStatus` 打印报错，**不调用** `onTranslation`，`Overlay.swift:187` 中 `dstText` 恒为 nil。

### 2. 永远无译文场景与用户视效
- **视效陷阱**：
  - 双语开启时，显示单行原文，下方留空；
  - **致命问题**：若用户勾选「只显示译文」(`SHOW_BILINGUAL=False`)，在 `subtitle_render.py:138-146` 中因 `german` 被跳过且 `chinese` 为空，`_pair_html` 返回空串，整句在屏幕上**完全隐形**；电视窗 (`tv_window.py:74`) 与影院条 (`cinema_bar.py:97`) 均因 `if not chinese: return` 静默丢弃该句。
  - 队列截断丢弃时，句子在 live 行闪现后凭空蒸发。
- **永远无译文的场景**：(1) Ollama 未启动/熔断开路；(2) 连续 2 次读超时；(3) 模型输出被 `<think>` 标签完全拦截；(4) `_trim_tx_queue` 积压截断；(5) rslite 系统语言包未下载。

### 3. 建议改法与状态机设计
- **显示策略**：失败句保留占位，显示暗淡灰色 `[未翻译 ⟳]`；「只显示译文」模式下回退显示原文或失败标记，禁止隐形。
- **重试机制**：后台对非熔断超时句自动重试 1 次（低优先级队列）；点击标记支持手动重试。
- **状态机模型**：
  ```
  [识别定稿] ──> PENDING (队列等待)
                   │
                   ▼ (worker拾取)
               TRANSLATING (翻译中/显示草稿)
                   │
        ┌──────────┴──────────┐
   (翻译成功)               (超时/错误)
        ▼                     ▼
     SUCCESS (已上屏)    FAILED_RETRYING (后台单次重试)
                              │
                    ┌─────────┴─────────┐
               (重试成功)           (仍失败/熔断/丢弃)
                    ▼                   ▼
                 SUCCESS         FAILED_TERMINAL (显示 [未翻译 ⟳])
                                        │
                                  (用户点击重试)
                                        ▼
                                     PENDING
  ```

### 4. 改动量与风险
- **改动量**：M（增加失败状态传递与 UI 重试入口）。
- **风险**：失败句过多时重试可能造成翻译队列拥堵；需严格限制重试并发。

---

## C. 中文译文出现体验：阶段分析、视觉抖动与量化优化

### 1. 出现阶段全链路剖析
- **Windows**：ASR 未稳定流式 (`unstable` 灰字) → ASR 提交 (`committed` 白字) → 残句草稿 (`live_draft` 浅蓝斜体流式更新) → 句子定稿入队 → Ollama 批处理翻译 → 正式译文上屏 (`add_pair` 清草稿并追加历史) → (可选) 节点/云端 P5 替换覆盖。
- **Mac rslite**：Speech volatile → SentenceCommitter 前缀稳定提取完整句 → `onFinal` 上屏白字 (译文为 nil) → 异步 Apple Translation → `onTranslation` 上屏黄字 → (可选) 云端段落到达 P5 覆盖本地行。

### 2. 闪烁、跳行、整块重排与旧句被顶走成因
- **整块重排与垂直跳动 (Windows)**：`subtitle_render.py:207-267` 每次 `_render()` 拼接全部可见块 HTML，通过 `QTextDocument` 二分测高计算容纳行数，再调用 `setText()`。因 QLabel 设置了底对齐，文本行数或高度每次变动，所有历史行垂直坐标都会上下颠簸。
- **草稿到定稿闪变 (Windows)**：正式句到达时，`live_draft` 被瞬间置空，文本从 live 行跳入历史区，样式从斜体蓝跳变为主文字，缺乏平滑过渡。
- **清空闪烁与高度伸缩 (Mac rslite)**：`Overlay.swift:179-195` 每次 `render()` 均清空 `stack.arrangedSubviews` 重建控件；`fitHeight()` 动态修改面板尺寸与 Y 坐标，窗口顶部不断抖动。
- **旧句顶走过快**：Windows 取决于窗口高度；Mac 仅保留末尾 2 行 (`store.lines.suffix(2)`)，长句或快语速下上一句停留不足 1.5 秒即被移出。

### 3. 量化评估方法
- **Windows**：在 `subtitle.log` 或独立 trace 中统计：
  - 定稿至草稿首字延迟 (Draft TTFT)、定稿至正式译文延迟 (Final Latency)；
  - 屏幕驻留时长：每句从 `add_pair` 到移出可见列表的时间分布（P10 / P50）；
  - 重排震荡指标：统计 10 秒内 `_render()` 触发次数及 `doc.size().height()` 变动方差。
- **Mac rslite**：在 headless 事件流中打点：
  - volatile 首次出现到 commit 耗时；commit 到 `onTranslation` 耗时；
  - 屏幕两行可见寿命（TTL）。

### 4. 具体优化建议
- **Windows (PyQt6)**：
  - **行级槽位替换**：放弃全量 QLabel 重测高，改为独立行 Widget 容器；草稿直接在预留槽位就地变更为正式译文，禁止全窗文本回流。
  - **平滑上浮动画**：新句进入时，旧句通过 `QPropertyAnimation` 向上平移（150ms 缓动），新句通过 `QGraphicsOpacityEffect` 淡入。
  - **保底显示时长**：引入 `MIN_DISPLAY_SEC = 3.0`，即便新句到达，前一句未满时长前不强行顶出，转为折叠暂存或下方平滑扩展。
  - **新句高亮**：最新句白字/黄字高亮，历史句降低透明度至 70%。
- **Mac (rslite)**：
  - **复用 View 避免清空**：停止 `removeAllArrangedSubviews()`，根据 key 进行 diff 更新。
  - **平滑变形**：使用 `NSAnimationContext.runAnimationGroup` 处理高度变动。
  - **扩充行数与驻留保护**：开放 2–4 行可调，设置最低存活时长。

### 5. 改动量与风险
- **改动量**：Windows L（涉及渲染模式重构）；Mac M。
- **风险**：动画可能引入微弱 GPU/CPU 额外开销；需确保与穿透及无边框布局兼容。

---

## D. 个性化设置方案与两端实现对照

### 1. 应开放项与推荐默认值
| 设置项 | 说明 | 推荐默认值 | 调节范围 |
| :--- | :--- | :--- | :--- |
| **字号** | 原文字号（译文等比缩放） | 26px / 28pt | 16–64px |
| **原文颜色** | 源语言文本色 | `#FFFFFF` | 拾色器 / 预设调色板 |
| **译文颜色** | 目标译文文本色 | `#FFD890` (浅黄) | 拾色器 / 预设调色板 |
| **背景颜色** | 底衬背景基础色 | `#000000` | 黑色 / 深灰 / 深蓝 |
| **背景透明度** | 窗口底衬 Alpha | 70% | 0%–100% |
| **文字边缘** | 边缘增强效果 | 描边 (Outline) | 无 / 投影 (Shadow) / 描边 |
| **窗口形态** | 布局形态 | 贴底横条 (影院式) | 方框卡片式 / 贴底横条 |
| **显示行数** | 屏幕同时保留的最大句对数 | 2 句对 | 1–5 句对 |
| **显示模式** | 语种展示过滤 | 双语对齐 | 双语 / 仅译文 / 仅原文 |

### 2. 两端现有配置与持久化对照
- **Windows (PyQt6)**：
  - **现有配置**：`config.py` 支持 `FONT_SIZE`, `CHINESE_TEXT_COLOR`, `BACKGROUND_OPACITY`, `SHOW_BILINGUAL`, `MAX_SENTENCE_PAIRS`。
  - **缺失项**：无文字边缘切换（目前仅纯色无描边）、背景色写死、无法自由切换卡片/条状形状。
  - **持久化位置**：`window_state.json` (`paths.py:repo_path("window_state.json")`)，通过 `state["tuning"]` 保存。
- **Mac (rslite)**：
  - **现状**：**完全无配置项与持久化**。字号（28）、颜色（白/黄）、阴影（NSShadow）、行数（2行）、圆角（18）、透明度（0.72）全部硬编码在 `Overlay.swift:123, 130, 186, 188, 220`。

### 3. 最小实现方案
- **Windows 端**：
  - 扩展 `settings_window.py` 增设「译文颜色选择器」「文字边缘 (无/描边/投影)」下拉框与「显示模式」单选组。
  - 在 `SubtitleWindow` 绘制层使用自定义 `QPainterPath` 绘制文字描边（解决样式表无法原生描边问题）。
  - 所有新字段全部存入 `window_state.json` 的 `tuning` 字典中自动同步。
- **Mac (rslite) 端**：
  - 新建 `UserSettings.swift` 单例，基于 `UserDefaults(suiteName: "com.rslite.config")` 持久化。
  - `OverlayController` 初始化及渲染时读取 `UserSettings` 代替静态数值。
  - 在菜单栏中增加「首选项...」菜单项，弹出轻量 SwiftUI `SettingsView` 窗口进行滑块配置。

### 4. 改动量与风险
- **改动量**：Windows M；Mac S。
- **风险**：Windows 自定义描边文字绘制需防止文字模糊或 CPU 软解卡顿（需真机确认不同 DPI 下的清晰度）。
