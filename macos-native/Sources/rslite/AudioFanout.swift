@preconcurrency import AVFoundation
import Foundation

/// 带统一样本时钟的一段音频。时钟只按「送出去的样本数」推进，不看墙上时间：
/// 本机 B 和节点客户端拿到的是同一份 startSeconds，两边的时间戳才能直接比较（RFC P4）。
struct TimedAudio: @unchecked Sendable {
    let buffer: AVAudioPCMBuffer
    /// 这段音频在统一样本时钟上的起点（秒）。
    let startSeconds: Double

    var duration: Double {
        Double(buffer.frameLength) / buffer.format.sampleRate
    }
}

/// 一路采集同时分给多个消费者（本机 B、NodeClient），共用同一个样本时钟。
///
/// 为什么每个消费者各有一个有界 AsyncStream：消费慢的一路（比如网络卡住的节点）
/// 不能拖住另一路，也不能让内存无限涨。满了就丢**最旧**的缓冲（bufferingNewest），
/// 实时字幕宁可丢老音频也要保证新音频及时被处理。
///
/// 时钟跨 stop()/start() 连续：暂停期间没有样本，时钟就不走；恢复后接着往下数，
/// 这样节点和本机在暂停前后看到的时间仍然一致。
final class AudioFanout: @unchecked Sendable {
    /// 默认每路缓冲上限（个数，不是秒数）。每个缓冲约 10–100ms，足够吸收几秒抖动。
    static let defaultCapacity = 64

    private let makeSource: @Sendable () throws -> AudioSource
    private let lock = NSLock()
    private var source: AudioSource?
    private var pump: Task<Void, Never>?
    /// 轮次号：每次 start() +1。订阅者记录自己归属的轮次（订阅时的 generation + 1，即紧随其后的那次 start）；pump 自然退出时只结束
    /// 归属本轮及更早的订阅者，不误伤下一轮 start 前新订阅的。
    private var generation = 0
    private var subscriberGeneration: [UUID: Int] = [:]
    private var subscribers: [UUID: AsyncStream<TimedAudio>.Continuation] = [:]
    /// 之前所有运行累计的时长（秒）。
    private var elapsedBeforeRun = 0.0
    /// 本次运行已送出的秒数。
    private var runSeconds = 0.0
    private var runFormat: AVAudioFormat?

    init(makeSource: @escaping @Sendable () throws -> AudioSource) {
        self.makeSource = makeSource
    }

    convenience init(sourceSpec: String) {
        self.init(makeSource: { try makeAudioSource(sourceSpec) })
    }

    /// start() 之后才有效。
    var format: AVAudioFormat? {
        lock.lock()
        defer { lock.unlock() }
        return runFormat
    }

    /// 当前统一样本时钟读数（秒）。
    var clockSeconds: Double {
        lock.lock()
        defer { lock.unlock() }
        return elapsedBeforeRun + runSeconds
    }

    /// 必须在 start() 之前订阅，否则会漏掉开头的音频；stop() 会结束并清掉所有订阅，
    /// 下一轮 start() 前需要重新订阅。
    func subscribe(capacity: Int = AudioFanout.defaultCapacity) -> AsyncStream<TimedAudio> {
        let id = UUID()
        var continuation: AsyncStream<TimedAudio>.Continuation!
        let stream = AsyncStream<TimedAudio>(bufferingPolicy: .bufferingNewest(max(1, capacity))) {
            continuation = $0
        }
        continuation.onTermination = { [weak self] _ in
            self?.removeSubscriber(id)
        }
        lock.lock()
        subscribers[id] = continuation
        subscriberGeneration[id] = generation + 1 // 归属于下一次 start() 的那一轮
        lock.unlock()
        return stream
    }

    func start() throws {
        let source = try makeSource()
        let stream = try source.start()
        lock.lock()
        self.source = source
        runFormat = source.format
        // 来源自然结束（文件放完）时不会走 stop()，在这里把上一轮的时长并进时钟
        elapsedBeforeRun += runSeconds
        runSeconds = 0
        generation += 1
        let myGeneration = generation
        // 在同一次持锁内创建并赋值 pump：stop() 也要拿这把锁，
        // 所以它要么看到完整的 source+pump 一起 cancel，要么在 start 之前完成（此时 source 尚未设置）。
        pump = Task.detached(priority: .userInitiated) { [weak self, stream] in
            for await buffer in stream {
                guard let self else {
                    return
                }
                self.dispatch(buffer)
            }
            // 被 cancel 退出说明是 stop() 在收尾，清理交给 stop；自然结束才在这里清
            guard !Task.isCancelled else {
                return
            }
            self?.finishSubscribers(upToGeneration: myGeneration)
        }
        lock.unlock()
    }

    func stop() {
        lock.lock()
        let source = self.source
        let pump = self.pump
        self.source = nil
        self.pump = nil
        elapsedBeforeRun += runSeconds
        runSeconds = 0
        lock.unlock()

        source?.stop()
        pump?.cancel()
        finishSubscribers(upToGeneration: nil)
    }

    private func dispatch(_ buffer: AVAudioPCMBuffer) {
        let seconds = Double(buffer.frameLength) / buffer.format.sampleRate
        lock.lock()
        let timed = TimedAudio(buffer: buffer, startSeconds: elapsedBeforeRun + runSeconds)
        runSeconds += seconds
        let targets = Array(subscribers.values)
        lock.unlock()
        // yield 不阻塞：满了由各自的 bufferingNewest 丢最旧的，慢消费者影响不到别人
        for target in targets {
            target.yield(timed)
        }
    }

    /// generation 为 nil 结束全部；否则只结束订阅轮次 <= 该值的（即本轮 start 时已在的）。
    private func finishSubscribers(upToGeneration generation: Int?) {
        lock.lock()
        var targets: [AsyncStream<TimedAudio>.Continuation] = []
        for (id, continuation) in subscribers {
            if let generation, let g = subscriberGeneration[id], g > generation {
                continue
            }
            targets.append(continuation)
            subscribers.removeValue(forKey: id)
            subscriberGeneration.removeValue(forKey: id)
        }
        lock.unlock()
        for target in targets {
            target.finish()
        }
    }

    private func removeSubscriber(_ id: UUID) {
        lock.lock()
        subscribers.removeValue(forKey: id)
        subscriberGeneration.removeValue(forKey: id)
        lock.unlock()
    }
}
