# I8 ios-mic-mode — iOS 主 App 麦克风模式（同一套 节点 / 本机 / 云端 路由）

> 仓库：realtime_subtitle，分支 `lane/i8-ios-mic-mode`（基于 `lane/rs010-int3` @ f11aa05）。
> 在 MacBook Air 上开发（Xcode / xcodegen / 真机都在这台）。草稿 PR 指向 `lane/rs010-int3`。

## 为什么
2026-10-21 晚首次真机（iPhone 15 Pro）验证：广播模式 + Windows 节点效果很好（tagesschau 实测，识别 0.2–0.5s/段、翻译 0.1–0.3s/句）。
但 iOS 现在**只能字幕 iPhone 自己在播的声音**，听不了房间里有人说话。用户要的是面对面 / 上课场景，对应 Android 的「麦克风」音源。

## 现状（int3 f11aa05 核实过）
- `ios/RSBroadcast/SampleHandler.swift` 约 818 行：`guard sampleBufferType == .audioApp` —— 只处理 App 音频。
- `ios/RSiOS/ContentView.swift` `BroadcastPickerView`：`showsMicrophoneButton = false`。
- 整个 `ios/` 没有 AVAudioEngine / 录音代码，`RSiOS/Info.plist` 没有 `NSMicrophoneUsageDescription`（`UIBackgroundModes` 已有 `audio`，给 PiP 用）。
- 全部路由逻辑（节点会话、AutoFallback、本机 LocalEngine、云端 Groq、P5 行仓库、状态行 / 最近句对写 Keychain + Darwin 通知）都写在 `SampleHandler` 一个类里（约 1230 行）；它对 ReplayKit 的依赖只有：基类 `RPBroadcastSampleHandler`、三个 override、`RPSampleBufferType`。
- `NodeSession`、`Routing`、`AutoFallback`、`CloudClient/CloudSegmenter/QuotaLedger`、`PCMConverter`、`LineStore`、`SharedKeychain` 已在 `ios/Shared/`（两个 target 都编）；`LocalEngine.swift`、`ReconnectPolicy.swift` 在 `ios/RSBroadcast/`（只有扩展编）。

## 设计（用户要求：麦克风在**主 App 里**采，走**同一套**路由，不是只把广播的麦克风打开）
1. **抽出共享引擎**：把 `SampleHandler` 的类体搬到新文件 `ios/Engine/SubtitleEngine.swift`（`final class SubtitleEngine`，不依赖 ReplayKit）：
   - `broadcastStarted` → `start()`，`broadcastFinished` → `finish()`；
   - `processSampleBuffer` 拆成：`ingest(sampleBuffer:)`（广播用）、`ingest(pcmBuffer:)`（麦克风用），两者共用的「5 秒状态行 + 自动模式 tick」和「按档位分发帧（本机 / 云端 / 节点）」抽成私有方法，**逻辑不改**；
   - 状态行加一个来源字段（如 `src=broadcast` / `src=mic`），方便真机区分。
   - `SampleHandler.swift` 只剩适配：持有 `SubtitleEngine`，三个 override 转发（`.audioApp` 过滤留在适配层）。
   - `LocalEngine.swift`、`ReconnectPolicy.swift` 移到 `ios/Engine/`；`project.yml` 两个 target 都加 `path: Engine`；`ios/Tests/run-selftest.sh` 里 `ReconnectPolicy.swift` 的路径跟着改。
2. **PCMConverter 新入口**：`append(pcmBuffer: AVAudioPCMBuffer) -> [Data]`，`append(_ sampleBuffer:)` 改为提取后调用它（行为不变）。
3. **麦克风采集**（新文件 `ios/RSiOS/MicCapture.swift`）：`AVAudioEngine` 输入节点 tap（用 `inputNode.outputFormat(forBus: 0)`），回调里把 buffer 投到一条串行队列再 `engine.ingest(pcmBuffer:)`（不在实时音频线程上做转换和加锁）。
   - 音频会话：`.playAndRecord`，选项 `[.mixWithOthers, .defaultToSpeaker, .allowBluetooth]`，**不能打断 PiP**（`ContentView.swift` 约 535 行 PiP 现在设的是 `.playback + .mixWithOthers`，两边要协调成同一个类别，开始麦克风时切到 playAndRecord，停止时还原）。
   - 处理中断 / 路由变化（来电、拔耳机）：中断结束后重启 tap 或停止并在状态里说明。
4. **权限**：`Info.plist` 加 `NSMicrophoneUsageDescription`（中文，说明音频只发给已配对的电脑节点、本机识别或用户自己开启的云端识别）。点「开始」时用 `AVAudioApplication.requestRecordPermission` 申请；拒绝时显示「麦克风权限被拒绝，请到 设置 → 实时字幕 打开」，不崩、不静默。
5. **界面**：主页在 Broadcast 选择器旁边加「麦克风字幕」区：开始 / 停止按钮 + 一行状态。字幕显示复用现有「最近句对」/ PiP / 今日字幕（引擎写 Keychain + Darwin 通知，App 里现有监听直接生效）。
   - 广播和麦克风**不能同时跑**（两边写同一组 Keychain 键）：麦克风开始前若扩展状态显示广播在跑，提示先停广播；反之广播开始时 App 停掉麦克风。判定写成纯函数。
6. **共用设计令牌**：新增的用户可见状态文案进 `design/tokens.json`（`python3 scripts/design/gen_tokens.py` 生成四端），`check_parity.py` 要过。

## 待办
- [x] 1. `SubtitleEngine` 抽取（纯搬家 + 拆 ingest），`SampleHandler` 变适配层；`project.yml` / `run-selftest.sh` 路径更新（406801c）
- [ ] 2. 广播回归：真机广播模式跑一遍 tagesschau，结果与抽取前一致（node 会话、字幕、状态行）
- [x] 3. `PCMConverter.append(pcmBuffer:)` + 自测（48k Float32 单声道 0.25s → 2 帧 3200 字节，余量留在 pending）（406801c）
- [x] 4. `MicCapture`：AVAudioEngine tap → 串行队列 → `SubtitleEngine.ingest(pcmBuffer:)`；音频会话与 PiP 共存；中断处理（b87878b）
- [x] 5. 麦克风权限：`NSMicrophoneUsageDescription` + 运行时申请 + 拒绝提示（b87878b、2361019）
- [x] 6. 主页「麦克风字幕」开始 / 停止 + 状态；广播与麦克风互斥（纯函数 + 自测）（b87878b）
- [x] 7. 新文案进设计令牌，`gen_tokens.py --check`、`check_parity.py` 通过（b87878b）
- [x] 8. `build-check.sh` 加断言：`Info.plist` 有 `NSMicrophoneUsageDescription`、Engine 目录被两个 target 编进去（406801c、b87878b）
- [x] 9. 门禁全绿：`bash ios/Tests/run-selftest.sh && bash ios/Tests/build-check.sh && python3 scripts/design/check_parity.py`（b87878b）
- [ ] 10. 真机验证（iPhone 15 Pro）：节点模式（Mac `say -v Anna "…"` 放德语，麦克风能听到）→ 字幕 + 翻译；节点日志 `session_start device=iphone-15-pro`；本机模式（iOS 26+ SpeechAnalyzer）；锁屏 / PiP 下继续出字幕；拒绝权限的提示
- [ ] 11. 合进 `lane/rs010-int3`（先拉 GitHub 上最新的 int3 再合——用户会直接在 GitHub 合 PR 进去），推送

## 验证命令（MacBook Air，worktree 根目录）
```bash
export PATH=/opt/homebrew/bin:$PATH
bash ios/Tests/run-selftest.sh && bash ios/Tests/build-check.sh && python3 scripts/design/check_parity.py
```

## 真机构建 / 安装（踩过的坑，照做）
- 设备：有线的 iPhone 15 Pro「iPhone von Antonio」，UDID `00008130-001A545E3C13803A`，iOS 27，开发者模式已开。另一台局域网的 iPhone 16 不要用。
- 签名：`DEVELOPMENT_TEAM=TGAP3L39D8`，`CODE_SIGN_STYLE=Automatic`，**不加** `-allowProvisioningUpdates`（团队通配描述文件已包含这台机和 `TGAP3L39D8.*` 钥匙串组，不在账号上新注册任何东西）。
- **ssh 里 codesign 会报 `errSecInternalComponent`**（登录钥匙串在 ssh 会话里是锁的）。不要用密码解锁；在桌面会话里跑：写脚本（`cd ios && xcodegen generate -q && xcodebuild -project RSiOS.xcodeproj -scheme RSiOS -sdk iphoneos -configuration Debug -destination 'id=<UDID>' -derivedDataPath /tmp/rsios-dev DEVELOPMENT_TEAM=TGAP3L39D8 CODE_SIGN_STYLE=Automatic build`），再 `osascript -e 'tell application "Terminal" to do script "bash <脚本>; exit"'`，轮询完成标记文件。
- 安装 / 启动 / 截屏：`xcrun devicectl device install app | process launch | capture screenshot --device <UDID> ...`。没有触控输入：点按要请用户来。
- 节点：Windows `yzwdmsnlmm`（int3 8262043 在跑），iPhone 已配对设备名 `iphone-15-pro`；iPhone 要开着 Tailscale。
- 首次运行会弹系统麦克风权限框，请用户点「允许」。

## 通用规则
- 提交身份 `yumei <yumei@localhost>`；只推 `lane/*`，不推 master / main，不 force；推送用 Windows 上的 `bash C:\Users\yzwdm\rs-push\push_lanes.sh`（MBA 没有 GitHub 凭据）。
- 注释用中文，写清「为什么」；不加卡片以外的功能。
- 每条改动先写会失败的自测再修；`SampleHandler` / 音频采集这类写不成自测的部分，在 PR 里写清真机怎么验证的。
- 广播模式是已经在真机上跑通的功能：抽取引擎后必须先在真机上回归广播，再做麦克风。

## 2026-10-22 通宵修复
- **dad941d** 节点握手阶段也受 15 秒 ready 截止时间约束：看门狗从 `resume()` 就启动，Tailscale 对端离线时也能以 1001/1006 收尾，自动档因此能收到 `.nodeFailed` 并回退到本机。
- **20d306f** 新增 `EngineRunGate`：节点建连、重连、事件、close、loading 都核对建会话时的轮次代号，旧回调不能跨轮次复活；本机档不碰节点，启动状态行写实际读到的 `started mode=<档位>`。
- **2361019** 本机失败码经 `LocalEngineStatus` 映射成主界面红字提示；主 App 在 `local` / `auto` 档开始麦克风或准备本机资源时申请一次语音识别授权，被拒只提示、不阻止麦克风。
