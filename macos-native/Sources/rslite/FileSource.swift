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
                    let clock = ContinuousClock()
                    let startedAt = clock.now
                    var audioEndSeconds = 0.0
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
                        audioEndSeconds += seconds

                        // 回放锚定在首块样本的墙钟：音频块要等它所代表的样本真正过去
                        // 才交付。逐块相对 sleep 会把读盘、调度和下游负载的额外耗时
                        // 永久累计，让样本时钟越来越落后于墙钟。
                        let wallElapsed = Self.seconds(from: startedAt.duration(to: clock.now))
                        let delay = Self.realtimeDelay(audioEndSeconds: audioEndSeconds, wallElapsed: wallElapsed)
                        if delay > 0 {
                            try await Task.sleep(for: .seconds(delay))
                        }
                        guard !self.isStopped else { break }
                        continuation.yield(buffer)
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

    private static func seconds(from duration: Duration) -> Double {
        Double(duration.components.seconds) + Double(duration.components.attoseconds) / 1e18
    }

    static func realtimeDelay(audioEndSeconds: Double, wallElapsed: Double) -> Double {
        max(0, audioEndSeconds - wallElapsed)
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }
        check(abs(realtimeDelay(audioEndSeconds: 0.2, wallElapsed: 0.104) - 0.096) < 0.000_001,
              "回放应睡到绝对样本截止时间，而非再睡完整块长")
        check(realtimeDelay(audioEndSeconds: 0.1, wallElapsed: 0.104) == 0,
              "落后时应立即追赶，不能继续累积滞后")
        return failures
    }
}
