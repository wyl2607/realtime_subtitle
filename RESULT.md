# 结果

状态：partial（代码修复与自检完成；release 构建质量门被执行环境阻塞）。

## 改动

- F2：先失效旧 key 的恢复记录，再执行 R3 的迟到旧周期判定；新 key 的双头响应即使 `sentAt == nil` 也会建立新恢复记录。
- F3：账本加载后用已加载用量恢复 `quotaHot`（>=90%）；状态查询只计算可信翻日，不消费恢复记录。仅 `applyQuotaEvent` 在同一锁定调用中消费记录并发出 `quota_reset_confirmed`。
- 在 `QuotaLedger.selfTest()` 加入 F2 与 F3 回归场景，并调整已有重启断言以验证“查询不消费、路由调用消费并确认”。

## 验证

- 红测（修改测试、未修改实现）：`SELFTEST FAIL`，分别为 F2 的“换 key 必须先于迟到旧周期判定”和 F3 的“状态查询不得在未发确认事件时消费恢复记录”。
- 绿测：`RSLITE_CONFIG_DIR=<工作树>/macos-native/.build/rslite-config.* .build/release/rslite --selftest` 输出 `SELFTEST OK`。
- `cd macos-native && swift build -c release` 未通过：SwiftPM manifest 内部沙箱报 `sandbox_apply: Operation not permitted`。
- 以 `--disable-sandbox` 重试后，源码编译与 rslite 链接产物均已生成，但 `dsymutil` 的 `GenerateDSYMFile ... rslite.dSYM` 仍报 `Operation not permitted`；这是当前执行沙箱对工具链的限制，非 Swift 编译错误。

隔离配置目录建在工作树的 `.build/` 下，验证后已清理；未启动 GUI，未访问用户配置或应用数据目录。
