# iOS 客户端（实时字幕）

同一套节点 / 本机 / 云端路由的 iOS 端：广播扩展抓 App 声音，主 App 抓麦克风，两者共用 `Engine/SubtitleEngine.swift`。

## 功能概览

### 两种音源

| 音源 | 入口 | 说明 |
|---|---|---|
| 屏幕广播 | `RSBroadcast/SampleHandler.swift` | ReplayKit 上传扩展；只接收 `.audioApp`（App 播放的声音），过滤留在适配层 |
| 麦克风 | `RSiOS/MicCapture.swift` | 主 App 用 `AVAudioEngine` 输入 tap（`inputNode.outputFormat(forBus:)`、bufferSize 4096）采集，复制 buffer 后投到串行队列，再调 `SubtitleEngine.ingest(pcmBuffer:)` |

两者都进 `Engine/SubtitleEngine.swift`，共用 PCM 转换、路由、状态行、节点会话、本机引擎和云端档。同一时间只跑一个：`Shared/CaptureArbiter.swift` 在 12 秒窗口内看到 `src=broadcast` 的状态行就拒绝开麦克风；麦克风运行中收到广播状态则停掉麦克风（广播侧没有反向检查，先开麦克风再开广播时由主 App 让出）。

### 三个引擎档位

主界面「识别引擎」写入 Keychain 的 `engine_mode`，下次开始采集时生效。

| 档位 | 行为 |
|---|---|
| 自动（`auto`） | 按「云端（Groq）→ 电脑节点 → 本机」选路；节点失败时本机接管，节点恢复后等当前句收尾再切回 |
| 电脑节点（`node`） | 只用已配对的节点；没配对时状态写 `no_config`，断线按 `ReconnectPolicy` 退避重连 |
| 本机（`local`） | iOS 26+ 的 `SpeechAnalyzer` 识别 + `Translation` 翻译；不是 iOS 26 时状态行写 `last=local_needs_ios26` 并停止 |

自动档的档位判定在 `Shared/Routing.swift`（`CloudRouteContext.tier()`），执行在 `Shared/AutoFallback.swift`：

- **云端**：Keychain 有 Groq key、用户打开云端开关、本机翻译可用（iOS 26 且 de→zh-Hans 已安装）。云端只出原文，译文仍走本机 `TranslationSession`。
- **节点**：已配对、节点已 `ready`、不是本机自身（iOS 端 `nodeIsSelf` 固定为 false）。
- **本机**：前两级都不可用且运行在 iOS 26+。
- **回退**：云端连续 3 段失败（含 5xx/网络错误）进入退避冷却（5/30/120/300 秒），429 冷却 `retry_after`（缺省 60 秒），额度 ≥90% 冷却、<80% 恢复，401/403 标记 key 失效直到换 key；冷却到期只发一段 probe。云端不在位时，节点失败或 15 秒未 `ready` 转本机并继续重连；节点恢复时若本机还有句子在识别或译文未落地，先等收尾再切回。
- iOS <26 时自动档没有本机可退：节点不可用只显示「不可用，正在重连」。

本机档和云端档都依赖 iOS 26。开始前点「准备本机识别资源」：代码检查德语 `SpeechTranscriber` 的资源状态和 de→zh-Hans 的 `LanguageAvailability`，并显示结果；当前实现只调用 `AssetInventory.status` 查询，没有主动下载调用，未安装时只提示「再次准备将下载资源」。

### 字幕显示

- 最近句对：引擎把最近 3 句写进 Keychain `latest_lines`，主 App 每 0.5 秒轮询并监听 Darwin 通知。
- 画中画：`RSiOS/ContentView.swift` 的 `PiPManager` 把最近 2 句渲染成 1280×360 图像，经 `AVSampleBufferDisplayLayer` 送 PiP；主 App `UIBackgroundModes` 含 `audio`。
- 今日字幕：`RSiOS/TranscriptStore.swift` 写 `Documents/transcripts/YYYY-MM-DD.txt`，每行 `HH:mm:ss\t原文\t中文\t去重键`；「今天的字幕」页面读当天文件回看。

## 生成 Xcode 工程

```bash
cd ios
xcodegen generate
```

会生成 `RSiOS.xcodeproj`。`xcodegen` 在 Homebrew 路径下，先 `export PATH=/opt/homebrew/bin:$PATH`。

## 构建（模拟器，免签名）

```bash
xcodebuild -project RSiOS.xcodeproj -scheme RSiOS -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO build
```

预期输出以 `** BUILD SUCCEEDED **` 结尾。

## 验证扩展已嵌入

```bash
find ~/Library/Developer/Xcode/DerivedData -path '*Debug-iphonesimulator/RSiOS.app/PlugIns/RSBroadcast.appex' -maxdepth 8
```

或指定派生数据路径：

```bash
xcodebuild -project RSiOS.xcodeproj -scheme RSiOS -sdk iphonesimulator -configuration Debug CODE_SIGNING_ALLOWED=NO -derivedDataPath build build
find build -path '*RSBroadcast.appex'
```

## 真机构建（需签名）

```bash
xcodebuild -project RSiOS.xcodeproj -scheme RSiOS -sdk iphoneos -configuration Debug \
  DEVELOPMENT_TEAM=<YOUR_TEAM_ID> \
  CODE_SIGN_STYLE=Automatic \
  build
```

- `DEVELOPMENT_TEAM` 填自己的 Team ID（示例占位）。
- 免费账号不支持 App Group，本工程只用 `keychain-access-groups` 共享数据。

## 目录结构

根目录：`project.yml`（XcodeGen 规格）、`.gitignore`（忽略 `*.xcodeproj`、`build/`、`DerivedData/` 等）。

### Engine/

| 文件 | 职责 |
|---|---|
| `SubtitleEngine.swift` | 共享路由引擎：`start` / `finish` / 两个 `ingest`、每 5 秒状态行、按档位分发音频、节点会话与重连、本机与云端档接线、写 Keychain |
| `LocalEngine.swift` | iOS 26 的 `SpeechAnalyzer` + `Translation`；两种模式（识别+翻译 / 仅翻译，后者给云端档用），输出整句与时间区间 |
| `ReconnectPolicy.swift` | 按 WebSocket 关闭码决定是否重连和等待秒数（1008/1003/1009 不重连，1013 固定 10 秒，其余 1/2/4/8/16/30 秒退避） |

### RSBroadcast/

| 文件 | 职责 |
|---|---|
| `SampleHandler.swift` | ReplayKit 适配层：`broadcastStarted` → `start()`、`broadcastFinished` → `finish()`、`.audioApp` 帧 → `ingest(sampleBuffer:)`；`onFatalError` 转成 `finishBroadcastWithError` |
| `Info.plist` | 扩展声明：`com.apple.broadcast-services-upload`、`RPBroadcastProcessModeSampleBuffer`、语音识别用途说明 |
| `RSBroadcast.entitlements` | 共享 Keychain access group |

### RSiOS/

| 文件 | 职责 |
|---|---|
| `ContentView.swift` | 主界面：广播选择器、麦克风开始/停止、节点配对、引擎档位、准备本机资源、Groq key/开关、PiP、扩展状态、最近句对、今日字幕入口；以及 `PiPManager`、`StatusMonitor`、`LinesMonitor` 等监听 |
| `MicCapture.swift` | 麦克风采集：权限申请、音频会话（开始 `playAndRecord` / 停止还原 `playback`）、中断与路由变化处理，buffer 投给共享引擎 |
| `TranscriptStore.swift` | 当天字幕存档，按「会话代号 + 句子 id」去重，译文后到原地更新 |
| `TranscriptView.swift` | 「今天的字幕」只读页面 |
| `RSiOSApp.swift` | App 入口 |
| `Assets.xcassets/` | App 图标资源 |
| `Info.plist` | 主 App 声明：麦克风/语音识别用途说明、`rsnode` URL scheme、`UIBackgroundModes: audio`、ATS |
| `RSiOS.entitlements` | 共享 Keychain access group |

### Shared/（两个 target 都编）

| 文件 | 职责 |
|---|---|
| `SharedKeychain.swift` | 跨进程 Keychain 读写；`Account` 定义全部共享键 |
| `TailnetPin.swift` | 解析节点主机名，要求所有 IPv4 都在 `100.64.0.0/10`，再把 URL host 换成已校验的 IP 字面量 |
| `Routing.swift` | `EngineMode`、云端/节点/本机三级状态机（`CloudRouteContext`）与夹具类型 |
| `AutoFallback.swift` | 自动档决策：节点失败/就绪、本机句收尾、云端成功/失败/HTTP 状态/冷却、额度反馈 |
| `CaptureArbiter.swift` | 广播与麦克风互斥的纯函数；麦克风与 PiP 的音频会话目标配置 |
| `EngineRunGate.swift` | 用自增代号把异步回调限制在所属的引擎轮次内；`mayTouchNode` 判定哪些档位可以碰节点 |
| `NodeProtocol.swift` | 节点协议 v2：hello、事件解析、关闭码还原、`ReadyDeadlineBox`、`NodeSession` WebSocket 与 15 秒 ready 看门狗 |
| `PairingLink.swift` | `NodeConfig` 与 `rsnode://pair` 链接解析/预填 |
| `PCMConverter.swift` | 任意采样率/声道/格式 → 16 kHz 单声道 s16le，每帧 100 ms（3200 字节） |
| `CloudClient.swift` | Groq 语音转写请求、multipart 构造、响应与限流头解析 |
| `CloudSegmenter.swift` | 云端分段：20 ms 帧、RMS ≥0.005 算有声、静音 ≥0.6 秒收段、最长 15 秒强切、短段合并 |
| `QuotaLedger.swift` | Groq 额度账本：分钟请求数、小时音频秒数、每日请求数与音频秒数 |
| `LineStore.swift` | P5 行合并/覆盖规则，与 mac rslite 同源 |
| `RecentLines.swift` | 「会话代号 + id」查行；切回节点前的收尾判定 |
| `LocalEngineStatus.swift` | 状态行里的本机失败码 → 中文提示 |
| `DesignTokens.swift` | 由 `scripts/design/gen_tokens.py` 生成的设计令牌 |
| `Resources/Info.plist` | 未被两个 target 编入（`project.yml` 排除了 `**/Resources/**`） |

### Tests/ 与 scripts/

| 文件 | 职责 |
|---|---|
| `run-selftest.sh` | 用 `swiftc` 编 7 个自测入口并逐个运行，各入口打印自己的 `SELFTEST OK` |
| `build-check.sh` | 跑自测后断言工程结构、模拟器构建、扩展与主 App 的 plist |
| `NodeSelftest.swift` | 节点协议、ready 看门狗、PCM、配对、Tailnet 校验、重连（入口） |
| `PCMConverterSelftest.swift` | `append(pcmBuffer:)` 的分帧与余量 |
| `PairingSelftest.swift` | `rsnode://pair` 解析与预填 |
| `TailnetPinSelftest.swift` | `100.64.0.0/10` 判定 |
| `ReconnectSelftest.swift` | 重连退避序列 |
| `RoutingSelftest.swift` | 路由夹具入口 |
| `LineStoreSelftest.swift` | P5 夹具 |
| `AutoFallbackSelftest.swift` | 三级自动回退决策 |
| `TranscriptStoreSelftest.swift` | 存档去重与跨天 |
| `CloudSelftestMain.swift` | 云端纯逻辑独立入口 |
| `CloudClientSelftest.swift` | multipart 与响应解析 |
| `QuotaLedgerSelftest.swift` | 计费下限、90/80 滞回、落盘 |
| `CloudSegmenterSelftest.swift` | 静音收段、强切、短段合并 |
| `RecentLinesSelftest.swift` | 会话查行与交接判定 |
| `CaptureArbiterSelftest.swift` | 互斥与音频会话目标 |
| `EngineRunGateSelftest.swift` | 旧回调不能跨轮次 |
| `LocalEngineStatusSelftest.swift` | 失败码 → 文案映射 |
| `fixtures/p5_cases.json` | 与 mac rslite 共用的 P5 用例 |
| `fixtures/route_cases.json` | 与 mac rslite 共用的路由用例 |
| `ios/scripts/archive.sh` | TestFlight 打包：Release archive → 导出或上传 ipa |

## 进程间共享

主 App 与广播扩展没有 App Group（免费账号不支持），只共享 Keychain access group：

- access group：`$(AppIdentifierPrefix)com.yilinwang.realtimesubtitle.shared`
- service：`rsios.spike`

`SharedKeychain.Account` 里的键：

| 键 | 用途 |
|---|---|
| `ext_status` | 引擎状态行；广播扩展或主 App 麦克风写入，主 App 轮询/通知读取 |
| `node_config` | 已配对节点的 JSON（id/host/port/token/name/src/dst） |
| `latest_lines` | 最近 3 句的 JSON（id、原文、译文、会话代号） |
| `engine_mode` | `auto` / `node` / `local`；没写过或旧值不认识时按 `auto` |
| `groq_key` | Groq API key；只存 Keychain，界面只显示后 4 位，空值视为没有 key |
| `cloud_enabled` | 云端开关 `"1"`/`"0"`；默认关闭 |

状态与句对另用 Darwin 通知即时唤醒主 App：

- `com.wyl2607.rsios.spike.tick`：状态行更新
- `com.wyl2607.rsios.spike.lines`：最近句对更新

通知只是加速；主 App 仍有 0.5 秒轮询兜底。

## 状态行

启动时先写 `started mode=<auto|node|local>`；`started`、`finished`、`no_config`、`last=<码>` 这类短状态会自动补 `src=<broadcast|mic>`。有音频输入时每 5 秒写一行完整状态：

| 字段 | 含义 |
|---|---|
| `blocks=` | 收到的音频块数 |
| `src=` | 音源：`broadcast`（扩展）或 `mic`（主 App） |
| `rate=` | 输入采样率（Hz） |
| `ch=` | 输入声道数 |
| `fmt=` | `float32` / `int16le` / `int16be` / `other` |
| `engine=` | 实际执行档位：本机档为 `local`，显式节点档为 `node`，自动档为 `cloud` / `node` / `local` |
| `node=` | 已配对节点名（本机档或本机接管时不出现） |
| `ready=` | 节点是否已 `ready`（1/0） |
| `sent=` | 已发给节点的帧数（每帧 100 ms） |
| `finals=` | 定稿句计数（节点/本机；云端段不计数） |
| `tx=` | 译文计数（节点/本机/云端） |
| `last=` | 最近状态（状态码或中文文案）；空时为 `-` |
| `drop=` | 节点 ready 前（或还没有会话时）丢掉的帧数 |
| `reconn=` | 节点重连次数 |
| `cloud=` | 仅云端档出现：`up:<上传段数> drop:<丢段数>` |

完整行还带 `flags=`、`ileave=`（1 交错 / 0 非交错）、`rms=`（平均 RMS）、`mem=`（常驻内存 MB）；致命错误会追加 `fatal:<码>`。本机档（含自动档落到本机）不写 `node/ready/sent/drop/reconn`。

`LocalEngineStatus` 把状态行里的 `last=` / `fatal:` 本机失败码映射成主界面红字，原始状态行仍保留：

| 码 | 界面提示 |
|---|---|
| `asr_unsupported` | 本机不支持德语识别 |
| `asr_assets_missing` | 本机识别资源未下载，请点「准备本机识别资源」 |
| `analyzer_format_failed` | 本机音频格式不支持，请停止后重新开始 |
| `tx_unavailable` / `tx_prepare_failed` | 本机翻译资源未下载，只显示原文 |
| `local_needs_ios26` | 本机识别需要 iOS 26 或更新 |
| `asr_failed…` / `asr_result_failed…` | 本机识别出错，请停止后重新开始 |

`tx_empty` / `tx_error` 没有映射，只在状态行里显示原始码。

## 权限

- **麦克风**：`RSiOS/Info.plist` 的 `NSMicrophoneUsageDescription`；点「开始」时用 `AVAudioApplication.requestRecordPermission` 申请。拒绝后状态显示「麦克风权限被拒绝，请到 设置 → 实时字幕 打开」。
- **语音识别**：`NSSpeechRecognitionUsageDescription`。主 App 在档位为 `local` 或 `auto` 时申请一次：麦克风授权通过后、以及点「准备本机识别资源」时。拒绝只提示，不阻止麦克风开始（节点/云端档不需要语音识别）。
- 广播扩展的 `Info.plist` 也有语音识别用途说明，但扩展里弹不出授权框；授权要在主 App 里完成。广播选择器 `showsMicrophoneButton = false`，扩展不采集麦克风。

## 隐私

音频只走三条路：

1. 已配对的电脑节点：节点 token 随 `node_config` 存 Keychain；`TailnetPin` 把节点主机名解析成 IPv4，要求所有地址都在 Tailscale `100.64.0.0/10`，再把 URL host 换成校验过的 IP 字面量，避免连接时二次解析。
2. 本机识别：iOS 26 的 `SpeechAnalyzer` + `Translation`，在设备上识别/翻译，代码不发网络请求。
3. Groq 云端：只有用户保存了 key、打开「启用云端识别」且本机翻译可用时才参与路由；音频按静音切段上传。Groq key 只进 Keychain，界面只显示后 4 位；云端开关默认关闭。

当天的字幕存档写在主 App 的 `Documents/transcripts/`，不经过网络。

## 自测与门禁

```bash
export PATH=/opt/homebrew/bin:$PATH
bash ios/Tests/run-selftest.sh
bash ios/Tests/build-check.sh
python3 scripts/design/gen_tokens.py --check
python3 scripts/design/check_parity.py
```

`run-selftest.sh` 用 `swiftc` 编译并运行 7 个入口：

- `nodeselftest`：节点协议/ready 看门狗、PCM 转换、配对链接、Tailnet 校验、重连策略
- `parityselftest`：路由夹具、P5 行仓库、自动回退、字幕存档
- `cloudselftest`：Groq 请求/响应、额度账本、云端分段
- `handoffselftest`：会话查行与交接判定
- `capturearbiterselftest`：广播/麦克风互斥、音频会话目标
- `enginerungateselftest`：引擎轮次闸门
- `localenginestatusselftest`：本机失败码映射

`build-check.sh` 额外断言：`Engine/` 存在且被两个 target 编入、`SampleHandler.swift` ≤120 行、`MicCapture.swift` 存在、主 App plist 有 `NSMicrophoneUsageDescription` 和 `rsnode`、扩展 plist 有 `RPBroadcastProcessModeSampleBuffer`、主界面包在 `ScrollView` 里、没有 `import Shared` / `type: framework`，并真跑一次模拟器构建。

## 部署目标与 Swift 版本

以 `ios/project.yml` 为准：

- 部署目标 iOS 17.0（`IPHONEOS_DEPLOYMENT_TARGET` / `deploymentTarget.iOS`）
- Swift 5.0（`SWIFT_VERSION` / `swiftVersion`）
- `SWIFT_STRICT_CONCURRENCY: minimal`
- XcodeGen 最低 2.40.0，`xcodeVersion: 27.0`
- Bundle ID：主 App `com.yilinwang.realtimesubtitle`，扩展 `com.yilinwang.realtimesubtitle.broadcast`

本机识别与云端档在运行时用 `#available(iOS 26.0, *)` 判断；iOS 17–25 仍可用广播/麦克风 + 电脑节点。
