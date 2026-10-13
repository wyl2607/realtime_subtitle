# iOS Spike — Build Instructions

## 生成 Xcode 工程

```bash
cd ios
xcodegen generate
```

会生成 `RSiOS.xcodeproj`。

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

- `DEVELOPMENT_TEAM` 为免费 Personal Team ID（仅示例，按实际填写）。
- 免费账号不支持 App Group，本工程只用 `keychain-access-groups` 共享数据。

## 目录结构

```
ios/
├── project.yml              # xcodegen 规格
├── RSiOS/                   # 主 App
│   ├── Info.plist
│   ├── RSiOS.entitlements
│   ├── RSiOSApp.swift
│   └── ContentView.swift    # UI + PiP + Keychain 监控 + Darwin 通知
├── RSBroadcast/             # Broadcast Upload Extension
│   ├── Info.plist
│   ├── RSBroadcast.entitlements
│   └── SampleHandler.swift  # 音频 RMS + Keychain 写入 + Darwin 通知
├── Shared/
│   └── SharedKeychain.swift # 共享 Keychain 读写（两 target 编译进去）
├── README.md
└── .gitignore
```

## 关键点

- 部署目标 iOS 17.0，Swift 5.0（避免 Swift 6 严格并发）。
- 扩展类型 `app-extension`，`NSExtensionPointIdentifier = com.apple.broadcast-services-upload`。
- `RPBroadcastProcessMode = RPBroadcastProcessModeSampleBuffer`。
- 共享 Keychain access group：`$(AppIdentifierPrefix)com.wyl2607.rsios.shared`。
- 主 App `UIBackgroundModes: [audio]` 以支持后台 PiP。
- 扩展只处理 `.audioApp`，计算 RMS，每秒写一次 Keychain + 发 Darwin 通知。
- 主 App 每 0.5 秒读 Keychain、刷新 PiP 字幕帧（1280×360 BGRA → CMSampleBuffer → AVSampleBufferDisplayLayer）。