# 结果：iOS 节点协议 v2 客户端（协议 + 音频转换）

## 改动

新增 4 个文件，全部在本仓库 `ios/` 目录下（未动其它任何文件）：

- `ios/Shared/NodeProtocol.swift`（仅依赖 Foundation）
  - `NodeHello`：`src`/`dst` 语言码校验（`^[a-z]{2,3}(-[A-Za-z]{2,4})?$`，不合法 `init?` 返回 nil）；`json()` 产出 `{"type":"hello","v":2,"src":..,"dst":..,"sample_rate":16000,"format":"s16le"}`。
  - `NodeEvent`：`ready` / `final` / `translation` / `status` / `drained` 五种事件，`parse` 对字段缺失、类型不对、未知 `ev`、非 JSON 一律返回 nil（用 `CFGetTypeID` 区分布尔与数字，避免把 `0/1` 当布尔）。`isFatal(code:)` 认 `protocol_error`/`engine_load_failed`/`internal_error`。`closeReason(code:reasonData:)` 按 CLAUDE.md 第 7 节第 17 条把 URLSession 误报的 1005 + reason 前两字节大端还原真实码。
  - `NodeSession`：`URLSessionWebSocketTask`，`start()` 带 `Authorization: Bearer` 头连上后先发 hello 文本帧再循环 receive；`send(pcm16k:)` 发二进制帧、`flush()` 发 `{"type":"flush"}`、`close()` 发 close 1000。不自动重连、不做 TailnetPin。全程无任何日志/打印，token 与字幕正文不进输出。
- `ios/Shared/PCMConverter.swift`（Foundation + AVFoundation + AudioToolbox + CoreMedia）
  - 把任意采样率/声道/格式（Int16 大小端、Float32、交错/非交错）的 `CMSampleBuffer` 经 `AVAudioConverter` 转成 16kHz 单声道 s16le；大端 Int16 在拷贝时字节翻转成小端再交给 converter；输入格式变化时重建 converter（按 commonFormat/sampleRate/channelCount/interleaved 比对）。
  - `append(_:)` 内部攒够 100ms（3200 字节）吐一帧，返回 `[Data]`（每帧恰好 3200 字节）。
  - 另给不依赖 CMSampleBuffer 的 `convert(interleavedInt16:sampleRate:channels:) -> Data` 入口。
- `ios/Tests/NodeSelftest.swift`（`@main`，失败 `exit(1)`）
- `ios/Tests/run-selftest.sh`（`mkdir -p .tmp` → `swiftc -O` 编译 → 运行 → `trap` 删 `.tmp`）

## 测试输出

```
$ bash ios/Tests/run-selftest.sh
SELFTEST OK

$ xcrun -sdk iphonesimulator swiftc -typecheck -target arm64-apple-ios17.0-simulator ios/Shared/NodeProtocol.swift ios/Shared/PCMConverter.swift
（无输出，exit 0）
```

自测覆盖：hello JSON 字段与非法语言码；五种事件解析 + 缺字段/未知 ev/非 JSON；`isFatal`；`closeReason`（1005+busy → 1013、1005 空载荷 → 1005、1000 → 1000 等）；PCMConverter 48kHz 立体声 1 秒 440Hz 正弦与 44.1kHz 单声道的字节总数（±3200）、每帧 3200 字节、RMS 相对差 < 20%。

## 未做的事

- 未写任何 UI、未创建/改动 Xcode 工程。
- 未实现自动重连、TailnetPin（IP 钉住/网段校验）、`/v1/info` 探测与路由——按任务说明「以后做」。
- 未对 `NodeSession` 的联网行为做端到端自测（无真实节点/服务器），仅保证编译与类型检查通过；其语义照 `macos-native/Sources/rslite/NodeClient.swift` 实现。
- 未做 `git commit`。
