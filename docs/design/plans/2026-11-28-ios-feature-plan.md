# 实时字幕 iOS 功能规划（2026-11-28）

## 1. 产品定位（2–4 句：给谁用、解决什么）

“实时字幕”面向观看德语视频、直播或其他 App 内容的 iPhone 用户，提供德语语音识别与中文翻译字幕。音源支持屏幕广播抓取 App 声音，也支持麦克风采集；识别可按云端、电脑节点、本机自动切换。当前产品仍是实验性 iOS 原型，核心代码较完整，但尚未完成 iPhone 真机验收和正式分发准备。证据：`ios/RSiOS/Info.plist`、`ios/README.md`、`README.md`。

## 2. 功能清单与现状

| 功能 | 代码实现（✅/🟡/❌） | 自动化验证（单测/自测/约束/CI） | 真机验证 | 证据（文件路径） |
|---|---|---|---|---|
| 屏幕广播抓取 App 声音 | ✅ | 有 ReplayKit 结构校验、致命错误静态检查、模拟器构建与扩展 plist 校验；未发现真实 ReplayKit 行为测试 | ❌ 未找到完整验收记录 | `ios/RSBroadcast/SampleHandler.swift`、`ios/RSBroadcast/Info.plist`、`tests/test_ios_fatal_error.py`、`ios/Tests/build-check.sh` |
| 麦克风采集 | ✅ | 有 `CaptureArbiter`、音频会话、前后台与并发约束自测；未发现真实音频输入集成测试 | ❌ 未找到完整验收记录 | `ios/RSiOS/MicCapture.swift`、`ios/Shared/CaptureArbiter.swift`、`ios/Tests/CaptureArbiterSelftest.swift`、`tests/test_ios_redesign_constraints.py` |
| 云端 → 节点 → 本机三级路由与自动回退 | ✅ | 路由、回退、冷却、额度、节点重连均有 Swift 自测；CI 配置会运行 iOS 自测 | ❌ 未找到真机长时运行、断网切换验收 | `ios/Shared/Routing.swift`、`ios/Shared/AutoFallback.swift`、`ios/Engine/SubtitleEngine.swift`、`ios/Tests/AutoFallbackSelftest.swift`、`ios/Tests/NodeSelftest.swift` |
| 实时字幕显示、草稿、样式设置与无障碍 | ✅ | 有状态词、样式、动态字体、无障碍静态约束；未发现 XCTest/UI 测试 | ❌ 未找到完整真机 UI 验收 | `ios/RSiOS/LiveCaptionsView.swift`、`ios/RSiOS/Pages/LivePage.swift`、`ios/RSiOS/Pages/SettingsPage.swift`、`ios/Shared/SubtitleStyle.swift`、`tests/test_ios_redesign_constraints.py` |
| 字幕存档、按日回看、搜索、复制/导出、失败重译 | ✅ | 有存档、异步写盘、重译策略自测与静态约束 | ❌ 未找到真机存档和导出验收 | `ios/RSiOS/TranscriptStore.swift`、`ios/RSiOS/TranscriptView.swift`、`ios/Tests/TranscriptStoreSelftest.swift`、`ios/Tests/RetryTranslationSelftest.swift`、`tests/test_ios_transcript_store_async.py` |
| 画中画、后台音频、锁屏实时活动 | ✅ | 有 PiP/音频会话约束、App 状态自测、plist/扩展构建校验；未发现系统 PiP 行为测试 | ❌ 明确列为待验 | `ios/RSiOS/PiP/PiPManager.swift`、`ios/RSiOS/App/LiveActivityController.swift`、`ios/RSLiveActivity/SubtitleLiveActivity.swift`、`ios/RSiOS/Info.plist`、`docs/TODO.md` |
| 电脑节点配对与节点会话 | 🟡 | `rsnode://pair` 解析、Tailnet 校验、节点协议和重连有自测；未找到二维码扫描实现 | ❌ 未找到真机配对验收 | `ios/Shared/PairingLink.swift`、`ios/Shared/NodeProtocol.swift`、`ios/RSiOS/Pages/SettingsPage.swift`、`ios/Tests/PairingSelftest.swift`、`README.md` |
| 云端 Groq、额度、蜂窝重发与网络诊断 | ✅ | CloudClient、分段、额度、HTTP chunked、网络探针均有自测；无真实云端端到端证据 | ❌ 未找到带真实 key/网络条件的真机验收 | `ios/Shared/CloudClient.swift`、`ios/Shared/QuotaLedger.swift`、`ios/Shared/CellularHTTP.swift`、`ios/RSiOS/NetworkProbe.swift`、`ios/Tests/CloudClientSelftest.swift` |
| Siri/快捷指令/控制中心开始与停止麦克风字幕 | ✅ | 有策略自测、源码约束、CI 静态检查；广播无法被程序化启动，代码明确不提供广播 Intent | ❌ 未找到真机 Siri/控制中心验收 | `ios/RSiOS/Intents/StartMicCaptionsIntent.swift`、`ios/RSiOS/Intents/StopCaptionsIntent.swift`、`ios/RSLiveActivity/SubtitleControlWidget.swift`、`ios/Tests/MicCaptionIntentPolicySelftest.swift` |
| 诊断页 | ✅ | 有网络探针取消闸门自测与工程结构校验 | ❌ 未找到真机诊断流程验收 | `ios/RSiOS/Pages/DiagnosticsPage.swift`、`ios/Shared/NetworkProbeGate.swift`、`ios/Tests/NetworkProbeSelftest.swift` |

补充判断：

- 当前 Xcode 工程没有 XCTest/UI Test target：`ios/project.yml` 中三个 target 的 `testTargets` 均为空。
- 当前 CI 覆盖的是 Swift 自测、模拟器构建、扩展 plist、跨端夹具和设计令牌一致性，不是真机验证：`.github/workflows/ci.yml`。
- “扫码配对”在产品说明中出现，但 iOS 页面证据是粘贴 `rsnode://pair` 链接；未找到相机/二维码扫描代码：`README.md`、`ios/RSiOS/Pages/SettingsPage.swift`。

> 注（Claude 审核补充）：「扫码配对」目前走系统相机扫二维码 → `rsnode://pair` 深链接拉起 App 并预填配对框（`ios/Shared/PairingLink.swift`），App 内没有内置扫码器，因此按部分完成计。

## 3. 完成度

口径：

- 按上述 10 个功能域等权计算。
- 代码实现：有完整对应实现记 1；只有部分实现记 0.5。
- 自动化验证：有相关自测/静态约束/CI 入口记 1；只有局部或结构验证记 0.5。
- 真机验证：必须有仓库内明确的 iPhone 安装、运行和验收证据才记 1。
- 综合权重：代码实现 40%、自动化验证 35%、真机验证 25%。真机为 0 时，综合不能视为完成。

评估结果：

| 指标 | 估算完成度 | 口径说明 |
|---|---:|---|
| 代码实现 | 95% | 核心功能均有实现；节点配对的“扫码”未找到实现，因此按部分完成 |
| 自动化验证 | 82% | 共享逻辑覆盖较好，但系统 API、UI、ReplayKit、PiP、真机网络链路缺少集成验证 |
| 真机验证 | 0% | `README.md` 明确写“尚未上真机”，`docs/TODO.md` 将 iPhone 安装、PiP、广播扩展列为未完成 |
| 综合 | 67% | `95%×40% + 82%×35% + 0%×25% ≈ 67%` |

因此，当前更准确的定位是：核心代码接近可演示，但产品尚未达到可交付或可发布状态。

## 4. 缺口与风险（按影响排序）

1. **没有完整 iPhone 真机验收。**  
   这是最大风险，尤其影响 ReplayKit 广播扩展、麦克风权限、锁屏后台、PiP、实时活动和蓝牙/耳机路由。证据：`README.md`、`docs/TODO.md`。

2. **签名与发布链路尚未落地。**  
   `DEVELOPMENT_TEAM` 为空；真机构建需要 Team ID，归档脚本要求外部传入 Team ID 和已登录的 App Store Connect 账号。证据：`ios/project.yml`、`ios/README.md`、`ios/scripts/archive.sh`。

3. **缺少 XCTest/UI 自动化测试。**  
   目前主要是 `swiftc` 自测和源码扫描，不能证明 SwiftUI 页面、系统权限弹窗、ReplayKit、PiP、Live Activity 在设备上的交互正确。证据：`ios/project.yml`、`ios/Tests/run-selftest.sh`、`.github/workflows/ci.yml`。

4. **“扫码配对”与实际页面能力不一致。**  
   当前可见证据是粘贴配对链接，未找到二维码扫描入口。证据：`README.md`、`ios/RSiOS/Pages/SettingsPage.swift`、`ios/Shared/PairingLink.swift`。

5. **iOS 版本范围非常窄。**  
   工程部署目标为 iOS 26，Swift 6；本机识别和翻译依赖 iOS 26。需要负责人确认这是刻意的产品门槛，而不是临时限制。证据：`ios/project.yml`、`ios/README.md`、`tests/test_ios_redesign_constraints.py`。

6. **云端隐私、网络和成本需要产品决策。**  
   开启 Groq 后音频片段会上传；云端开关默认关闭，但代码同时允许任意 ATS 加载，未找到更细的生产网络策略说明。证据：`ios/RSiOS/Pages/SettingsPage.swift`、`ios/RSiOS/Info.plist`、`ios/README.md`。

7. **桌面端能力不能直接照搬到 iOS。**  
   README 明确 iOS 目前只能用画中画显示，不能像桌面端一样使用悬浮窗。证据：`README.md`、`ios/RSiOS/PiP/PiPManager.swift`。

## 5. 下一步路线

### P0：完成 iPhone 真机基线验收

- 目标：证明核心链路可安装、可采集、可出字幕、可持续运行。
- 涉及文件：`ios/project.yml`、`ios/RSiOS/Info.plist`、`ios/RSiOS/RSiOS.entitlements`、`ios/RSBroadcast/Info.plist`、`ios/RSBroadcast/RSBroadcast.entitlements`、`ios/RSiOS/MicCapture.swift`、`ios/RSBroadcast/SampleHandler.swift`、`ios/RSiOS/PiP/PiPManager.swift`、`ios/RSLiveActivity/SubtitleLiveActivity.swift`。
- 验收标准：
  - 可通过签名构建并安装到 iPhone。
  - 麦克风字幕连续运行至少一段固定时长，锁屏后仍能采集、存档并显示实时活动。
  - 屏幕广播能抓取目标 App 声音并显示字幕。
  - PiP 可启动、切后台、停止。
  - 广播与麦克风互斥行为符合预期。
- 是否可并行：可以分为“采集/路由”和“显示/后台”两条 lane，但最终必须在同一台真机整体验收；会重叠 `project.yml`、Info.plist 和权限配置。
- 适合 lane：iOS 真机验证 lane，**需 Mac + Xcode**。

### P0：完成签名、App Group 与归档链路

- 目标：建立可重复的 Debug 真机包和 Release/TestFlight 包。
- 涉及文件：`ios/project.yml`、`ios/RSiOS/RSiOS.entitlements`、`ios/RSBroadcast/RSBroadcast.entitlements`、`ios/scripts/archive.sh`、`ios/README.md`。
- 验收标准：
  - `xcodebuild archive` 成功。
  - 主 App、广播扩展、实时活动扩展的 bundle/signing 配置一致。
  - App Group 和共享 Keychain 在真机运行时可读写。
  - 能导出可安装的 ipa；如决定上架，能上传到 TestFlight。
- 是否可并行：与功能验收部分并行，但会和真机构建共享签名配置。
- 适合 lane：发布工程 lane，**需 Mac + Xcode**，需要 Apple Developer/App Store Connect 权限。

### P1：建立 XCTest/UI Test 验收层

- 目标：把当前源码扫描和纯逻辑自测补成可重复的 App 行为验证。
- 涉及文件：`ios/project.yml`、新增 iOS XCTest/UI Test target、`ios/RSiOS/Pages/*.swift`、`ios/RSiOS/Intents/*.swift`。
- 验收标准：
  - 至少覆盖四个 Tab、开始/停止麦克风、设置保存、回看搜索/导出入口、错误状态展示。
  - CI 至少运行模拟器 UI 测试。
  - 系统权限、ReplayKit、PiP 等无法模拟的部分明确保留真机验收清单。
- 是否可并行：测试工程和真机验收可并行；新增测试 target 会修改 `project.yml`，与发布 lane 有配置重叠。
- 适合 lane：iOS 测试工程 lane，**需 Mac + Xcode**。

### P1：补齐配对入口与产品文案一致性

- 目标：决定并实现“扫码配对”，或把产品说明统一改为“粘贴配对链接”。
- 涉及文件：`ios/RSiOS/Pages/SettingsPage.swift`、`ios/Shared/PairingLink.swift`、`ios/RSiOS/Info.plist`、`README.md`、`ios/README.md`。
- 验收标准：
  - 若保留扫码：真机相机扫描后可填充并应用 `rsnode://pair`。
  - 若不做扫码：所有 README 和页面文案都不再声称支持扫码。
  - 配对 token 不出现在普通日志和诊断界面。
- 是否可并行：可独立于真机媒体链路；会与文档 lane 重叠。
- 适合 lane：iOS 功能 lane + 文档 lane。

### P2：补充性能、稳定性与发布前风险验证

- 目标：量化长时间运行、网络切换、节点断线、资源准备、功耗和存档写入表现。
- 涉及文件：`ios/Engine/SubtitleEngine.swift`、`ios/Engine/LocalEngine.swift`、`ios/RSiOS/TranscriptStore.swift`、`ios/Shared/AutoFallback.swift`、`ios/RSiOS/DiagnosticsPage.swift`、`docs/TODO.md`。
- 验收标准：
  - 固定音频样本下记录首句延迟、译文延迟、断线恢复时间和丢句数。
  - Wi-Fi/蜂窝切换、节点离线、云端 429/401/403 均有可复现记录。
  - 连续运行测试后无明显存档丢失、重复或异常增长。
- 是否可并行：可与 UI 测试并行，但需要共用真实设备和测试节点。
- 适合 lane：性能/稳定性 lane，**需 Mac + Xcode + iPhone**。

### P2：决定产品分发形态

- 目标：明确仅内部 TestFlight、公开 App Store，还是继续源码原型。
- 涉及文件：`ios/scripts/archive.sh`、`ios/README.md`、`ios/project.yml`、App Store Connect 配置。
- 验收标准：
  - 明确支持设备、最低系统、隐私说明、云端数据政策和版本号策略。
  - 若公开发布，完成签名、TestFlight、隐私问卷和审核材料。
- 是否可并行：可以与代码稳定性工作并行，但最终依赖 P0 真机和签名结果。
- 适合 lane：发布/产品 lane，**需 Mac + Xcode**。

## 6. 需要负责人决定或在场的事

- 是否提供 iPhone 真机和 iOS 26 测试设备；至少需要负责人在场完成权限、锁屏、广播和 PiP 操作。
- 是否申请或使用 Apple Developer Program、App Store Connect 和 TestFlight。
- 使用哪个 Team ID、Bundle ID、App Group，以及是否允许自动签名。
- 是否保留 iOS 26 作为最低版本。
- 是否真的需要“扫码配对”；若需要，是否接受增加相机权限和二维码识别实现。
- 是否允许用户主动开启 Groq 云端识别；需要确定音频上传告知、额度、失败时的回退和隐私文案。
- 是否有可用的电脑节点/Tailscale 环境用于节点配对、断线和网络切换验收。
- iOS 产品是否接受“只能 PiP/锁屏显示、没有桌面式悬浮窗”的交互限制。
- 是否把当前目标定义为内部原型、内部 TestFlight，还是正式 App Store 产品。