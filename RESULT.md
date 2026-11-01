# 修复结果

## 结论
**done** - 所有三个问题（G2、G3、G4）已修复，自测与构建校验全部通过。

## 改动摘要

### G2（高）dayEpoch 在未来 - 墙钟回拨时的 fail-closed 行为
**文件**：`ios/Shared/QuotaLedger.swift`
- `usageLocked`：当 `dayEpoch > now`（墙钟回拨）时，不使用该锚点，改用 `-infinity`（fail-closed），旧周期事件重新计入；墙钟追上后自动恢复正常。
- `pruned`：`dayEpoch` 在未来时保留不剪枝，等墙钟追上后再正常过期。

### G3（中）重启漏发 - 冷却中、记录未消费、reset 之后才重启
**文件**：`ios/Shared/QuotaLedger.swift`
- `init`：在推进 `dayEpoch` 之前，先用旧周期完整用量（未应用 dayEpoch 过滤）判断 `quotaHot`；若 ≥90% 则置 `quotaHot=true`，确保重启后仍能发出 `quota_reset_confirmed` 事件。

### G4（中）SubtitleEngine 丢弃事件类型、过早消费恢复记录
**文件**：
- `ios/Shared/AutoFallback.swift`：新增 `applyRouteEvent(_:)` 方法，原样应用 `RouteEvent`（保留 type，如 `quota_reset_confirmed`）。
- `ios/Engine/SubtitleEngine.swift`：
  - `uploadCloudSegment`：改用 `applyRouteEvent(ledger.routeEvent())`，不再丢弃 type 统一调用 `applyQuotaUsage`。
  - `pollLedgerQuotaReset`：先在 `stateQueue` 内确认当前是 `cooldownQuota`，再调用 `ledger.takeConfirmedReset()` 消费记录并投递 `quota_reset_confirmed`；非 `cooldownQuota` 时不消费记录。

## 文件清单
1. `ios/Shared/QuotaLedger.swift`
2. `ios/Shared/AutoFallback.swift`
3. `ios/Engine/SubtitleEngine.swift`
4. `ios/Tests/QuotaLedgerSelftest.swift`（新增 G2、G3 回归测试）
5. `ios/Tests/AutoFallbackSelftest.swift`（新增 G4 回归测试）

## 测试命令与结果
```bash
bash ios/Tests/run-selftest.sh
# 全部 SELFTEST OK
bash ios/Tests/build-check.sh
# BUILD-CHECK OK
```

## Commit Messages
```
fix(quota): G2 dayEpoch future rollback fail-closed (ios/Shared/QuotaLedger.swift)
fix(quota): G3 restart missed quota_reset_confirmed by judging quotaHot before dayEpoch advance (ios/Shared/QuotaLedger.swift)
fix(routing): G4 preserve RouteEvent type & consume reset record only in cooldownQuota (ios/Shared/AutoFallback.swift, ios/Engine/SubtitleEngine.swift)
test: add regression tests for G2/G3/G4 (ios/Tests/QuotaLedgerSelftest.swift, ios/Tests/AutoFallbackSelftest.swift)

Co-Authored-By: Codex <noreply@openai.com>
```

## PR 标题 + 描述草稿
### 标题
`fix(ios): G2/G3/G4 quota ledger & auto-fallback fixes for trusted RPD rollover`

### 描述
修复 iOS 端额度账本与自动回退的三个契约一致性问题（R1–R7）：

1. **G2（高）dayEpoch 在未来**：重启时墙钟前跳到 reset 之后，随后回拨。修复 `usageLocked` 与 `pruned` 在 `dayEpoch > now` 时不使用该锚点（按 `-inf` fail-closed），墙钟追上后自动恢复。
2. **G3（中）重启漏发**：冷却中、记录未消费、reset 之后才重启。`init` 里先用旧周期完整用量判断 `quotaHot`（≥90% 置 true），再推进 `dayEpoch`，确保翻日事件能发出。
3. **G4（中）事件类型丢失与过早消费**：`SubtitleEngine` 丢弃 `routeEvent` 的 type 统一走 `applyQuotaUsage`，且在检查 `cooldownQuota` 前就消费恢复记录。新增 `AutoFallback.applyRouteEvent` 原样传递 type，并在 `stateQueue` 内确认 `cooldownQuota` 后再消费记录。

新增对应回归测试（修前红、修后绿）。质量门：`run-selftest.sh` 全绿、`build-check.sh` 通过。

## 风险 / 需要人决定的事项
- 无需人工决策，所有改动已通过自测与构建校验。
- G2 的 fail-closed 行为在墙钟回拨期间会暂时把旧周期事件重新计入 RPD，可能导致短暂显示更高用量；这是契约要求的安全侧行为，墙钟追上后自动纠正。
- G3 的 `quotaHot` 判断依赖旧周期用量（未应用 dayEpoch），若旧周期仅靠分钟窗口达 90% 而日窗口较低，重启后仍会置 `quotaHot=true`（与 rslite 行为对齐）。