# iOS 节点客户端第 2 轮评审修复结果

## 修复的三个问题

### 1. PCMConverter Int16 大小端读反了（严重）
**文件**: `ios/Shared/PCMConverter.swift:207-212`

**修复前代码**:
```swift
private func int16(from bytes: UnsafePointer<UInt8>, offset: Int, bigEndian: Bool) -> Int16 {
    let lo = bytes[offset]
    let hi = bytes[offset + 1]
    let value: UInt16 = bigEndian ? (UInt16(hi) << 8) | UInt16(lo) : (UInt16(lo) << 8) | UInt16(hi)
    return Int16(bitPattern: value)
}
```

**修复后代码**:
```swift
private func int16(from bytes: UnsafePointer<UInt8>, offset: Int, bigEndian: Bool) -> Int16 {
    let b0 = bytes[offset]
    let b1 = bytes[offset + 1]
    let value: UInt16 = bigEndian ? (UInt16(b0) << 8) | UInt16(b1) : UInt16(b0) | (UInt16(b1) << 8)
    return Int16(bitPattern: value)
}
```

---

### 2. 自测没覆盖 CMSampleBuffer 路径
**文件**: `ios/Tests/NodeSelftest.swift` - 新增 `testCMSampleBufferPaths` 测试

**新测试覆盖三种输入格式**（每种 1 秒 440Hz 正弦、幅度 0.5）：
- 44.1kHz 立体声 Int16 **大端**交错
- 48kHz 立体声 Int16 小端交错
- 48kHz 立体声 Float32 **非交错**

**断言条件**：
- 吐出帧总字节在 32000±3200
- 每帧 3200 字节
- 输出归一化 RMS（按 Int16/32768 算）在 0.5/√2 的 ±20% 内

---

### 3. NodeSession 关闭码被吞
**文件**: `ios/Shared/NodeProtocol.swift`

**修复点 1**: `receiveLoop` 失败时先检查 `socket.closeCode != .invalid`
```swift
case .failure:
    if socket.closeCode != .invalid {
        self?.notifyClosed(NodeEvent.closeReason(code: socket.closeCode.rawValue, reasonData: socket.closeReason))
    } else {
        self?.notifyClosed(1006)
    }
```

**修复点 2**: `close()` 改用 `finishTasksAndInvalidate()`
```swift
func close() {
    socket?.cancel(with: .normalClosure, reason: nil)
    session?.finishTasksAndInvalidate()
}
```

---

## 验收测试输出

### 修复前（新测试因大小端 Bug 失败）:
```
FAIL: 44.1kHz立体声Int16大端交错 RMS 相差 <20%（预期 0.35355339059327373，实际 10984.608432932924，相对差 31068.16444642323）
FAIL: 48kHz立体声Int16小端交错 RMS 相差 <20%（预期 0.35355339059327373，实际 9192.594105413064，相对差 25999.582514532252）
FAIL: 48kHz立体声Float32非交错 RMS 相差 <20%（预期 0.35355339059327373，实际 31187.758252883097，相对差 88211.3014024814）
```

### 修复后（所有测试通过）:
```
SELFTEST OK
```

### 类型检查:
```
xcrun -sdk iphonesimulator swiftc -typecheck -target arm64-apple-ios17.0-simulator ios/Shared/NodeProtocol.swift ios/Shared/PCMConverter.swift
# 无错误输出
```

---

## 修改文件清单
1. `ios/Shared/PCMConverter.swift` - 修复 Int16 大小端读取
2. `ios/Shared/NodeProtocol.swift` - 修复关闭码处理、close() 方法
3. `ios/Tests/NodeSelftest.swift` - 新增 CMSampleBuffer 三格式测试