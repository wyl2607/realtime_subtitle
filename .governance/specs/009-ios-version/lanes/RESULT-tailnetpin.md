# RESULT-tailnetpin

## 结论

已在 iOS Broadcast Extension 连接节点前加入 tailnet IPv4 校验与 IP 字面量钉住：

- 新增 `ios/Shared/TailnetPin.swift`，移植 macOS `TailnetPin` 的 `isTailnetIPv4`、`pinned(_:resolve:)`、`isIPv4Literal`、`resolveIPv4`，未带 `selfTest()`。
- `ios/RSBroadcast/SampleHandler.swift` 在后台 `connectionQueue` 中执行阻塞解析；只有 `TailnetPin.pinned(config.sessionURL)` 成功后才创建并启动 `NodeSession`。
- 解析失败或非 tailnet 地址时不连接，写状态 `last=resolve_failed` 或 `last=not_tailnet`，音频采样路径继续只计算电平。
- 新增 `ios/Tests/TailnetPinSelftest.swift`，并在 `ios/Tests/NodeSelftest.swift` 入口末尾追加一次调用。
- `ios/Tests/run-selftest.sh` 已加入 `ios/Shared/TailnetPin.swift` 与 `ios/Tests/TailnetPinSelftest.swift` 编译；同时把 Swift module cache 放进 `.tmp/module-cache`，避免本地受限执行环境写 `~/.cache/clang` 失败。

未修改 `macos-native/`。

## 自测覆盖

`runTailnetPinSelftest()` 覆盖：

- `100.64.0.0`、`100.127.255.255` 属于 tailnet。
- `100.63.255.255`、`100.128.0.0`、`192.168.1.1`、非 IPv4 主机名不属于 tailnet。
- 注入 resolve：全部 tailnet 时钉成第一个 IP，并保留端口、路径、query、fragment。
- 注入 resolve：混有 `192.168.1.1` 时拒绝。
- 注入 resolve：空结果时拒绝。
- IPv4 字面量只校验不解析；tailnet 字面量通过，非 tailnet 字面量拒绝。

## 验证记录

- 初始 gate：`bash ios/Tests/run-selftest.sh` 在 `TailnetPin` 未实现时失败，错误为 `cannot find 'TailnetPin' in scope`。
- 实现后：`bash ios/Tests/run-selftest.sh` 通过，输出 `SELFTEST OK`。
- 扩展编译：`cd ios && xcodebuild -project RSiOS.xcodeproj -scheme RSBroadcast -sdk iphonesimulator -configuration Debug -derivedDataPath build CODE_SIGNING_ALLOWED=NO build` 通过，输出 `** BUILD SUCCEEDED **`。

## build-check 状态

按要求运行 `bash ios/Tests/build-check.sh`，当前执行环境未能得到 `BUILD-CHECK OK`。脚本退出码为 1 且无 stdout；拆步后定位到完整 `RSiOS` app target 编译阶段的既有 SwiftUI macro/plugin 环境失败：

- `sandbox-exec: sandbox_apply: Operation not permitted`
- `external macro implementation type 'SwiftUIMacros.StateMacro' could not be found for macro 'State()'`
- `swift-plugin-server produced malformed response`

同一次拆步中 `RSBroadcast` scheme 可成功构建，说明本次新增的 `TailnetPin` 与 extension 连接路径编译通过。
