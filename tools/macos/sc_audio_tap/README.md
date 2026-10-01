# sc-audio-tap

macOS 14+ 的 ScreenCaptureKit 系统音频助手，无额外依赖。

```sh
cd tools/macos/sc_audio_tap
swift build -c release
./.build/release/sc-audio-tap
./.build/release/sc-audio-tap --bundle-ids com.apple.Safari,org.mozilla.firefox
```

stdout 第一行是 `{"sample_rate":48000,"channels":2,"format":"f32le"}`，
随后只有交错的小端 float32 PCM；stderr 接收诊断。不要把 stdout 当文本日志。
默认抓全部应用的系统混音，排除助手自身的音频；视频仅 2×2、1 fps，回调忽略。
非空 bundle ID 列表只选择启动时正在运行的匹配应用；没有匹配则报错退出。

退出码：`0` 为 SIGTERM/SIGINT 或 stdout 关闭；`2` 为 ScreenCaptureKit 明确拒绝
录音授权；`1` 为其它错误（包括无显示器、无匹配应用、启动超时）。授权提示：
请在「系统设置 → 隐私与安全性 → 屏幕与系统录音」里允许（终端/本程序），然后重启字幕。

Python 配置读取 `MAC_AUDIO_HELPER`（可执行文件路径，空值采用上述默认位置），
以及可选的 `MAC_AUDIO_BUNDLE_IDS`（逗号分隔字符串或字符串列表）。macOS 忽略
`LOOPBACK_DEVICE_NAME`。应用接线请使用 `realtime_subtitle.capture.make_audio_capture`。

☠️ ScreenCaptureKit 常给非交错声道平面，助手会明确转成协议要求的交错 PCM。
管道会拆分任何帧/协议头，Python 必须拼接余数；双方都支持没有音频时终止。
SSH/受限沙箱的系统服务可能挂起，助手有 20 秒启动超时，Python 协议握手有 15 秒
超时。这种失败不能据此断言用户拒绝了 TCC 权限。

受限开发环境若禁止 SwiftPM 写用户缓存，可把缓存落在临时目录：

```sh
CLANG_MODULE_CACHE_PATH=/private/tmp/sc-tap-clang \
SWIFT_MODULECACHE_PATH=/private/tmp/sc-tap-modules \
swift build -c release --disable-sandbox \
  --cache-path /private/tmp/sc-tap-cache \
  --config-path /private/tmp/sc-tap-config \
  --security-path /private/tmp/sc-tap-security
```

API 依据：[Apple ScreenCaptureKit](https://developer.apple.com/documentation/screencapturekit)、
[应用级音频过滤](https://developer.apple.com/videos/play/wwdc2022/10155/)。本助手独立实现，
未复制 livecaptions 源码。
