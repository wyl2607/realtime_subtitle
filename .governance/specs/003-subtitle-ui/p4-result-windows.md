# P4「出现体验」Windows / PyQt6 悬浮窗结果

- 日期：2026-10-31
- 范围：`realtime_subtitle/ui/subtitle_render.py` + `realtime_subtitle/ui/subtitle_window.py`
  的主悬浮窗（单 `QLabel` 富文本），不改电视窗 / 影院条 / 历史窗 / 命中测试。
- 对应需求：spec 003 P4；令牌仍来自 `design_tokens.MOTION_FADE_IN_MS` /
  `MOTION_MIN_HOLD_SEC` / `OPACITY_OLDER_PAIR`，没有改 `tokens.json`。

## 1. 改法摘要

### 1.1 可见句对集合「粘住」

`_render` 不再每次按「当前 live 行 + 句对高度」重新二分决定显示几条，而是保留上一次的
`self._visible_pair_keys` 作为基线：

- live 行变高、草稿变长、`update_dst` 原位换译文 / 失败句改「未翻译 ⟳」：只重排 HTML，
  **不收缩可见集合**。装不下就让 `QLabel`（`AlignBottom`）把顶部裁掉。
- 新句对到来且装不下：从最旧一句开始挤。
- 窗口尺寸 / 字号 / 显示模式 / `MAX_SENTENCE_PAIRS` 变化：按新环境重新二分，允许收缩
  （这些是用户明确改变布局的动作，不套 minHold）。
- 语言切换（`language_revision` 变化）：清掉旧 minHold 截止点，把可见句的计时锚到此刻。

每次 `_render` 结束刷新：

- `self._visible_pair_keys`：当前上屏句对 key，从旧到新；无业务 key 的句对用
  `@ts:<首次加入墙钟>` 作稳定标识（`update_dst` 替换 tuple 但保留 `item[2]`）。
- `self._shown_at[key]`：首次进入可见集合的 `_now()` 时刻；离开时删掉。
- `self._hold_deadline`：有待释放的受保护旧句时为到期 `_now()` 值，否则 `None`。

### 1.2 最短显示时长（minHold）

新句对挤到最旧一句、而那句上屏未满 `MOTION_MIN_HOLD_SEC` 时，保留它（顶部裁切可接受），
记录 `_hold_deadline` 并用**单发 `QTimer`**（`self._hold_timer`）在到期时调用
`_on_hold_release()`；回调重新渲染、此时才允许挤掉它。语言切换 / 窗口变化会停表并重置
截止点，避免旧定时器把新语言句子误放出去。`_now()` 默认 `time.monotonic()`，测试和探针
可替换成虚拟时钟。

### 1.3 新句淡入

- 只有「从未上过屏」的 key 首次进入可见集合才淡入，动画 `QVariantAnimation`，
  时长 `MOTION_FADE_IN_MS`，`self._fade_key` 标记当前淡入 key，结束后为 `None`。
- 每帧只调用 `_html_for_keys(...)` 重建 HTML + `setText`，不重跑可见集合二分。
- 颜色用 `rgba(r,g,b,alpha)` 从 0 到目标值；`_last_html` 始终保持终值版本（点词命中
  测试不解析半透明中间态）。`update_dst` / 失败句更新不会触发淡入；淡入期间又来新句时，
  旧动画直接结束到终值，再淡入新 key。
- 旧句不重淡：`self._ever_shown` 只增不减。

### 1.4 最新句高亮

沿用现有实现：除最新句外 `_pair_html(dim=True)` 用 `OPACITY_OLDER_PAIR`，最新句全不透明。
淡入终值 `alpha=1.0`，所以结束时最新句恰好全不透明、上一句恰好 dim；没有新增令牌。

## 2. 探针改造（`scripts/bench/p4_appear_probe.py`）

- 可见集合：有 `win._visible_pair_keys` 时直接读它；没有（改前基线代码）才用原二分复刻。
- 虚拟时钟：有 `win._now` 时把它换成探针时钟，执行每个事件前按 0.1s tick 推进，并在
  事件后 / tick 中检查 `_hold_deadline`，到期直接调 `win._on_hold_release()`。
- 真 `QTimer` 换成探针的空定时器，避免墙钟提前触发污染虚拟时间线。
- 淡入：记录 `_fade_key` 由 `None` 变为某 key 的次数；若该 key 之前已经上过屏则计入
  「旧 key 重淡」。每次记录后直接 `win._finish_fade()` 推到终值，不等 150ms。
- 其余指标、输出格式不变；新增「淡入」一行在摘要末尾。

## 3. 改后探针输出（本机 offscreen macOS，Windows 待真机复跑）

### 3.1 `--pairs-fit 2`（窗口 1200x313）

```text
=== P4 appear probe ===
配置：pairs-fit=2  窗口=1200x313  字号=28px  虚拟时长=39.9s
可见句对数：min=0 p50=3 max=3  分布={'0': 17, '1': 18, '2': 19, '3': 166}
每句驻留：p50=10.5s p10=9.0s min=8.2s max=12.5s  n=9（期末仍在屏 3）
驻留 < minHold(3.0s) 的句数：0
重排：可见集合变化 12/220 次事件；QLabel 文本实际变化 84 次
旧句 y 跳动：48 个事件 / 118 个旧句，最大位移 99.0px
旧句消失后重新出现：0 次（无）
UI 侧首字延迟：p50=0.055ms p90=0.842ms max=1.771ms (n=33)
按事件类型：draft: events=156 text=21 vis=0 old_y_jumps=30; live: events=36 text=36 vis=0 old_y_jumps=30; live_clear: events=12 text=11 vis=0 old_y_jumps=30; pair: events=12 text=12 vis=12 old_y_jumps=21; update_dst: events=4 text=4 vis=0 old_y_jumps=7
淡入：次数=12  淡入了已上屏过的旧 key 的次数=0
```

### 3.2 `--pairs-fit 6`（窗口 1200x605）

```text
=== P4 appear probe ===
配置：pairs-fit=6  窗口=1200x605  字号=28px  虚拟时长=39.9s
可见句对数：min=0 p50=6 max=6  分布={'0': 17, '1': 18, '2': 19, '3': 18, '4': 18, '5': 19, '6': 111}
每句驻留：p50=20.2s p10=18.9s min=18.9s max=21.7s  n=6（期末仍在屏 6）
驻留 < minHold(3.0s) 的句数：0
重排：可见集合变化 12/220 次事件；QLabel 文本实际变化 84 次
旧句 y 跳动：48 个事件 / 213 个旧句，最大位移 99.0px
旧句消失后重新出现：0 次（无）
UI 侧首字延迟：p50=0.081ms p90=1.562ms max=3.098ms (n=33)
按事件类型：draft: events=156 text=21 vis=0 old_y_jumps=51; live: events=36 text=36 vis=0 old_y_jumps=51; live_clear: events=12 text=11 vis=0 old_y_jumps=51; pair: events=12 text=12 vis=12 old_y_jumps=45; update_dst: events=4 text=4 vis=0 old_y_jumps=15
淡入：次数=12  淡入了已上屏过的旧 key 的次数=0
```

## 4. 与基线对比

| 指标 | 基线 2 句 | 改后 2 句 | 基线 6 句 | 改后 6 句 |
|---|---|---|---|---|
| 旧句消失后重新出现 | 9 次 | **0 次** | 6 次 | **0 次** |
| 旧句 y 跳动（事件 / 旧句） | 48 / 91 | 48 / 118 | 48 / 201 | 48 / 213 |
| `update_dst` 旧句 y 跳动（事件 / 旧句） | 4 / 7 | 4 / 7 | 4 / 15 | 4 / 15 |
| 每句驻留 min | 6.35s | 8.2s | 17.4s | 18.9s |
| 每句驻留 p50 | 8.65s | 10.5s | 18.7s | 20.2s |
| 驻留 < minHold(3.0s) 的句数 | 0 | 0 | 0 | 0 |
| 淡入次数 / 旧 key 重淡次数 | 0 / 0 | **12 / 0** | 0 / 0 | **12 / 0** |
| 可见集合变化 / 总事件 | 30 / 220 | 12 / 220 | 24 / 220 | 12 / 220 |
| 可见句对数 p50 / max | 2 / 3 | 3 / 3 | 5 / 6 | 6 / 6 |
| `QLabel.setText` 实际变化 | 84 | 84 | 84 | 84 |

读法：

- **「消失又回来」归零**：live/草稿变高不再挤掉历史句，这是本次最直接的变化。
- **y 跳动事件数不变**：单 `QLabel` 底对齐下，live 行/更长的译文仍会把上面的句对整体顶高；
  本任务只保证「可见集合不收缩」，没有拆分每句一个 `QLabel`，所以高度变化引起的位移仍在。
  旧句数变多是因为可见集合粘住后同时上屏的句对更多。
- **`update_dst` 的 y 跳动没有下降**：换成更长的润色译文仍会改变块高；这也是单 `QLabel`
  架构的已知代价，不在「可见集合」目标内。
- **本时间线没有触发 minHold**：句间隔都 ≥ 2.2s 且窗口能容纳的句数较多，
  最短驻留仍有 8.2s / 18.9s。minHold 的强制保留/到期释放由
  `tests/test_ui_appear.py` 用压缩间隔 + 虚拟时钟/真实短定时器覆盖。
- **淡入 12 次、没有旧 key 重淡**，与「12 句、每句首次上屏」一致。

## 5. 验证命令

```bash
ruff check realtime_subtitle tests scripts/bench

TMPDIR=/tmp /Users/yumei/projects/rs-mac-native/.venv/bin/python -m pytest -q
# 1491 passed, 33 skipped

QT_QPA_PLATFORM=offscreen TMPDIR=/tmp /Users/yumei/projects/rs-mac-native/.venv/bin/python \
  scripts/bench/p4_appear_probe.py --pairs-fit 2
QT_QPA_PLATFORM=offscreen TMPDIR=/tmp /Users/yumei/projects/rs-mac-native/.venv/bin/python \
  scripts/bench/p4_appear_probe.py --pairs-fit 6
```

## 6. 风险与未验证项

- **真机 Windows 观感未验证**：探针是 macOS offscreen、无 paint，只能证明可见集合/文本/
  计时逻辑；淡入的 150ms 观感、顶部硬裁切、Windows 字体回退后的实际高度需在 Windows
  真机复跑。
- **顶部裁切是硬切**：粘住的旧句装不下时，`AlignBottom` 直接裁掉顶部，没有渐隐遮罩；
  半行字可能露出来。若观感不可接受，后续可在顶部加渐变 mask，但不属于本任务。
- **单 `QLabel` 的 y 位移仍在**：live/更长译文仍会顶动旧句（见对比表）。要彻底消除需
  拆成每句一个 `QLabel`，与本次「不拆架构」的约束相反。
- **淡入用 `rgba()` 颜色**：在当前 Qt6 富文本上有效；若未来降级到不解析 `rgba()` 的
  渲染路径，可能退化成忽略 alpha。默认深色背景下影响很小。
- **环境变化不保护 minHold**：窗口缩小/换字号/换模式时会直接收缩，这是有意的；如果产品
  希望这些动作也保留 3 秒，需要再讨论（会进一步突破窗口容量）。
- **`_ever_shown` 只增不减**：长时间运行会累积 key 字符串（量级与上屏句数相同，可忽略）；
  好处是旧 key 永远不会重淡。
- **探针虚拟时钟不推进真实动画**：淡入次数来自 `_fade_key` 状态转变，淡入时长本身由
  `QVariantAnimation` 的单元测试/`_pump_until` 覆盖，不在探针里计时。
