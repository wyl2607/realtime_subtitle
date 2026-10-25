# S1 第 2 轮修改结果

## 执行时间
2026-10-13

## 验收结果
`bash ios/Tests/build-check.sh` 最终输出：
```
BUILD-CHECK OK
```

## 修改清单

### 必修 1：去掉 Shared framework target
- 删掉 `project.yml` 里的 `Shared` target（第 84-98 行）
- 删掉 RSiOS 和 RSBroadcast 两处 `- target: Shared` 依赖
- 把 `Shared` 目录作为源码同时加进 RSiOS 和 RSBroadcast 两个 target 的 `sources`，并 `excludes: ["**/Resources/**"]` 避免 Info.plist 冲突
- 删掉 `Shared/Resources/` 目录（不再需要）
- 各 Swift 文件里的 `import Shared` 已删掉（SampleHandler.swift、ContentView.swift 均已无该导入）
- `SharedKeychain` 里的 `public` 保留

### 必修 2：去掉不存在的 resources 路径与无用 build setting
- 删掉 RSiOS/Resources、RSBroadcast/Resources 的 resources 配置
- 删掉 project.yml 里无用的 `NSEXTENSION_*` / `RPBROADCAST_*` build setting（原在 RSBroadcast target 的 settings.base 里）
- 删掉两个 target 的 `FRAMEWORK_SEARCH_PATHS`（不再需要）

### 必修 3：扩展 Info.plist 修正
- 删掉 `UIBackgroundModes`
- `NSExtension` 下新增 `NSExtensionAttributes` 字典，含 `RPBroadcastProcessMode = RPBroadcastProcessModeSampleBuffer`

### 必修 4：两个 Info.plist 都加键 `AppIdentifierPrefix`
- RSiOS/Info.plist 新增 `<key>AppIdentifierPrefix</key><string>$(AppIdentifierPrefix)</string>`
- RSBroadcast/Info.plist 新增同键值

### 必修 5：扩展 RMS 越界修复
- `SampleHandler.swift`：
  - 样本总数改为 `length / (mBitsPerChannel/8)`（原 `length / mBytesPerFrame`，非交错格式下会越界）
  - 新增检测 `kAudioFormatFlagIsBigEndian`：大端 Int16 时用 `Int16(bigEndian:)` 读取
  - 状态行 fmt 改为 `int16be` / `int16le` / `float32` / `other`
  - 状态行追加 `flags=<mFormatFlags>`、`ileave=<0|1>`（`kAudioFormatFlagIsNonInterleaved` 为 0 则 ileave=1）

### 必修 6：画中画修复（最重要）
- 新增 `PiPHostView : UIViewRepresentable`，内部 `PiPHostUIView` 在 `layoutSubviews` 里把 display layer 的 frame 设为 bounds
- ContentView 里显示该 view（320×90，作为字幕预览）
- `PiPManager` 在 **init 时**就建好 layer 和 `AVPictureInPictureController`，并立刻开始每 0.5s 渲染帧（预览一直在刷）
- 「开始画中画」按钮：先判 `isPictureInPicturePossible`，为 false 时设 `lastError = "PiP 尚不可用（isPictureInPicturePossible=false）"` 并返回；否则 `startPictureInPicture()`
- 用 KVO 观察 `pipController.isPictureInPicturePossible`，界面实时显示当前值
- `stopPiP` 只调 `stopPictureInPicture()`，**不销毁** layer / controller / 不停渲染
- `pictureInPictureControllerDidStopPictureInPicture` 里**不停渲染**
- 渲染时顺序修正：`layer.status == .failed` 时**先 `flush()` 再 `enqueue()`**（原顺序反了）
- `presentationTimeStamp` 用 `CMClockGetTime(CMClockGetHostTimeClock())`，duration 用 0.5 秒

### 必修 7：主 App 界面加 App 状态行 & PiP 后台帧数
- 新增 `AppStateMonitor` 单例，监听 `UIApplication.didEnterBackgroundNotification` / `willEnterForegroundNotification`
- 界面第一行显示「App 状态：前台/后台」（前台绿、后台橙）
- PiP 第二行改为 `HH:mm:ss · 后台刷新 N 帧`（N = 进入后台后渲染的帧数，回前台清零）

## 未做事项
- 无（所有必修项均已完成并通过验收）

## 备注
- 删除了 `Shared/Resources/Info.plist` 文件（随 Resources 目录一并删除）
- `NodeSelftest.swift` 自测继续通过（SELFTEST OK）
- 模拟器构建成功，且 build-check.sh 所有检查项均通过：
  - 自测通过
  - xcodegen 生成项目通过
  - 模拟器构建通过
  - 扩展 Info.plist 含 `RPBroadcastProcessModeSampleBuffer`
  - 两个 Info.plist 含 `AppIdentifierPrefix`
  - 无 `import Shared` 残留
  - project.yml 无 `type: framework`