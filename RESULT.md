# RESULT

## 1. 结论

done。iOS 端已按 R1–R7 补上「可信 RPD 翻日」信任门：恢复记录绑定 key 指纹与请求发出时刻，墙钟 + 单调钟双条件确认翻日，只清 24h 请求数的服务端分量，翻日成立时把 `quota_reset_confirmed` 发给路由（每条记录只发一次，`consumed` 落盘）。未改 Python、`macos-native/`、fixture、`Routing.swift`。

## 2. 改动摘要与文件清单

- `ios/Shared/QuotaLedger.swift`
  - 恢复记录：`server_reset_at` / `server_reset_observed_at` / `server_reset_key_fp` / `server_reset_consumed` 落盘，`observed_mono` 只在进程内。
  - R1：只有同一条响应同时带合法 remaining 与 reset（且 reset ≤ 24h+60s）才建立/替换记录。
  - R2：key 指纹变化丢弃旧记录、保留 `requestCorrections` 里的服务端用量下限；`key_fp=None` 不建记录。
  - R3：恢复点已过后，`sent_at=None` 或早于恢复点的迟到响应头一律不采信。
  - R4：墙钟（含回拨 >5s 丢弃）与单调钟（重启后只看墙钟）都满足才算翻日；本地午夜/单纯 24h/单个更大 remaining 都不算证据。
  - R5：只影响 24h 请求数的服务端分量，1 分钟、1 小时音频、24h 音频窗口与本地事件不动。
  - R6：`routeEvent()` / `applyQuotaEvent()` 在「此前进过冷却 + 翻日刚成立 + 未消费」时发出 `quota_reset_confirmed`，`usage_percent` 取四窗口最大值；其余保持 `quota_update`。`takeConfirmedReset()` 给冷却期轮询用。
  - R7：原子写沿用现有实现；旧文件无新字段视为无记录；字段类型不对/非有限数只丢记录，不隔离整份账本。
- `ios/Shared/CloudClient.swift`
  - 新增 `keyFingerprint`（SHA-256）与 `sentAt`，成功结果和 `httpStatus` 错误都带上。
- `ios/Engine/SubtitleEngine.swift`
  - 成功/失败回调把 key 指纹与请求发出时刻传给账本；上传前改用 `routeEvent()`；冷却期每 5 秒轮询 `takeConfirmedReset()`，仅在 `cooldownQuota` 时按等价百分比放行（不碰 429 的 retry-after）。
- `ios/Tests/QuotaLedgerSelftest.swift`：新增契约要求的 10 组用例；旧重置用例注入可控单调钟。
- `ios/Tests/CloudClientSelftest.swift`：新增 key 指纹与额度头字段自测。

Commit message：

```text
feat(ios): 可信 RPD 翻日信任门并发出 quota_reset_confirmed

- 恢复记录绑定 key 指纹与请求发出时刻，墙钟+单调钟双条件确认翻日
- 翻日只清 24h 请求数的服务端分量，冷却中恰好发一次 quota_reset_confirmed
- CloudClient 成功/失败回执携带 key 指纹与 sent_at，账本写入 consumed

Co-Authored-By: Codex <noreply@openai.com>
```

## 3. 测试命令与结果

在仓库根执行：

```bash
bash ios/Tests/run-selftest.sh
# SELFTEST OK（各入口均通过）

bash ios/Tests/build-check.sh
# BUILD-CHECK OK
```

## 4. PR 标题与描述草稿

**标题**：`feat(ios): 可信 RPD 翻日信任门并发出 quota_reset_confirmed`

**描述**：

```markdown
## 背景
issue #76 剩余部分：三端同一份的可信 RPD 翻日信任门。基线 PR #87 已解析 reset 头并支持
`quota_reset_confirmed` 事件，但还没有生产者。本 PR 只补 iOS 账本层。

## 改动
- 恢复记录：reset_at / observed_at / key_fp / consumed 落盘，observed_mono 不落盘。
- R1 两个头同响应才建记录；R2 key 绑定与下限保留；R3 迟到响应头不采信；
  R4 墙钟+单调钟双条件（含回拨处理）；R5 只重置 RPD 服务端分量；
  R6 冷却中恰好一次 `quota_reset_confirmed`（四窗口最大值）；R7 旧文件/坏字段兼容。
- CloudClient 的成功与 httpStatus 错误回执携带 key 指纹（SHA-256）与 sent_at。
- 字幕引擎在成功/失败回调传参，冷却期间轮询账本并用等价百分比恢复云端。

## 验证
- `bash ios/Tests/run-selftest.sh` → SELFTEST OK
- `bash ios/Tests/build-check.sh` → BUILD-CHECK OK

## 范围
未改 Python、macos-native、fixture、Routing.swift。
```

## 5. 风险 / 需要人决定的事项

- 允许改动文件不含 `AutoFallback.swift`，因此路由层收到的仍是 `quota_update` 的百分比形式；对 `cooldownQuota` 而言状态迁移与 `quota_reset_confirmed` 等价（同样走 `quotaUpdate`，且仅在 80% 以下放行），并且已规避清掉 429 的 retry-after。
- 单调钟沿用 `ProcessInfo.systemUptime`；若设备长时间休眠且该钟不计休眠，重启后按 R4a（无 observed_mono）判定，正常运行中会按单调钟流逝判定。
- `RESULT.md` 为本任务交付文件，未纳入代码提交。
