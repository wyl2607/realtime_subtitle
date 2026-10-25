# S2 Spike 结果记录

## 改动摘要

### 1. SharedKeychain 泛化 (`ios/Shared/SharedKeychain.swift`)
- 新增 `Account` 枚举：`extStatus`、`nodeConfig`、`latestLines`
- 新增通用读写方法：`write(_ value: String, account: String)` / `read(account: String)`
- 保留兼容方法：`writeExtStatus`/`readExtStatus`、`writeNodeConfig`/`readNodeConfig`、`writeLatestLines`/`readLatestLines`
- 日志不再输出具体值，只输出 account 名

### 2. 配对链接解析 (`ios/Shared/PairingLink.swift` 新建)
- `NodeConfig: Codable` 含 `id/host/port/token/name/src/dst` 与 `sessionURL` 计算属性
- `PairingLink.parse(_:)` 用 `URLComponents` 解析 `rsnode://pair?v=1&...`
- 校验：v 必须 1；host 非空且仅 `[A-Za-z0-9.-]`；port 1–65535；tok 64 位十六进制
- `displayName` 与 `description` 均不含 token

### 3. 主 App 设置界面 (`ios/RSiOS/ContentView.swift`)
- 新增「粘贴配对链接」按钮（读 `UIPasteboard`）+ 文本框
- 解析成功写 `node_config` 到 keychain，显示「已配对：<name>（<host>）」
- 失败显示「配对链接无效」；界面不显示 token
- 新增 `LinesMonitor` 监听 `com.wyl2607.rsios.spike.lines` 并 0.5s 轮询兜底
- 主界面列出最近句对（原文白、译文黄、空译文显示 `…`）

### 4. 广播扩展 (`ios/RSBroadcast/SampleHandler.swift`) 重写
- `broadcastStarted`：读 `node_config`，无则写 `no_config` 状态；有则建 `PCMConverter` + `NodeSession` 并 `start()`
- `processSampleBuffer(.audioApp)`：保留 RMS/状态统计；有会话时 `converter.append` 得到的每帧 `session.send(pcm16k:)`
- **仅 `ready` 后发送**，之前帧计入 `pre_ready_dropped`
- 事件处理：
  - `ready`：置位 `readyReceived`
  - `final`：追加 `RecentLine{id, src, dst=""}`，最多保留 3 条
  - `translation`：按 id 回填 `dst`
  - `status`：记录 `lastStatusCode`；`isFatal` 后停止发送
- 变化时写 `latest_lines` JSON 到 keychain + post `com.wyl2607.rsios.spike.lines`
- `onClose(code)`：记 `closed=<code>`（1013 显示 `busy`），不自动重连
- `broadcastFinished`：`flush()` 再 `close()`
- 状态行（每秒写 `ext_status`）追加：`node=<name> ready=<0|1> sent=<帧数> finals=<n> tx=<n> last=<status/closed>`
- **日志/状态不含 token 与字幕正文**（正文仅进 `latest_lines`）
- 内存约束：不攒音频、最近句对限 3 条

### 5. computeRMS 修复
- 由 `CMBlockBufferGetDataPointer(..., lengthAtOffsetOut: nil, ...)` 改为取 `lengthAtOffsetOut` 仅读该段，避免非连续 block buffer 越界

### 6. PiP 显示 (`ios/RSiOS/ContentView.swift` 中的 `PiPManager`)
- 画中画帧最多 2 条句对，每对两行：原文 `#F0F0F0`、译文 `#FFE894`（空显示 `…`），旧的一对 alpha 0.55
- 底部小字：时间 + `ready/sent/finals/last` + `后台刷新 N 帧`（`backgroundFrameCount`，前台归零）
- 注册 `lines` Darwin 通知 + 0.5s 轮询读 `latest_lines`

### 7. 网络许可
- 主 App 与扩展 `Info.plist` 均已有 `NSAppTransportSecurity → NSAllowsArbitraryLoads = true`

### 8. 自测 (`ios/Tests/`)
- 新建 `PairingSelftest.swift` 覆盖：合法链接、v≠1、缺 tok、tok 非 64 hex、端口越界、host 非法字符、description 不含 token
- `run-selftest.sh` 编译命令加入 `PairingLink.swift` 与 `PairingSelftest.swift`
- `NodeSelftest.swift` 入口末尾调用 `runPairingSelftest()`（唯一允许改动处）

## 验收输出

```
$ bash ios/Tests/build-check.sh
BUILD-CHECK OK

$ bash ios/Tests/run-selftest.sh
SELFTEST OK
```

## 未做事项（按需求范围外或后续阶段）

1. **真机验证 H2/H3/H4/H6/H7/H8** —— 本次仅模拟器编译通过，真机需：
   - 免费 Personal Team 签名安装（H1/H5）
   - Safari 播放德语视频 → 扩展拿音频 → 连 mini2 节点 → PiP 显示双语（H2/H3/H4/H6/H7）
   - 后台 10 分钟不挂（H8）
2. **路线 B 本机 SpeechAnalyzer + Translation** —— 需求记为后续阶段
3. **付费账号到位后切 Team / App Group** —— 当前免费账号无 App Group，已用 keychain-access-groups 绕过
4. **节点发现/路由（P1 `/v1/info` 选节点）** —— 当前写死单节点配对
5. **TailnetPin（钉 IP 校验 100.64.0.0/10、禁止二次 DNS）** —— NodeSession 目前直连 host，后续需补
6. **协议 v2 完整实现**：`drain`/`flush` 语义、心跳、重连策略
7. **灵动岛/实时活动辅助显示** —— 记为次要

## 真机上最可能出问题的 3 点

1. **扩展内存超限被杀（H3）**
   - 扩展硬性上限 ~50 MB。当前实现：`PCMConverter` 内部有 `pending` 缓冲（最多几帧）、`NodeSession` 无队列直发、`recentLines` 限 3 条。但 `URLSessionWebSocketTask` 内部缓冲、系统音频回调频率、节点冷启动期间 `pre_ready_dropped` 累积可能触发内存增长。真机需 `os_signpost` / Instruments 监控 `phys_footprint`。

2. **Tailscale 网络不可达（H4）**
   - iPhone 必须装 Tailscale 并登录同一 tailnet。扩展进程能否复用主 App 的网络上下文、是否受 `NetworkExtension` 权限限制、DNS 解析 `mini2.tailnet.ts.net` 是否走 Tailscale MagicDNS，均需真机验证。模拟器无 Tailscale，无法复现。

3. **PiP 后台渲染不稳定（H7/H8）**
   - `AVSampleBufferDisplayLayer` 在后台持续 enqueue、音频会话 `.playback + .mixWithOthers` 能否长期保活，iOS 版本差异大。`backgroundFrameCount` 计数仅作观测，真机可能因内存/电量策略被挂起。需 30 分钟实测确认。

---

*生成时间：2026-10-13*