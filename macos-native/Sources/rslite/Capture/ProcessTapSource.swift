import AVFoundation
import CoreAudio
import Foundation

/// 用 Core Audio Process Tap 抓取系统正在播放的声音（不需要 BlackHole）。
///
/// 配方照搬 realtime_subtitle/capture/macos_tap.py 在 M2/macOS 27 上试通的那一套：
/// tap 必须是私有的，并且只能挂进**私有**聚合设备；聚合设备以默认输出设备为主子设备
/// 当时钟并开漂移补偿。非私有聚合设备建得出来，但输入流是 0。
/// tap 不静音，用户照常听得到声音。
///
/// 线程约定：start()/stop() 只能从同一个线程调用（调用方是流水线的主线程）。
/// IOProc 回调在 Core Audio 实时线程上运行，只读取 start() 时捕获的局部常量。
final class ProcessTapSource: AudioSource {
    // 聚合设备的显示名，同时用于启动时识别上次崩溃残留的设备。
    private static let aggregateName = "rslite Process Tap"

    private var tapID: AudioObjectID = kAudioObjectUnknown
    private var aggregateID: AudioObjectID = kAudioObjectUnknown
    private var ioProcID: AudioDeviceIOProcID?
    private var ioStarted = false
    private var continuation: AsyncStream<AVAudioPCMBuffer>.Continuation?
    private var _format: AVAudioFormat?

    var format: AVAudioFormat {
        guard let _format else { preconditionFailure("ProcessTapSource.format 只在 start() 之后有效") }
        return _format
    }

    func start() throws -> AsyncStream<AVAudioPCMBuffer> {
        precondition(continuation == nil, "ProcessTapSource 已经 start，需要先 stop")
        do {
            return try startCapture()
        } catch {
            // 半途失败时把已创建的 Core Audio 对象全部收回，否则会残留在用户的“音频 MIDI 设置”里
            teardown()
            throw error
        }
    }

    func stop() {
        teardown()
    }

    private func startCapture() throws -> AsyncStream<AVAudioPCMBuffer> {
        try destroyStaleAggregates()

        let excluded = try ownProcessObjectIDs()
        // 排除本进程：字幕程序自己的声音不回灌进识别，否则会出现自己识别自己的死循环
        let desc = CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
        // muteBehavior 保持头文件默认的 CATapUnmuted：用户照常听得到
        desc.isPrivate = true

        var newTap: AudioObjectID = kAudioObjectUnknown
        try checkOSStatus(AudioHardwareCreateProcessTap(desc, &newTap), "创建系统音频 tap")
        tapID = newTap

        let tapUID: CFString = try audioProperty(tapID, kAudioTapPropertyUID, "读取 tap UID", initial: "" as CFString)
        var asbd: AudioStreamBasicDescription = try audioProperty(
            tapID, kAudioTapPropertyFormat, "读取 tap 音频格式", initial: AudioStreamBasicDescription())
        guard let fmt = AVAudioFormat(streamDescription: &asbd) else {
            throw CaptureError(message: "系统音频 tap 给出的格式无法解析（采样率 \(asbd.mSampleRate)，声道 \(asbd.mChannelsPerFrame)）")
        }

        let outputUID = try defaultOutputDeviceUID()
        let dict: [String: Any] = [
            kAudioAggregateDeviceNameKey: Self.aggregateName,
            kAudioAggregateDeviceUIDKey: "com.rslite.tap.\(UUID().uuidString)",
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [[
                kAudioSubTapUIDKey: tapUID as String,
                kAudioSubTapDriftCompensationKey: true,
            ]],
            kAudioAggregateDeviceTapAutoStartKey: true,
        ]
        var newAggregate: AudioObjectID = kAudioObjectUnknown
        try checkOSStatus(
            AudioHardwareCreateAggregateDevice(dict as CFDictionary, &newAggregate),
            "创建私有聚合设备")
        aggregateID = newAggregate

        let (stream, cont) = AsyncStream<AVAudioPCMBuffer>.makeStream()
        continuation = cont
        _format = fmt

        // 实时线程上只做拷贝和 yield；按值捕获 fmt/cont，不捕获 self
        let bytesPerFrame = Int(asbd.mBytesPerFrame)
        var procID: AudioDeviceIOProcID?
        try checkOSStatus(
            AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, nil) { _, inInputData, _, _, _ in
                guard let buffer = Self.copy(inInputData, format: fmt, bytesPerFrame: bytesPerFrame) else { return }
                cont.yield(buffer)
            },
            "注册音频读取回调")
        ioProcID = procID

        try checkOSStatus(AudioDeviceStart(aggregateID, procID), "启动系统音频读取")
        ioStarted = true
        return stream
    }

    /// 按“先停 IO、再销毁对象”的顺序拆除，每一步都尽量继续，保证不留残余。
    /// stop() 不能抛错，失败只打印警告。
    private func teardown() {
        if ioStarted, let procID = ioProcID {
            warnIfFailed(AudioDeviceStop(aggregateID, procID), "停止系统音频读取")
        }
        ioStarted = false
        if let procID = ioProcID {
            warnIfFailed(AudioDeviceDestroyIOProcID(aggregateID, procID), "注销音频读取回调")
        }
        ioProcID = nil
        if aggregateID != kAudioObjectUnknown {
            warnIfFailed(AudioHardwareDestroyAggregateDevice(aggregateID), "删除私有聚合设备")
        }
        aggregateID = kAudioObjectUnknown
        if tapID != kAudioObjectUnknown {
            warnIfFailed(AudioHardwareDestroyProcessTap(tapID), "删除系统音频 tap")
        }
        tapID = kAudioObjectUnknown
        continuation?.finish()
        continuation = nil
    }

    private func warnIfFailed(_ status: OSStatus, _ action: String) {
        if status != noErr {
            warn("\(action) 失败：OSStatus \(status)（'\(fourCharCode(status))'）")
        }
    }

    /// 上次异常退出残留的同名聚合设备会一直挂在系统里，启动前先清掉。
    private func destroyStaleAggregates() throws {
        for id in try audioObjectIDs(systemObject, kAudioHardwarePropertyDevices, "枚举音频设备") {
            // 设备可能在枚举和读取之间消失，读不到就跳过
            guard let cls = try? audioProperty(id, kAudioObjectPropertyClass, "读取设备类别", initial: AudioClassID(0)),
                  cls == kAudioAggregateDeviceClassID,
                  let name = try? audioProperty(id, kAudioObjectPropertyName, "读取设备名", initial: "" as CFString),
                  (name as String) == Self.aggregateName
            else { continue }
            warn("清理上次残留的聚合设备：\(Self.aggregateName)")
            try checkOSStatus(AudioHardwareDestroyAggregateDevice(id), "删除残留聚合设备")
        }
    }

    /// 本进程在 Core Audio 里的 AudioObjectID。本进程还没打开过音频时可能查不到（返回 0），
    /// 那种情况下 tap 不排除任何进程，会把本进程的提示音也采进来，只打印警告。
    private func ownProcessObjectIDs() throws -> [AudioObjectID] {
        var pid = getpid()
        let obj: AudioObjectID = try withUnsafeBytes(of: &pid) { raw in
            try audioProperty(
                systemObject,
                kAudioHardwarePropertyTranslatePIDToProcessObject,
                "查询本进程的音频对象",
                initial: kAudioObjectUnknown,
                qualifier: (raw.baseAddress!, UInt32(raw.count)))
        }
        guard obj != kAudioObjectUnknown else {
            warn("无法解析本进程的 AudioObjectID，系统音频 tap 不会排除本进程的声音")
            return []
        }
        return [obj]
    }

    private func defaultOutputDeviceUID() throws -> String {
        let device: AudioDeviceID = try audioProperty(
            systemObject, kAudioHardwarePropertyDefaultOutputDevice,
            "读取默认输出设备", initial: kAudioObjectUnknown)
        let uid: CFString = try audioProperty(
            device, kAudioDevicePropertyDeviceUID, "读取默认输出设备 UID", initial: "" as CFString)
        return uid as String
    }

    /// 把 IOProc 收到的 AudioBufferList 拷进新的 AVAudioPCMBuffer。
    /// 在实时线程上运行，所以不做任何日志或阻塞操作；结构对不上时直接丢弃这一块。
    private static func copy(
        _ input: UnsafePointer<AudioBufferList>,
        format: AVAudioFormat,
        bytesPerFrame: Int
    ) -> AVAudioPCMBuffer? {
        guard bytesPerFrame > 0 else { return nil }
        let src = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: input))
        guard let first = src.first else { return nil }
        let frames = Int(first.mDataByteSize) / bytesPerFrame
        guard frames > 0,
              let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(frames))
        else { return nil }
        // 必须先设 frameLength：新建缓冲的 mDataByteSize 在此之前是 0，按它做容量检查会把所有块都丢掉
        buffer.frameLength = AVAudioFrameCount(frames)
        let capacityBytes = Int(buffer.frameCapacity) * bytesPerFrame
        let dst = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
        guard dst.count == src.count else { return nil }
        for i in 0..<src.count {
            let n = Int(src[i].mDataByteSize)
            guard n <= capacityBytes, let from = src[i].mData, let to = dst[i].mData else { return nil }
            memcpy(to, from, n)
        }
        return buffer
    }
}
