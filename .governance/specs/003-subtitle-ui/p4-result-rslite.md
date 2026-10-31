# P4 出现体验：rslite 补缺口结果

- 日期：2026-10-31
- 范围：`macos-native/Sources/rslite/{Overlay,OverlayMotion}.swift`、设计令牌及生成物。
- 对应基线：`p4-baseline.md` 第 3.2 / 4 节。
- 未改 `macos-native/Package.swift`、Info.plist、打包脚本；未 commit。

## 1. 改了什么

### 1.1 瀑布流最短显示时长

- 新增纯函数 `OverlayMotion.waterfallHoldCount(shown:now:fit:reviewing:minHoldSec:)`：
  fit 之后取 `pool` 中紧挨可见段上方的那一对；若它上屏未满 `DesignTokens.Motion.minHoldSec`，
  返回 `pairCount + 1` 且 `clipsTop = true`。回看（`reviewing == true`）直接返回原 fit。
- `OverlayController.render` 的瀑布流分支改为：先按高度算 fit，再经上述纯函数决定最终条数和
  `clipsTop`；回看时取消 hold 定时器，实时状态则调用推广后的 `scheduleHoldRelease`。
- `scheduleHoldRelease` 从「固定行数 + history」改为接收 `shown` 与 `preferred` 两个参数：
  固定行数传 `style.lines`，瀑布流传 fit 的基准条数。到期后仍走 `render(animate: true)`，
  把多留的一对放走。回看不调度。

### 1.2 瀑布流最旧透明度进令牌

- `design/tokens.json` 的 `opacity` 增加 `waterfallOldest: 0.35`。
- 运行 `python3 scripts/design/gen_tokens.py`，重新生成：
  `macos-native/Sources/rslite/DesignTokens.swift`、`ios/Shared/DesignTokens.swift`、
  `realtime_subtitle/ui/design_tokens.py`。
- 删除 `OverlayMotion` 本地常量 `waterfallOldestOpacity`，`pairOpacity` 与 selftest 改用
  `DesignTokens.Opacity.waterfallOldest`。

### 1.3 译文首次到达淡入

- `OverlayController.addTranslation` 在原有「失败标记 → 译文」淡入之外，增加：
  双语模式、该句已上屏、且这个 key 第一次拿到非空译文时，把 key 放进
  `fadeTranslationIDs`，复用现有 `beginTranslationFade` / `translationFading`，只对译文字段
  做 `fadeInMs` 淡入。
- 已有译文之后的润色替换不再淡入；仅译文模式仍由「新 key 整对淡入」负责，避免淡两次；
  volatile / 草稿标签路径未改。

## 2. selftest 新用例

`OverlayMotion.selfTest` 新增 5 条：

1. 未满 `minHold`：多留一对并置 `clipsTop`。
2. 已满 `minHold`：保持原 fit 不保留。
3. 多句都未满：最多只多留一对（不是无限加）。
4. 空池：fit 不变。
5. 回看：不做最短停留。

另将最旧透明度断言改为读取生成的令牌。

## 3. 验证

```text
$ cd macos-native && swift build -c release 2>&1 | tail -3
[119 / 121] rslite-product
[120 / 121] rslite-product
Build complete! (30.34 Sek.)

$ .build/release/rslite --selftest
...
SELFTEST OK

$ python3 scripts/design/gen_tokens.py --check
（无输出，退出码 0）

$ python3 scripts/design/check_parity.py
PARITY OK

$ TMPDIR=/tmp /Users/yumei/projects/rs-mac-native/.venv/bin/python -m pytest -q tests/test_design_tokens.py
...                                                                      [100%]
3 passed
```

## 4. 风险与未验证项

- **真机观感未验**：`waterfallHoldCount` 只保证「多留一对 + 顶部渐隐」的数据选择，没在真机
  拉伸窗口观察滚动/裁切是否自然。瀑布流窗口高度固定，多留的一对位于可见段上方，实际可见
  程度取决于顶部余量；若观感上像没变化，需要真机再决定是否改为调整窗口高度或改变裁切策略。
- **回看交互**：回看时明确不做 minHold，但回看退出、恢复实时后会重新按当前 `appearedAt`
  决定是否保留，未在真机连续滚动下验证。
- **译文淡入的时序**：以 `pairViews[id]?.didFadeIn` 判定「已上屏」，与原有失败标记→译文的
  判据一致；若译文与原文在同一轮渲染内到达，仍按新 key 整对淡入，不额外淡第二次。
- 未跑 rslite 的端到端识别/翻译；未做 `swift test`（仓库 `macos-native/` 下没有测试 target）。
