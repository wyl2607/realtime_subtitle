import AVFoundation
import Foundation

final class FileSource: AudioSource, @unchecked Sendable {
    private let url: URL
    private var stopped = false
    private let lock = NSLock()
    private var continuation: AsyncStream<AVAudioPCMBuffer>.Continuation?

    private(set) var format: AVAudioFormat

    init(path: String) throws {
        url = URL(fileURLWithPath: path)
        let file = try AVAudioFile(forReading: url)
        format = file.processingFormat
    }

    func start() throws -> AsyncStream<AVAudioPCMBuffer> {
        let file = try AVAudioFile(forReading: url)
        format = file.processingFormat
        setStopped(false)

        return AsyncStream { continuation in
            self.continuation = continuation
            let framesPerChunk = max(1, AVAudioFrameCount(format.sampleRate * 0.1))

            Task.detached(priority: .userInitiated) { [weak self] in
                guard let self else {
                    continuation.finish()
                    return
                }

                do {
                    while !self.isStopped {
                        guard let buffer = AVAudioPCMBuffer(
                            pcmFormat: file.processingFormat,
                            frameCapacity: framesPerChunk
                        ) else {
                            break
                        }
                        try file.read(into: buffer, frameCount: framesPerChunk)
                        if buffer.frameLength == 0 {
                            break
                        }

                        let seconds = Double(buffer.frameLength) / file.processingFormat.sampleRate
                        continuation.yield(buffer)

                        // 测试输入也按真实节拍喂，避免把离线文件跑成不真实的低延迟。
                        try await Task.sleep(for: .seconds(seconds))
                    }
                } catch {
                    // AudioSource 契约没有错误通道；文件读错时结束流，由 Pipeline 报启动错误。
                }

                continuation.finish()
            }
        }
    }

    func stop() {
        setStopped(true)
        continuation?.finish()
    }

    private var isStopped: Bool {
        lock.lock()
        defer { lock.unlock() }
        return stopped
    }

    private func setStopped(_ value: Bool) {
        lock.lock()
        stopped = value
        lock.unlock()
    }
}
