# Spec 009 — iOS 版实时字幕

状态：requirement（2026-10-12 起草，待用户确认）；spike 进行中（分支 `spike/ios-broadcast`，worktree `~/lanes/wt/rs-ios-spike`）。

## Goal
在 iPhone 上看任意 App（Safari / YouTube / 直播 App）里的德语视频时，能看到德语原文 + 中文译文双语字幕；与 Mac rslite / 安卓重写版同一套结构与风格。

## 动机
- 三端（Win / Mac / 安卓）已有或在做，iOS 是最后一块；spec 008 写明「iOS 以后做，沿用同一套结构」。
- 用户日常在手机上看德语内容，Mac/Windows 节点（mini2、Windows、mac-mini）已能提供识别+翻译算力。

## 背景（已知事实，均有出处）
- 节点协议 v2（`docs/protocol-v2.md`）：`WS /v2/session`，hello→16kHz s16le 二进制帧→`final`/`translation`/`status`；远程只监听 Tailscale IPv4 `100.64.0.0/10:8791`，Bearer token。iPhone 要连节点必须装 Tailscale 并在同一 tailnet。
- rslite（`macos-native/`）已有可移植 Swift：`NodeClient` / `NodeRouter` / `TailnetPin` / `SentenceCommitter` / `DesignTokens`，以及 SpeechAnalyzer + Translation 本机链路（实测 WER 7.38%、chrF 33.8、单句 0.12s）。
- 本机开发环境：Xcode 27.0、iOS 27 SDK；签名身份只有免费 Personal Team（Team ID 不入库，构建时由命令行传入）；付费开发者账号申请中。已配对一台 iPhone 16 测试机。
- HealthBridge 实测（2026-10-06）：免费 Personal Team 的 profile 不含 `application-groups` → 用 App Group 直接 BUILD FAILED；`keychain-access-groups` 可签过。

## 方案方向（待 spike 验证，不是事实）
| 环节 | 方向 | 备选 |
|---|---|---|
| 抓声音 | ReplayKit 广播上传扩展（Broadcast Upload Extension）拿 `.audioApp` 样本 | 麦克风模式（外放/会议场景） |
| 识别+翻译 | A：扩展把音频直发节点（协议 v2）；B：iOS 26+ 本机 SpeechAnalyzer + Translation（在主 App 里跑） | 混合：节点优先、不可用回本机（对齐 rslite 路由语义） |
| 扩展↔主 App 传数据 | 扩展直连节点、主 App 另开连接取文字？（协议目前无「旁听」能力）/ keychain 共享配置 + 本机 socket / Darwin 通知 + 共享文件 | 付费账号到位后用 App Group |
| 显示 | 画中画（`AVPictureInPictureController` + `AVSampleBufferDisplayLayer` 渲染字幕图像），盖在其他 App 上 | 灵动岛 / 实时活动（刷新受限，只作辅助）；主 App 全屏字幕页 |

## 风险项 / 待验证假设（spike 逐条给出 成立 / 证伪 / 未验）
| # | 假设 | 为什么要紧 | 验证方式 |
|---|---|---|---|
| H1 | 免费 Personal Team 能签名构建「主 App + Broadcast Upload Extension」并装到真机 | 付费账号还在审核，免费账号能不能先开发决定节奏 | xcodebuild 真机签名构建 |
| H2 | 广播扩展能拿到 Safari 等其他 App 播放的声音（`RPSampleBufferType.audioApp`） | 整个产品的前提 | 真机：开始广播→Safari 放德语视频→扩展日志/电平 |
| H3 | 扩展内存上限约 50MB，跑不了识别；但能做重采样 + WebSocket 发送 | 决定识别放哪 | 扩展里重采样到 16k + URLSessionWebSocketTask 发节点，看内存与是否被杀 |
| H4 | 扩展进程能访问 Tailscale（100.x）网络并直连节点 | 路线 A 前提 | 真机直连 mini2 `/v1/info` |
| H5 | 免费账号没有 App Group；扩展与主 App 可用 keychain-access-groups 共享配置（token/节点） | 配对信息只录入一次 | 签名构建 + 运行时读写 |
| H6 | 扩展→主 App 的实时文字通道在免费账号下可行（无 App Group 的情况下：本机 TCP/UDP loopback？Darwin notify + keychain？） | 主 App 要拿文字画画中画 | 真机实测一种通道 |
| H7 | 主 App 在后台时画中画仍可持续更新（`AVSampleBufferDisplayLayer` 喂合成帧），且 PiP 可在无真实视频时启动 | 「盖在别的 App 上」的唯一合规方式 | 真机：启 PiP→切到 Safari→文字刷新 |
| H8 | 主 App 后台保活：PiP 在播 + 音频会话，后台不被挂起 | 主 App 收文字与渲染要活着 | 真机后台 10 分钟 |
| H9 | iOS 本机 SpeechAnalyzer + Translation 在主 App 后台可跑（路线 B） | 无节点时的退路 | 后续阶段，spike 不做 |
| H10 | 灵动岛/实时活动刷新太受限，只能辅助 | 定位次要显示 | 文档核实，spike 不做 |

## Constraints
- **隐私**：音频只进内存不落盘；只发往 tailnet 内用户自己的节点（钉 IP、100.64.0.0/10 校验，同 rslite `TailnetPin`）；日志无正文无 token。不接云端（Groq 等本 spec 不做）。
- 原生 Swift / SwiftUI，不引入跨平台框架；逻辑尽量与 `macos-native/` 共享（目标：抽 Swift Package 共用，spike 阶段允许复制）。
- 仓库 PUBLIC：不提交 token、节点 IP 以外的私密配置；spike 不推送、不开 PR。
- 签名：先免费 Personal Team（7 天重签、App ID 数量受限），付费账号到位后切 Team 与 bundle id。
- 不改 `feat/macos-native` 及其它会话的分支；spike 只在 `spike/ios-broadcast`。

## Done criteria（本 spec 的产品级完成标准，spike 只验其中前置部分）
1. 真机：开始广播后，Safari 播德语视频，画中画窗口在 Safari 上方显示原文+中文，端到端延迟 ≤ 3s（节点路线）。
2. 节点不可用时有明确状态提示（文案与 rslite 一致）；路线 B 若可行则自动回本机。
3. 连续 30 分钟不崩、扩展不被系统杀；整机耗电有一次实测记录。
4. 配对：粘贴/扫码 `rsnode://pair?...`（spec 007）录入一次，扩展与主 App 共用。
5. 风格与 Mac/安卓一致（DesignTokens 同源）。

## Spike 范围（本轮）
- S1：免费账号下「主 App + 广播扩展 + PiP」能否签名构建、能否装机跑起来（H1、H5、H7 部分）。
- S2：扩展抓 Safari 声音 → 发 mini2 节点 → 主 App PiP 显示原文/译文（H2、H3、H4、H6、H7、H8）。
- 结论写入本目录 `spike-results.md`。

## 不做（本 spec）
App Store 上架 / TestFlight（等付费账号）；云端识别；收费；iPad 适配；本机识别路线 B 的实现（只作后续阶段）。
