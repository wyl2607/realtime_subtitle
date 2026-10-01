import CoreAudio
import CoreMedia
import Darwin
import Foundation
@preconcurrency import ScreenCaptureKit

// 协议约定：0 正常关闭，2 权限拒绝，1 其它错误。stdout 只能出现协议头和 PCM。
private func errorCode(_ error: Error) -> Int32 {
    let nsError = error as NSError
    return nsError.domain == SCStreamErrorDomain && nsError.code == SCStreamError.Code.userDeclined.rawValue ? 2 : 1
}

private func logError(_ message: String) {
    FileHandle.standardError.write(Data((message + "\n").utf8))
}

private enum TapError: Error {
    case noDisplay, noMatchingApplications, invalidPCM, writeFailed(Int32)
}

// 只有串行 outputQueue 会访问 PCM 格式状态；取消标记用锁保护，供信号处理队列读取。
private final class AudioTap: NSObject, SCStreamOutput, SCStreamDelegate, @unchecked Sendable {
    private let finish: @Sendable (Int32) -> Void
    private let lock = NSLock()
    private var cancelled = false
    private var sentHeader = false
    private var channels = 0
    let outputQueue = DispatchQueue(label: "sc-audio-tap.pcm")

    init(finish: @escaping @Sendable (Int32) -> Void) {
        self.finish = finish
    }

    func prepare() throws {
        // 先写约定格式，静音时 Python 也能完成启动握手；实际回调必须保持该格式。
        try outputQueue.sync {
            channels = 2
            try writeAll(Data("{\"sample_rate\":48000,\"channels\":2,\"format\":\"f32le\"}\n".utf8))
            sentHeader = true
        }
    }

    func cancel() {
        lock.lock()
        cancelled = true
        lock.unlock()
    }

    private var isCancelled: Bool {
        lock.lock()
        defer { lock.unlock() }
        return cancelled
    }

    private func writeAll(_ data: Data) throws {
        try data.withUnsafeBytes { bytes in
            var offset = 0
            while offset < bytes.count && !isCancelled {
                let written = Darwin.write(STDOUT_FILENO, bytes.baseAddress!.advanced(by: offset), bytes.count - offset)
                if written > 0 {
                    offset += written
                } else if written < 0 && errno == EINTR {
                    continue
                } else if written < 0 && (errno == EAGAIN || errno == EWOULDBLOCK) {
                    // ☠️ stdout 没人读时 write 会卡死，非阻塞 + 有界 poll 才能响应 SIGTERM。
                    var descriptor = pollfd(fd: STDOUT_FILENO, events: Int16(POLLOUT), revents: 0)
                    _ = poll(&descriptor, 1, 100)
                    if descriptor.revents & Int16(POLLERR | POLLHUP | POLLNVAL) != 0 {
                        finish(0)
                        return
                    }
                } else if written < 0 && errno == EPIPE {
                    finish(0)
                    return
                } else {
                    throw TapError.writeFailed(errno)
                }
            }
        }
    }

    func stream(_ stream: SCStream, didStopWithError error: Error) {
        guard !isCancelled else { return }
        logError("ScreenCaptureKit: \(error)")
        finish(errorCode(error))
    }

    func stream(_ stream: SCStream, didOutputSampleBuffer sample: CMSampleBuffer, of type: SCStreamOutputType) {
        guard type == .audio, sample.isValid, !isCancelled, sample.numSamples > 0 else { return }
        do {
            guard let description = sample.formatDescription,
                  let pointer = CMAudioFormatDescriptionGetStreamBasicDescription(description) else {
                throw TapError.invalidPCM
            }
            let format = pointer.pointee
            guard format.mFormatID == kAudioFormatLinearPCM,
                  format.mFormatFlags & kAudioFormatFlagIsFloat != 0,
                  format.mFormatFlags & kAudioFormatFlagIsBigEndian == 0,
                  format.mBitsPerChannel == 32, format.mSampleRate == 48000,
                  format.mChannelsPerFrame > 0 else { throw TapError.invalidPCM }
            let count = Int(format.mChannelsPerFrame)
            if !sentHeader {
                channels = count
                try writeAll(Data("{\"sample_rate\":48000,\"channels\":\(count),\"format\":\"f32le\"}\n".utf8))
                sentHeader = true
            }
            guard count == channels else { throw TapError.invalidPCM }
            let frames = sample.numSamples
            try sample.withAudioBufferList { buffers, _ in
                let planar = format.mFormatFlags & kAudioFormatFlagIsNonInterleaved != 0
                if planar {
                    // ☠️ ScreenCaptureKit 通常给非交错浮点平面，不能把第一个声道当 stereo 写出。
                    guard buffers.count == count else { throw TapError.invalidPCM }
                    var interleaved = [Float](repeating: 0, count: frames * count)
                    for channel in 0..<count {
                        guard let data = buffers[channel].mData,
                              Int(buffers[channel].mDataByteSize) >= frames * 4 else { throw TapError.invalidPCM }
                        let samples = data.assumingMemoryBound(to: Float.self)
                        for frame in 0..<frames {
                            interleaved[frame * count + channel] = samples[frame]
                        }
                    }
                    try interleaved.withUnsafeBytes { try writeAll(Data($0)) }
                } else {
                    guard buffers.count == 1, let data = buffers[0].mData,
                          Int(buffers[0].mDataByteSize) >= frames * count * 4 else { throw TapError.invalidPCM }
                    try writeAll(Data(bytes: data, count: frames * count * 4))
                }
            }
        } catch {
            logError("PCM 输出失败: \(error)")
            finish(1)
        }
    }
}

@MainActor
private final class CaptureSession {
    private var stream: SCStream?
    private var tap: AudioTap?
    private var sources: [any DispatchSourceProtocol] = []
    private var continuation: CheckedContinuation<Int32, Never>?
    private var finished = false
    private var startupTimeout: Task<Void, Never>?

    func finish(_ code: Int32) async {
        guard !finished else { return }
        finished = true
        tap?.cancel()
        startupTimeout?.cancel()
        for source in sources { source.cancel() }
        // 给 ScreenCaptureKit 一次正常停止机会，系统服务失去响应时仍保证有界退出。
        let fallback = Task { @MainActor in
            try? await Task.sleep(for: .seconds(2))
            if !Task.isCancelled { self.complete(code) }
        }
        if let stream { try? await stream.stopCapture() }
        fallback.cancel()
        complete(code)
    }

    private func complete(_ code: Int32) {
        continuation?.resume(returning: code)
        continuation = nil
    }

    func run(bundleIDs: Set<String>) async -> Int32 {
        await withCheckedContinuation { continuation in
            self.continuation = continuation
            let tap = AudioTap { code in Task { await self.finish(code) } }
            self.tap = tap
            for number in [SIGTERM, SIGINT] {
                signal(number, SIG_IGN)
                let source = DispatchSource.makeSignalSource(signal: number, queue: .main)
                source.setEventHandler { Task { await self.finish(0) } }
                source.resume()
                sources.append(source)
            }
            // 即使静音没 PCM，管道下游关闭也应退出，不能等下一次音频回调才发现。
            let timer = DispatchSource.makeTimerSource(queue: .main)
            timer.schedule(deadline: .now(), repeating: .milliseconds(100))
            timer.setEventHandler {
                var descriptor = pollfd(fd: STDOUT_FILENO, events: 0, revents: 0)
                _ = poll(&descriptor, 1, 0)
                if descriptor.revents & Int16(POLLERR | POLLHUP | POLLNVAL) != 0 {
                    Task { await self.finish(0) }
                }
            }
            timer.resume()
            sources.append(timer)
            // ☠️ SSH/受限沙箱里系统服务可能不回调 SCShareableContent 的 continuation。
            // 不把这种挂起误报为权限拒绝；超时归入普通错误，Python 仍能退避重试。
            startupTimeout = Task { @MainActor in
                try? await Task.sleep(for: .seconds(20))
                if !Task.isCancelled {
                    logError("ScreenCaptureKit 启动超时：请在已登录的 macOS 图形会话中运行")
                    await self.finish(1)
                }
            }
            Task {
                do {
                    let content = try await SCShareableContent.excludingDesktopWindows(false, onScreenWindowsOnly: false)
                    guard !finished else { return }
                    guard let display = content.displays.first else { throw TapError.noDisplay }
                    let filter: SCContentFilter
                    if bundleIDs.isEmpty {
                        filter = SCContentFilter(display: display, excludingApplications: [], exceptingWindows: [])
                    } else {
                        let apps = content.applications.filter { bundleIDs.contains($0.bundleIdentifier) }
                        guard !apps.isEmpty else { throw TapError.noMatchingApplications }
                        filter = SCContentFilter(display: display, including: apps, exceptingWindows: [])
                    }
                    let configuration = SCStreamConfiguration()
                    configuration.capturesAudio = true
                    configuration.excludesCurrentProcessAudio = true
                    configuration.sampleRate = 48000
                    configuration.channelCount = 2
                    configuration.width = 2
                    configuration.height = 2
                    configuration.minimumFrameInterval = CMTime(seconds: 1, preferredTimescale: 1)
                    configuration.queueDepth = 3
                    configuration.showsCursor = false
                    let stream = SCStream(filter: filter, configuration: configuration, delegate: tap)
                    self.stream = stream
                    try stream.addStreamOutput(tap, type: .audio, sampleHandlerQueue: tap.outputQueue)
                    // 忽略视频回调；注册 screen output 避免系统反复报告缺少输出消费者。
                    try stream.addStreamOutput(tap, type: .screen, sampleHandlerQueue: tap.outputQueue)
                    try tap.prepare()
                    try await stream.startCapture()
                    startupTimeout?.cancel()
                } catch {
                    logError("无法启动系统音频捕获: \(error)")
                    await finish(errorCode(error))
                }
            }
        }
    }
}

@main
private struct ScAudioTap {
    static func main() async {
        let arguments = Array(CommandLine.arguments.dropFirst())
        var bundleIDs = Set<String>()
        if !arguments.isEmpty {
            guard arguments.count == 2, arguments[0] == "--bundle-ids" else {
                logError("用法: sc-audio-tap [--bundle-ids a,b]")
                exit(1)
            }
            bundleIDs = Set(arguments[1].split(separator: ",").map { $0.trimmingCharacters(in: .whitespaces) }.filter { !$0.isEmpty })
        }
        signal(SIGPIPE, SIG_IGN)
        let flags = fcntl(STDOUT_FILENO, F_GETFL)
        guard flags >= 0, fcntl(STDOUT_FILENO, F_SETFL, flags | O_NONBLOCK) == 0 else {
            logError("无法设置 stdout 非阻塞模式")
            exit(1)
        }
        let code = await CaptureSession().run(bundleIDs: bundleIDs)
        exit(code)
    }
}
