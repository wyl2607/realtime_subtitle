# P4「出现体验」基线（Python 悬浮窗 / rslite 现状核对）

- 日期：2026-10-31
- 范围：只做测量与差距清单，不改产品代码。
- 对应需求：`.governance/specs/003-subtitle-ui/requirement.md:19`（P4）。
- 令牌：`design/tokens.json` 的 `motion`（`fadeInMs 150` / `shiftUpMs 180` / `resizeMs 200` / `minHoldSec 3.0`）。
- 不重测端到端识别/翻译管线；这里只量 **UI 层**。

## 1. 探针方法

新增 `scripts/bench/p4_appear_probe.py`：

1. `QT_QPA_PLATFORM=offscreen` + `os.environ["REALTIME_SUBTITLE_NO_SINGLETON"]="1"`，模块级持有
   `QApplication`；建真实 `SubtitleWindow`（参考 `tests/test_ui_polish.py` 的写法）。
2. 建窗前把 `SubtitleWindow._load_state` 临时替换为空 dict，建窗后停掉 `_state_timer` /
   `_status_clear_timer` / `_save_state_if_changed`，**不读也不写真实 `window_state.json`**。
3. 窗口宽 1200，字号取令牌默认 28。高度按「典型 live 行 + `pairs-fit` 句历史」实测排版后
   定，`pairs-fit=2` 得到 1200x313，`pairs-fit=6` 得到 1200x605。
4. 内置 12 句自写德/中句子（不读 `transcripts/`、`downloads/`、`subtitle.log`），确定性事件时间线：
   - 每句先 3 次 live 词流（每 0.5s），中间每 0.15s 一次逐步变长的草稿；
   - 2.0s 后清 live 并 `_add_pair` 正式句对；
   - 第 1/4/7/10 句在 0.8s 后 `update_dst` 原位替换成更长的润色译文；
   - 句间隔按 2.2–5.0s 循环。
5. 用**虚拟时钟**直接调用槽函数（不 sleep）。事件后：
   - 用与 `_render` 相同的 `_pair_html` / `_join_render_blocks` / `_doc_for_render` /
     二分测高复算「真正可见的 key 列表」和每个句块相对窗口底边的 y；
   - 驻留 = 首次进入可见集合 → 首次被挤出（期末仍可见的句子单列为 censored，不混进
     p50/min）；
   - 旧句 y 跳动 = 同一事件前后都可见、且 y 位移 > 0.5px 的旧 key；
   - 首字延迟 = 事件槽函数同步执行到 `QLabel` 文本实际改变的墙钟耗时（离屏，无 paint）。

复现命令：

```bash
QT_QPA_PLATFORM=offscreen <venv>/bin/python \
  scripts/bench/p4_appear_probe.py --pairs-fit 2
QT_QPA_PLATFORM=offscreen <venv>/bin/python \
  scripts/bench/p4_appear_probe.py --pairs-fit 6 --json p4_6.json
```

> 口径边界：可见集合由探针按 `_render` 当前算法复刻，若以后 `_render` 的选择逻辑改了，
> 探针必须同步改；耗时只含槽函数 + `QLabel.setText`，不含 Qt paint、窗口合成、ASR/翻译。

## 2. 基线数字（本机真实输出）

### 2.1 `--pairs-fit 2`（窗口 1200x313）

```text
=== P4 appear probe ===
配置：pairs-fit=2  窗口=1200x313  字号=28px  虚拟时长=39.9s
可见句对数：min=0 p50=2 max=3  分布={'0': 17, '1': 18, '2': 163, '3': 22}
每句驻留：p50=8.65s p10=7.15s min=6.35s max=10.65s  n=9（期末仍在屏 3）
驻留 < minHold(3.0s) 的句数：0
重排：可见集合变化 30/220 次事件；QLabel 文本实际变化 84 次
旧句 y 跳动：48 个事件 / 91 个旧句，最大位移 99.0px
旧句消失后重新出现：9 次（s00, s01, s02, s03, s04, s05, s06, s07, s08）
UI 侧首字延迟：p50=0.397ms p90=1.031ms max=1.403ms (n=33)
按事件类型：draft: events=156 text=21 vis=9 old_y_jumps=21; live: events=36 text=36 vis=0 old_y_jumps=21; live_clear: events=12 text=11 vis=9 old_y_jumps=21; pair: events=12 text=12 vis=12 old_y_jumps=21; update_dst: events=4 text=4 vis=0 old_y_jumps=7
```

### 2.2 `--pairs-fit 6`（窗口 1200x605）

```text
=== P4 appear probe ===
配置：pairs-fit=6  窗口=1200x605  字号=28px  虚拟时长=39.9s
可见句对数：min=0 p50=5 max=6  分布={'0': 17, '1': 18, '2': 19, '3': 18, '4': 18, '5': 97, '6': 33}
每句驻留：p50=18.7s p10=17.4s min=17.4s max=20.2s  n=6（期末仍在屏 6）
驻留 < minHold(3.0s) 的句数：0
重排：可见集合变化 24/220 次事件；QLabel 文本实际变化 84 次
旧句 y 跳动：48 个事件 / 201 个旧句，最大位移 99.0px
旧句消失后重新出现：6 次（s00, s01, s02, s03, s04, s05）
UI 侧首字延迟：p50=0.539ms p90=1.537ms max=1.608ms (n=33)
按事件类型：draft: events=156 text=21 vis=0 old_y_jumps=51; live: events=36 text=36 vis=6 old_y_jumps=45; live_clear: events=12 text=11 vis=6 old_y_jumps=45; pair: events=12 text=12 vis=12 old_y_jumps=45; update_dst: events=4 text=4 vis=0 old_y_jumps=15
```

### 2.3 汇总

| 指标 | pairs-fit=2 | pairs-fit=6 |
|---|---|---|
| 可见句对数（min / p50 / max） | 0 / 2 / 3 | 0 / 5 / 6 |
| 驻留 p50 / p10 / min | 8.65 / 7.15 / 6.35s | 18.7 / 17.4 / 17.4s |
| 驻留 < 3.0s 的句数 | 0（n=9，另有 3 句期末仍可见） | 0（n=6，另有 6 句期末仍可见） |
| 可见集合变化 / 总事件 | 30 / 220 | 24 / 220 |
| `QLabel.setText` 实际改文本 | 84 | 84 |
| 旧句 y 跳动（事件 / 旧句数） | 48 / 91 | 48 / 201 |
| 旧句最大 y 位移 | 99px | 99px |
| 旧句消失后重新出现 | 9 次 | 6 次 |
| UI 侧首字延迟 p50 / p90 | 0.397 / 1.031ms | 0.539 / 1.537ms |
| 其中草稿事件 p50 | 0.017ms | 0.016ms |
| 其中正式句对事件 p50 | 0.718ms | 1.284ms |
| `update_dst` 事件的旧句 y 跳动 | 4 个事件 / 7 个旧句，最大 33px | 4 个事件 / 15 个旧句，最大 33px |

### 2.4 从数字里能直接读出的现状

- **UI 侧首字延迟不是瓶颈**：草稿 p50 < 0.1ms，正式句对 p50 约 0.7–1.3ms，且不含 paint。
  同一配置重复跑时总体 p50 会在 0.4–1.0ms 间抖动（离屏字体/调度噪声），量级不变；
  用户感知的 3–4s 延迟在识别/翻译管线，不在「事件到达 → 文本进 QLabel」。
- **每次 live/草稿变高都会推动历史句**：`draft`、`live`、`live_clear` 各有 11 个事件
  产生旧句 y 跳动；`live_clear` 单次最大位移 99px（整整一句对的高度）。
- **`update_dst` 确实整窗重排**：4 次原位替换里，2 句配置有 4 个事件 / 7 个旧句发生
  y 跳动，6 句配置 4 个事件 / 15 个旧句，单次最大 33px。即使只是换译文，屏幕上的旧句
  也会动。
- **旧句会「消失又回来」**：2 句配置 9 次、6 句配置 6 次。原因是可见条数由
  「live 行高度 + 句对高度」实时二分决定，live 行变高时把旧句挤出去，live 清空后又
  重新装回来——这是当前最像“闪”的一条。
- **驻留本身不短，但没有任何最短停留保证**：2–5s 的句间隔下 min 6.35s / 17.4s，
  没触发 3s；这只是「窗口装得下 + 到达间隔」的算术结果，代码里没有按时间保留的逻辑
  （见第 3 节）。如果把句间隔压到 2s 以下，2 句配置会自然跌破 3s。

## 3. 两端四项现状

### 3.1 Windows / Python（`SubtitleWindow` + `LiveTextRenderMixin`）

| P4 项 | 判定 | 依据 |
|---|---|---|
| 原位替换不整窗重排 | **未实现** | `update_dst` 改完 tuple 后直接 `self._render()`（`realtime_subtitle/ui/subtitle_render.py:119-138`）；`_render` 每次从 `sentence_pairs` 重新拼整段 HTML、二分测高、`self.window.setText(self._last_html)`（`subtitle_render.py:332-404`，setText 在 402-404）。窗口是无 QLayout 的单 `QLabel`（`realtime_subtitle/ui/subtitle_window.py:325-334`）。探针实测 `update_dst` 仍造成 7/15 个旧句 y 跳动。 |
| 新句淡入 | **未实现** | 字幕渲染路径没有任何 `QPropertyAnimation` / `QGraphicsOpacityEffect`；现有淡入只用于 hover 的拖动条/按钮工具条（`realtime_subtitle/ui/window_chrome.py:74-100`）。`MOTION_FADE_IN_MS` 只生成在 `realtime_subtitle/ui/design_tokens.py:36`，全仓库无其他引用。句对只分「最新不 dim / 旧句 dim」。 |
| 最短显示时长 | **未实现** | `MOTION_MIN_HOLD_SEC`（`design_tokens.py:39`）在 Python UI 里没有任何引用。可见条数只由高度二分决定（`subtitle_render.py:383-397`），没有 `shown_at`/定时器/到期再渲染。 |
| 最新句高亮 | **部分（等价实现）** | 只有“旧句变暗”：`_pair_html(dim=...)` 用 `OPACITY_OLDER_PAIR` 把颜色乘 0.6（`subtitle_render.py:220-235`），`_render` 对除最新外的块传 `dim=True`（`subtitle_render.py:367-377`）。最新句本身没有独立的颜色/字重/描边令牌，算不上显式“高亮”。 |

补充：`_add_pair` 在 key 已存在时也走 `update_dst`（`subtitle_render.py:162-172`），所以
“译文晚到”和“润色替换”共用同一条整窗重排路径。

### 3.2 macOS / rslite（`Overlay.swift` + `OverlayMotion.swift`）

| P4 项 | 判定 | 依据 |
|---|---|---|
| 按 key diff 更新、不清空重建 | **已实现** | `render` 按 `store.lines` 的 key 复用/更新 `pairViews`，只移除已不在历史里的 key（`macos-native/Sources/rslite/Overlay.swift:719-740`）。`arrange` 先比较 `stack.arrangedSubviews` 与目标数组，完全一致直接 return；只删除多余、插入缺失，不做 `removeAll`（`Overlay.swift:867-905`）。全仓库 Overlay 里没有 `arrangedSubviews.removeAll` 之类的清空路径。`update` 还有签名比较，文本/颜色/字号没变就不重设（`Overlay.swift:976-1008`）。 |
| 新句淡入 | **已实现（仅新 key）** | render 先把非最新 pair 标成 `didFadeIn=true`，只让最新 key 保持未 fade（`Overlay.swift:803-810`）；`applyOpacity` 只对 `!didFadeIn` 的 pair 做 `fadeInMs` 动画（`Overlay.swift:907-930`），时长取 `DesignTokens.Motion.fadeInMs`（`:909`）。额外：失败标记换成正常译文时会再淡入一次（`Overlay.swift:329-350`、`397-410`、`969-970`）。 |
| 最短显示时长 | **部分** | 固定行数模式已接到真实上屏流程：`visibleLineKeys` 在换出句未满 `minHoldSec` 时多留一行（`OverlayMotion.swift:23-41`），`render` 调用它并 `scheduleHoldRelease` 到期重渲染（`Overlay.swift:792-797`、`850-865`）。但**瀑布流模式不生效**：瀑布分支直接 `holdTask?.cancel()`，按高度 fit 选 key（`Overlay.swift:770-785`）。默认 `style.height == nil` 时走固定行数，所以默认路径有；用户拉高窗口进入瀑布流后没有。 |
| 最新句高亮 | **部分（等价实现）** | `pairOpacity(indexFromNewest:)` 最新返回 1.0，非最新返回 `Opacity.olderPair`（固定行数）或从 0.6 渐到 0.35（瀑布流）（`OverlayMotion.swift:130-145`）。0.6 来自 `design/tokens.json` 的 `opacity.olderPair`；**0.35 是 `OverlayMotion.swift:7` 的本地常量 `waterfallOldestOpacity`，没有进令牌**。没有独立的“最新句颜色/字重/描边”高亮。 |

**特别核对结论**：

- `render` **没有**“清空 stack 再全部重建”的路径；只有 `pairViews` 里过期 key 的移除
  （`Overlay.swift:727-736`）和 `arrange` 对差异项的增删（`878-891`）。
- 淡入**只作用于新 key**（以及失败标记→译文的替换），不会让所有旧句重淡。
- 最新句“高亮”目前只是相对变暗旧句，没有显式高亮元素。
- `minHold` 在固定行数模式接进了真实上屏（含到期定时重渲染），**瀑布流模式例外**。

## 4. 建议改法（本任务不实现）

### Python / Windows

1. **原位替换不整窗重排**
   - 改哪些函数：`LiveTextRenderMixin._render` / `update_dst` / `_on_label_click` /
     `SubtitleWindow.__init__` 里的 `self.window`。
   - 建议：把单 `QLabel` 上屏层拆成「每句对一个 QLabel + 一个 live QLabel」，仍无
     QLayout，由 `_render` 只计算可见 key 和每块 HTML；`update_dst` 只改命中 key 的
     label。只有新块高度变化时才移动下方块（可用 `shiftUpMs`）。点词/「未翻译 ⟳」
     改成按点击落在哪个 label 找 key，再复用该 label 的 `_build_doc`；`_last_html`
     仅作兜底。
   - 风险：**L**。命中测试、点词、重试链接、容器 resize、hover chrome、电视/历史/影院
     同步都要回归；不碰 `BTN_RESERVE` / `WM_NCHITTEST` 只能保证窗口边框行为不变，
     不能保证 label 几何改动不出问题。建议先用开关/分阶段落地。
   - 验证：改前 `update_dst: 4 事件 / 7–15 旧句跳动`；改后同一条时间线应看到
     `update_dst` 的 `old_pair_y_jumps` 降为 0（译文高度不变时），`max_old_pair_shift_px`
     不再由替换事件贡献；整体 `settext_changes` 仍应等于实际内容变化次数。

2. **新句淡入**
   - 改哪些函数：每句对 label 的创建/更新路径 + `_render`；时长只用
     `MOTION_FADE_IN_MS`（`design_tokens.py:36`）。
   - 建议：新 key 首次上屏时给该 label 挂 `QGraphicsOpacityEffect` +
     `QPropertyAnimation(opacity)`，从 0 到目标不透明度；旧 key 不动。不要对整个
     `self.window` 做淡入（会把历史一起闪）。
   - 风险：中。每 label 一个 effect 有额外合成开销；与现有 hover chrome 的 effect
     不要叠在同一 widget；动画期间命中测试/鼠标穿透不能变。
   - 验证：探针需要补一个「块级 alpha / effect 存在性」采样（当前只量布局）；改后
     应只有新 key 出现动画、旧 key alpha 不变。动画异步，采样要走 `_pump_until` 式
     条件等待，不能固定 sleep。

3. **最短显示时长**
   - 改哪些函数：`_render` 的可见 key 选择（当前 `subtitle_render.py:383-397`）+
     `_update_draft` / `_add_pair` / `_on_container_resize` 触发路径；新增每 key
     `shown_at`。
   - 建议：选可见集合时，若“即将被挤出的最旧句”上屏未满 `MOTION_MIN_HOLD_SEC`，
     多保留它一行；用单发 `QTimer` 在到期时重渲染（参考 rslite 的
     `scheduleHoldRelease`）。多出的一行若装不下，需要明确策略：允许顶部裁切/渐隐
     （推荐，接近 rslite 瀑布流）或放弃该句的 minHold。
   - 风险：中–高。多一行会突破“窗口只够 N 句”的视觉预期；顶部裁切要做好，否则
     出现半行字；定时器要处理快速连续句对、语言切换、窗口缩放；不能让新句因此
     被藏起来。
   - 验证：探针要加“按虚拟时间 tick（如每 0.1s）调用一次渲染/到期检查”的驱动，
     否则纯事件驱动看不到定时释放；改后 `under_min_hold` 应为 0（或在窗口装不下
     N+1 的配置里明确记录例外），并观察 `visible_pair_count.max` 是否临时变成 N+1。

4. **最新句高亮**
   - 改哪些函数：`_pair_html` / `_render`；颜色/透明度只从令牌来。
   - 建议：先确认产品要的是“旧句变暗”（已有）还是“最新句额外高亮”。若要显式高亮，
     在 `design/tokens.json` 增加（例如 `opacity.newestPair` 或
     `color.subtitleNewest`），跑 `scripts/design/gen_tokens.py` 生成
     `realtime_subtitle/ui/design_tokens.py`，再在 `_pair_html` 里按 `is_newest`
     使用；不要写死颜色/时长。
   - 风险：低。别覆盖未翻译灰色和 `retry:` 链接颜色。
   - 验证：探针增加“每个可见块的实际前景色/不透明度”采样，断言最新块与旧块不同，
     且旧块仍等于 `OPACITY_OLDER_PAIR`。

### macOS / rslite

1. **按 key diff 更新**：现状已满足，无需大改。可选优化是 `render` 在可见 key 与
   文本签名都没变时提前返回，避免每次重放历史；`arrange` 已有相等短路
   （`Overlay.swift:869-872`）。风险低。验证：在 Swift 侧补一个“render 前后
   `stack.arrangedSubviews` 身份不变”的自检（现有 `--selftest` 只覆盖
   `OverlayMotion` 纯逻辑）。
2. **新句淡入**：现状已满足；唯一缺口是“同一条已上屏句子的普通译文更新”不淡入
   （只有失败标记→正常译文会淡入，`Overlay.swift:329-334`）。若产品希望译文替换
   也有反馈，可在 `addTranslation` 里对 `alreadyShown` 且首次拿到译文的 key 插入
   `fadeTranslationIDs`，仍复用 `beginTranslationFade`。风险低；注意别让流式草稿
   每次都闪。
3. **最短显示时长**：把固定行数模式的 hold 策略补到瀑布流分支（`Overlay.swift:770-785`
   当前取消 `holdTask`）。建议在 waterfall fit 之后再判断“被裁掉的最旧句是否未满
   `minHoldSec`”，未满则多保留一对并用现有 `clipsTop` 顶部渐隐；或者明确在 UI 文案/
   规格里声明瀑布流不受 minHold 约束。风险中：会改变瀑布流可见条数和滚动/回看交互。
   验证：给 `OverlayMotion.selfTest` 增加“waterfall + 未满 minHold”用例，并真机看
   顶部裁切是否自然。
4. **最新句高亮**：现状是相对透明度；若产品要显式高亮，同样先加
   `design/tokens.json` 令牌、跑 `scripts/design/gen_tokens.py` 生成 Swift 常量，再在
   `applyOpacity`/`SubtitlePairView` 使用。顺带把 `OverlayMotion.swift:7` 的
   `waterfallOldestOpacity = 0.35` 迁进令牌，避免“改瀑布流透明度却绕开令牌”。
   风险低。

## 5. 风险与注意事项

- **探针复刻了 `_render` 的选择算法**。以后改 `_render` 的二分/可见条数逻辑，必须
  同步改 `_visible_pair_items` / `_layout_snapshot`，否则数字会悄悄失真。
- 探针是**离屏 + 无 paint**：首字延迟只代表“事件 → setText”这一段；真实上屏还要加
  Qt 布局/合成/显示刷新。绝对值不要和端到端 3–4s 混在一起解读。
- 虚拟时钟不推进真实 `QTimer`/动画。当前 Python 没有字幕动画/minHold，所以没影响；
  一旦按建议加上动画或 minHold 定时器，探针必须改成“推进事件循环 + 按虚拟时间 tick”
  的驱动，否则测不到异步效果。
- 本机是 macOS offscreen，字体回退与 Windows 的 Microsoft YaHei 不同，**像素高度和
  可见条数只作相对基线**；最终 Windows 数字应在 Windows 上重跑同一脚本。
- 探针不写 `window_state.json`，不读用户字幕存档；12 句全部是脚本内自写文本。
- rslite 的判断来自源码阅读，没有跑 GUI；`minHold` 的“固定行数有、瀑布流无”是当前
  代码最值得协调者确认的差异。
