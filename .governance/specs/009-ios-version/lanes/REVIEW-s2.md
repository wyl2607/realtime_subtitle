# iOS Spike 评审报告

## 问题列表（按严重度排序）

### 1. 线程安全问题：共享状态无同步（危急）
- **文件**: ios/RSBroadcast/SampleHandler.swift:24-33
- **会出什么事**: `readyReceived`、`framesSent`、`finalsCount`、`translationsCount`、`preReadyDropped`、`lastStatusCode`、`fatalErrorOccurred`、`recentLines` 变量在 URLSession 回调线程（`handleNodeEvent`/`handleNodeClose`）、ReplayKit 线程（`processSampleBuffer`）和主线程（通过 `writeStatus`/`writeLatestLines` 间接访问）之间无同步访问。可能导致竞态条件、数据损坏、崩溃或未定义行为，进而导致扩展被系统杀掉。
- **怎么修**: 使用串行 dispatch queue 或 NSLock 保护这些共享变量的所有读写操作。例如，在 SampleHandler 中添加一个私有队列，所有对这些变量的访问都通过该队列同步。

### 2. 隐私问题：字幕文本写入 keychain（高危）
- **文件**: 
  - ios/RSBroadcast/SampleHandler.swift:247-257（`writeLatestLines`）
  - ios/Shared/SharedKeychain.swift:99-106（`writeLatestLines`/`readLatestLines`）
- **会出什么事**: 扩展通过 `SharedKeychain.writeLatestLines` 将最近的句对（包含原文和译文）写入 keychain。虽然 keychain 是加密的，但这违背了「音频只进内存不落盘」的隐私精神（虽然音频未落盘，但用户文本落盘了）。keychain 数据可能通过 iCloud 备份泄露，增加隐私风险。
- **怎么修**: 改为仅在内存中共享字幕数据（例如使用 Darwin 通知传递文本内容，或仅传递增量更新而非完整句对）。若必须持久化，应考虑使用加密的应用沙盒文件夹（但需确保不备份）。

### 3. PiP 透明度逻辑错误：最新字幕被错误地调淡（高危）
- **文件**: ios/RSiOS/ContentView.swift:547-556（在 `renderSubtitleImage` 方法中）
- **会出什么事**: 代码中 `let displayLines = Array(cachedLines.suffix(2))` 获取最近两条字幕，然后在绘制循环中 `let alpha = idx == 0 ? 1.0 : 0.55` 使得第一条（较旧的字幕）完全不透明，第二条（最新的字幕）半透明（0.55 alpha）。这导致最新的字幕视觉效果变弱，与用户期望相反（应让最新字幕更突出）。
- **怎么修**: 将 alpha 逻辑改为 `let alpha = idx == 0 ? 0.55 : 1.0`（让最新字幕不透明，较旧字幕半透明），或更清晰地按时间顺序绘制并让最新字幕在上层。

### 4. 状态行遗漏预备丢帧计数（中危）
- **文件**: ios/RSBroadcast/SampleHandler.swift:127（状态字符串构建）
- **会出什么事**: 变量 `preReadyDropped` 已在 `processSampleBuffer` 中累计（第142行），但未包含在每秒写入 keychain 的状态字符串中。用户无法通过状态行了解节点就绪前丢帧情况，影响故障诊断。
- **怎么修**: 在状态字符串中加入 `preReady=\(preReadyDropped)` 字段（例如在 `sent=\(framesSent)` 后添加）。

### 5. keychain 写入频率过高（中危）
- **文件**: 
  - ios/RSBroadcast/SampleHandler.swift:122-132（每秒写入状态）
  - ios/RSBroadcast/SampleHandler.swift:158、163（每条 final/translation 事件写入句对）
- **会出什么事**: keychain 写入涉及磁盘 I/O，频繁写入可能造成性能开销（尤其在设备存储 I/O 压力大时）。虽然单次数据量小，但长期运行可能影响系统流畅度或被看板异常检测。
- **怎么修**: 
  - 状态写入可降低频率（例如每 5 秒一次），除非实时监控有严格要求。
  - 句对写入可改为仅在有新内容时写入（当前已是事件触发，但若频率极高仍可考虑节流：例如只在字幕内容变化时写入，或使用内存缓存批量写入）。

### 6. fatal error 状态未充分传达（低危）
- **文件**: ios/RSBroadcast/SampleHandler.swift:168-170（`fatalErrorOccurred` 设置）和 134（提前返回）
- **会出什么事**: 当节点返回致命状态码时，`fatalErrorOccurred` 被设为 true，随后 `processSampleBuffer` 会直接返回早退。然而，扩展没有尝试重连或向用户明确指示发生了不可恢复的错误，状态行仅显示最后的状态码（如 `closed=1013`），用户可能误认为是临时问题。
- **怎么修**: 在致命错误发生时，应通过状态行明确提示错误类型（如 `fatal: protocol_error`），并在可能的情况下尝试重新启动节点会话（或至少在下次 `broadcastStarted` 时重置状态）。

### 7. PiP 渲染定时器可能造成不必要的开销（低危）
- **文件**: ios/RSiOS/ContentView.swift:430-438（`startRendering` 方法中的 renderTimer）
- **会出什么事**: renderTimer 每 0.5 秒触发一次渲染，即使字幕内容和状态未变化也会重复生成帧并送到 `AVSampleBufferDisplayLayer`。这造成不必要的 CPU/GPU 使用。
- **怎么修**: 仅在字幕内容（`cachedLines`）或状态（`SharedKeychain.readExtStatus()`）实际变化时触发渲染。可通过在 `refreshLinesFromKeychain` 和 `updateKeychainStatus` 中检测变化后重启渲染定时器（或直接调用 `renderFrame`）。
