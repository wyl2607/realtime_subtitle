# 本机识别探针实现结果

## 改动清单

### 1. `ios/Shared/SharedKeychain.swift`
- 新增 `Account.engineMode = "engine_mode"` 常量
- 新增 `writeEngineMode(_:)` / `readEngineMode()` 读写方法
- 用于在主 App 与广播扩展间共享识别引擎选择（`node` 或 `local`，默认 `node`）

### 2. `ios/RSiOS/ContentView.swift`
- 导入 `Speech`、`Translation` 框架
- 新增状态：`engineMode`、`prepareStatus`、`isPreparing`
- UI 新增分段控件：「识别：电脑节点 / 本机」→ 写入 `engine_mode`
- UI 新增按钮：「准备本机识别资源」：
  - `#available(iOS 26.0, *)` 时：用 `AssetInventory.status(forModules:)` 检查 de-DE SpeechTranscriber 资源；用 `LanguageAvailability().status(from:to:)` 查 de→zh-Hans 翻译可用性；在状态栏显示 `ASR资源: installed/notInstalled · 翻译 de→zh-Hans: installed/supported/unsupported`
  - iOS < 26 时按钮显示「需要 iOS 26」，点击直接置状态
- `onAppear` 读取 `engine_mode` 同步分段控件初始值

### 3. `ios/RSBroadcast/LocalEngine.swift`（新文件，整体 `@available(iOS 26.0, *)`）
- `start(onFinal:, onTranslation:, onStatus:)`：
  - 校验 de-DE 支持、资源已装（否则 `onStatus("asr_assets_missing")` 返回）
  - 检查翻译可用性（不可用时 `onStatus("tx_unavailable")` 但仍启动只出原文）
  - 建 `SpeechTranscriber`（`reportingOptions: [.volatileResults, .fastResults]`, `attributeOptions: [.audioTimeRange]`）
  - 用 `SpeechAnalyzer.bestAvailableAudioFormat(compatibleWith:considering:)` 拿目标格式
  - 创建 `SpeechAnalyzer` + `AsyncStream<AnalyzerInput>` 输入流
  - 并行跑 `transcriber.results` 消费 final 结果 → 分配递增 id → `onFinal(id, text)` → 再用 `TranslationSession(installedSource:target:)` 翻译 → 成功 `onTranslation(id, translated)`
- `append(pcm16k: Data)`：把 16kHz 单声道 s16le 3200 字节帧转 `AVAudioPCMBuffer`，必要时用 `AVAudioConverter` 转到 analyzer 格式，推进输入流
- `stop()`：finalize analyzer、结束输入流、取消任务

### 4. `ios/RSBroadcast/SampleHandler.swift`
- 新增 `import Speech; import Translation`
- 新增属性：`localEngine: AnyObject?`、`engineMode: String`
- `broadcastStarted`：读 `engine_mode`
  - `local` 且 iOS 26+ → `startLocalEngine()`（建 PCMConverter + LocalEngine，**不连节点**）
  - `local` 但 < iOS 26 → 状态 `last=local_needs_ios26`、置 fatal
  - `node` → 原有流程（读配置、建 NodeSession、连节点）
- `processSampleBuffer`：
  - `engineMode == "local"` → converter 吐帧喂 `LocalEngine.append(pcm16k:)`
  - `node` → 原有逻辑（等 ready 后 `session.send(pcm16k:)`）
- `broadcastFinished`：local 模式调 `localEngine?.stop()` 清理
- 状态行新增 `engine=local|node` 字段；`mem=` 保留（H3/H9 关键数据）

## 验收输出

```
$ bash ios/Tests/build-check.sh
SELFTEST OK
... (xcodegen + xcodebuild) ...
** BUILD SUCCEEDED **
APPEX exists
"RPBroadcastProcessMode" => "RPBroadcastProcessModeSampleBuffer"
"AppIdentifierPrefix" => ""
"AppIdentifierPrefix" => ""
No 'import Shared' found
No 'type: framework' in project.yml
BUILD-CHECK OK
```

## 真机上最可能出问题的 3 点

1. **扩展内存超限被杀（H3 验证的核心风险）**
   - 广播扩展硬上限 ~50MB；`SpeechAnalyzer` + `SpeechTranscriber` 模型在系统守护进程跑虽不计入扩展进程，但 `AVAudioConverter`、输入流缓冲、PCMConverter 暂存、翻译会话对象等仍在扩展进程。真机上连续跑 5–10 分钟观察 `mem=` 是否逼近 45MB+，一旦接近上限会被 jetsam 静默杀掉，日志只剩 `mem=XX` 最后一行。

2. **SpeechTranscriber 资源未安装导致静默失败**
   - `AssetInventory.status` 返回 `.notInstalled` 时探针会回 `asr_assets_missing` 并不启动，但用户若在主 App 点过「准备本机识别资源」下载、随后卸载/系统回收存储，扩展启动时会直接进入不可用分支。需在真机反复安装/卸载/离线场景验证状态机完整性。

3. **TranslationSession 并发/生命周期竞态**
   - `TranslationSession` 文档未明确是否线程安全；探针在 `handleResult`（识别结果回调线程）里直接 `await session.translate(text)`。若识别吞吐 > 翻译吞吐，会堆积大量并发翻译 Task，可能触发系统限流或内存抖动。真机上需压测高语速德语（如 tagesschau 1.5 倍速）观察 `tx=` 计数是否跟上 `finals=`、是否出现 `tx_error`/`tx_empty`。