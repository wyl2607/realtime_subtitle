import AVFoundation
import CoreAudio
import Foundation

// Core Audio 调用的共用小工具：统一把 OSStatus 转成带人话说明的 Error。

// kAudioObjectSystemObject 在 Swift 里被导入为 Int32，而 AudioObjectID 是 UInt32，需要显式转换。
let systemObject = AudioObjectID(kAudioObjectSystemObject)

// AVAudioPCMBuffer 没有声明 Sendable，而 AsyncStream.Continuation.yield 要求可跨线程转移。
// 每个缓冲都是回调里新建的，yield 之后生产方不再碰它，所以这里的声明是安全的。
extension AVAudioPCMBuffer: @unchecked Sendable {}

struct CaptureError: Error, CustomStringConvertible {
    let message: String
    var description: String { message }
}

// AudioHardwareBase.h 里的权限相关错误码（'nope' / 'unop'）。Swift 侧的常量名导入不稳定，直接用数值。
private let permissionLikeStatuses: Set<OSStatus> = [0x6E6F_7065, 0x756E_6F70]

func fourCharCode(_ status: OSStatus) -> String {
    let u = UInt32(bitPattern: status)
    let bytes = [24, 16, 8, 0].map { UInt8((u >> $0) & 0xFF) }
    guard bytes.allSatisfy({ $0 >= 0x20 && $0 < 0x7F }) else { return "\(status)" }
    return String(bytes.map { Character(UnicodeScalar($0)) })
}

func checkOSStatus(_ status: OSStatus, _ action: String) throws {
    guard status != noErr else { return }
    var message = "\(action) 失败：OSStatus \(status)（'\(fourCharCode(status))'）"
    // 没有系统音频录制权限时 Core Audio 通常返回这两个码，而不是明确的权限错误
    if permissionLikeStatuses.contains(status) {
        message += "。多半是没有“系统音频录制”权限：到 系统设置→隐私与安全性→屏幕与系统音频录制 里允许运行本程序的终端或应用"
    }
    throw CaptureError(message: message)
}

/// 同步读取一个标量或 CF 对象类型的属性。CFString 通过 inout 写入，Swift 会按 Create 规则在作用域结束时释放。
func audioProperty<T>(
    _ object: AudioObjectID,
    _ selector: AudioObjectPropertySelector,
    _ action: String,
    initial: T,
    qualifier: (pointer: UnsafeRawPointer, size: UInt32)? = nil
) throws -> T {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size = UInt32(MemoryLayout<T>.size)
    var value = initial
    let status: OSStatus
    if let qualifier {
        status = AudioObjectGetPropertyData(object, &address, qualifier.size, qualifier.pointer, &size, &value)
    } else {
        status = AudioObjectGetPropertyData(object, &address, 0, nil, &size, &value)
    }
    try checkOSStatus(status, action)
    return value
}

/// 读取返回 AudioObjectID 数组的属性（例如系统的设备列表）。
func audioObjectIDs(_ object: AudioObjectID, _ selector: AudioObjectPropertySelector, _ action: String) throws -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    try checkOSStatus(AudioObjectGetPropertyDataSize(object, &address, 0, nil, &size), "\(action)（读取大小）")
    var ids = [AudioObjectID](repeating: kAudioObjectUnknown, count: Int(size) / MemoryLayout<AudioObjectID>.size)
    try checkOSStatus(AudioObjectGetPropertyData(object, &address, 0, nil, &size, &ids), action)
    return ids
}

/// 往 stderr 打印警告。stop() 不能抛错，清理阶段的失败只能走这里。
func warn(_ message: String) {
    FileHandle.standardError.write(Data("⚠️  \(message)\n".utf8))
}
