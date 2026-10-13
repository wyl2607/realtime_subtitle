# iOS Spike 评审修复结果记录

> 基于 `.governance/specs/009-ios-version/lanes/REVIEW-s2.md` 与 `requirement.md` 逐条核对、修复，`bash ios/Tests/build-check.sh` 输出 `BUILD-CHECK OK`。

---

## 评审报告逐项判定与修复

| 编号 | 评审项 | 判定 | 理由与位置 | 修复方式 |
|---|---|---|---|---|
| **C1** | 扩展 Info.plist 缺 `NSAppTransportSecurity` → `NSAllowsArbitraryLoads = true` | **真实** | 协调者确认必修；原 `RSBroadcast/Info.plist` 无此键，ws:// 连节点会被 ATS 拦截 | 在 `RSBroadcast/Info.plist` 根 dict 追加 `NSAppTransportSecurity` → `NSAllowsArbitraryLoads = true`（`ios/RSBroadcast/Info.plist:35-39`） |
| **C2** | `SampleHandler` 跨线程共享状态无同步 | **真实** | 评审项 1 + 协调者确认必修；`readyReceived`、`framesSent`、`finalsCount`、`translationsCount`、`preReadyDropped`、`lastStatusCode`、`fatalErrorOccurred`、`recentLines` 在 URLSession 回调线程、`processSampleBuffer`(ReplayKit 线程)、主线程（通过 keychain 通知间接）三方并发读写，存在竞态 | 新增串行 `stateQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.state")`（`SampleHandler.swift:33`），所有共享变量读写均经 `stateQueue.sync { … }`（`SampleHandler.swift:64-71, 135-144, 150-173, 176-183`）；`processSampleBuffer` 里用 `sync` 取快照，避免在锁内做重活 |
| **C3** | PiP 透明度逻辑反了：较旧字幕应 0.55，最新 1.0 | **真实** | 评审项 3 + 协调者确认必修；`ContentView.swift` 中 `displayLines = cachedLines.suffix(2)` 取最近两条，**idx 0 是较旧、idx 1 是最新**，原代码 `idx == 0 ? 1.0 : 0.55` 导致最新被调淡 | 改为 `idx == 0 ? 0.55 : 1.0`（`ContentView.swift:550`），并同步调整属性映射逻辑（`srcAttrsWithAlpha`、`dstAttrsWithAlpha` 分支互换，`553-558, 567-572`） |
| **C4** | 状态行缺 `preReadyDropped` 计数 | **真实** | 评审项 4 + 协调者确认必修；变量 `preReadyDropped` 已在 `processSampleBuffer` 累计（`SampleHandler.swift:144`），但状态字符串未包含 | 状态字符串追加 `drop=\(preReadyDropped)`（`SampleHandler.swift:137`），同时把状态写入频率从 1s 改为 5s（见下） |
| **R1** | keychain 写入频率过高（状态 1s 一次、每条事件写句对） | **真实** | 评审项 5；keychain 涉及磁盘 I/O，长期高频写入可能影响性能 | 1. 状态写入改为 **5 秒一次**（`SampleHandler.swift:122-131`，新增 `lastStatusWriteTime`）<br>2. `writeLatestLines` 增加内容去重：仅当 JSON 与上一次不同才写入（`SampleHandler.swift:260-268`，新增 `lastLinesJSON`） |
| **R2** | 致命错误状态未充分传达 | **真实** | 评审项 6；`fatalErrorOccurred` 置位后仅早退，状态行只显示 `closed=1013`，用户难以区分临时/永久故障 | 新增 `fatalErrorReason` 字段（`SampleHandler.swift:31`），在 `handleNodeEvent` 收到 fatal code 与 `handleNodeClose` 时记录 `fatal:protocol_error` / `fatal:closed=busy` 等（`SampleHandler.swift:168-171, 182-186`），状态行拼接时若 `fatalErrorOccurred` 则追加 `fatalErrorReason`（`SampleHandler.swift:142-143`） |
| **R3** | PiP 渲染定时器固定 0.5s 触发，即使内容未变也重复生成帧 | **真实** | 评审项 7；`renderTimer` 每 0.5s 无条件 `renderFrame`，造成无谓 CPU/GPU 开销 | `PiPManager` 新增 `lastRenderedLinesJSON`、`lastRenderedStatus`（`ContentView.swift:498-499`），`renderSubtitleImage` 返回 `UIImage?`，仅当句对 JSON 或状态字符串发生变化才生成新图并返回（`ContentView.swift:501-510, 586`），`renderFrame` 收到 `nil` 直接返回（`ContentView.swift:451-452`） |

---

## 记录但未修改的项

| 编号 | 评审项 | 说明 |
|---|---|---|
| **R4** | 隐私问题：字幕文本写入 keychain（评审项 2） | **架构权衡，暂不改**。免费 Personal Team 无 App Group，keychain-access-groups 是扩展↔主 App 共享数据的唯一可行通道；已设 `kSecAttrSynchronizable = false` 禁用 iCloud 同步，数据加密存储，符合「音频不落盘、文本仅本地加密共享」的隐私底线。后续付费账号到位后可迁移至 App Group + 共享内存/本地 socket。 |

---

## 验收结果

```bash
$ bash ios/Tests/build-check.sh
BUILD-CHECK OK
```

- ✅ `xcodegen generate` 生成工程无报错
- ✅ `xcodebuild -scheme RSiOS -sdk iphonesimulator` 模拟器构建成功
- ✅ 产物中 `RSBroadcast.appex/Info.plist` 含 `RPBroadcastProcessModeSampleBuffer` 与 `AppIdentifierPrefix`
- ✅ 主 App `Info.plist` 含 `AppIdentifierPrefix`
- ✅ 无 `import Shared` 跨模块引用（仍按 target 编译）
- ✅ `project.yml` 无 `type: framework`