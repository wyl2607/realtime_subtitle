# 结果

## 结论

代码修复完成：F4 恢复持久化 `day_epoch`，避免未来 reset 重新计入旧周期；F1 仅接受 `0...requestsPerDay` 的整数 remaining。

## 改动

- `ios/Shared/QuotaLedger.swift`：持久化/剪枝 `day_epoch`；R4 成立时在替换恢复记录前推进锚点；拒绝越界、布尔及非整数 `remaining`。
- `ios/Tests/QuotaLedgerSelftest.swift`：新增 F4 翻日后未来 reset 回归、`day_epoch` 24 小时剪枝、F1 两端与越界/非整数 remaining 回归。

建议提交信息：

```text
fix(ios): preserve quota day epoch across reset rollover

Co-Authored-By: Codex <noreply@openai.com>
```

## 验证

- `HOME="$PWD/.tmp/home" XDG_CONFIG_HOME="$PWD/.tmp/xdg-config" bash ios/Tests/run-selftest.sh`：通过，全部 `SELFTEST OK`。
- `bash ios/Tests/build-check.sh`：受控沙箱会阻止 Xcode SwiftUI 宏插件创建其子沙箱（`sandbox_apply: Operation not permitted`）。
- 对本次受控验证仅给 `xcodebuild` 注入 Swift 的 `-disable-sandbox`（未改项目配置）后运行同一 build-check：通过，输出 `BUILD-CHECK OK`；未启动模拟器 App。

## 风险

常规开发/CI 环境应再运行未注入编译器开关的 `bash ios/Tests/build-check.sh`。当前失败点是环境宏插件沙箱权限，出现在未改动的 SwiftUI 文件，不是 QuotaLedger 编译或测试失败。
