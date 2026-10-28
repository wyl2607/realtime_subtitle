import AVFoundation
import CryptoKit
import Foundation

/// TK-001 实测：SpeechTranscriber 的 volatile 结果没有词级时间，用它判段尾会把延迟做成 final 的 ~2s。
/// 所以收段只看环形缓冲里的 16k 能量（A3'）。上屏时间用 Groq 返回的 segments，不再用本机 final 改已发出段。
///
/// 门限写死，不是旋钮：20ms 帧 RMS ≥ 0.005 算有声；静音 ≥ 0.6s 收段；段长 ≤ 15s 强切；
/// 短于 1.5s 且与下一段间隔在 (0.02s, 1.5s) 才合并。间隔为 0 的是强切接缝，不能粘回去。
struct EmittedCloudSegment: Equatable {
    var id: Int
    var speechA0: Double
    var speechA1: Double
    var audioOrigin: Double
    var pcm16: Data
    var reason: String

    var duration: Double {
        Double(pcm16.count) / 2 / 16_000
    }
}

struct SegmenterOutput: Equatable {
    var segments: [EmittedCloudSegment]
    /// 这一拍的末尾是否已经处于 ≥0.6s 的静音。层间切换只在这个沿上发生。
    var atSilence: Bool
}

struct CloudSpeechBounds: Equatable {
    var speechA0: Double
    var speechA1: Double
    var audioOrigin: Double
    var pcmCount: Int
}

final class CloudSegmenter {
    static let sampleRate = 16_000.0
    static let frameSeconds = 0.02
    static let rmsThreshold = 0.005
    static let silenceSeconds = 0.6
    static let maxSeconds = 15.0
    static let mergeShortSeconds = 1.5
    static let mergeGapSeconds = 1.5
    /// 强切接缝的间隔是 0。留出半帧，避免浮点把接缝当成可合并的停顿。
    static let mergeGapFloor = 0.02
    static let audioPad = 0.2

    private struct OpenSpeech {
        var a0: Double
        var last: Double
    }

    private struct HeldSpeech {
        var speechA0: Double
        var speechA1: Double
        var reason: String
    }

    private var samples: [Float] = []
    private var origin = 0.0
    private var frameCursor = 0
    private var open: OpenSpeech?
    private var held: HeldSpeech?
    private var queued: [HeldSpeech] = []
    private var silenceRun = 0.0
    private var nextID = 1
    private var converter: AVAudioConverter?
    private var converterKey: String?
    private let eps = 1e-6

    func append(audio: TimedAudio) -> SegmenterOutput {
        append(samples: Self.floats16k(from: audio, converter: &converter, converterKey: &converterKey), startSeconds: audio.startSeconds)
    }

    func append(samples incoming: [Float], startSeconds: Double) -> SegmenterOutput {
        guard !incoming.isEmpty else {
            return SegmenterOutput(segments: [], atSilence: atSilence)
        }
        if samples.isEmpty && open == nil && held == nil && queued.isEmpty {
            origin = startSeconds
            samples = incoming
            frameCursor = 0
            return SegmenterOutput(segments: drainFrames(), atSilence: atSilence)
        }
        let availableEnd = origin + Double(samples.count) / Self.sampleRate
        var emitted: [EmittedCloudSegment] = []
        if startSeconds > availableEnd + eps {
            let gap = startSeconds - availableEnd
            // 缺的样本按静音计。大缺口不把整段零填进缓冲，否则断网几分钟会把环形缓冲撑满。
            if gap >= Self.silenceSeconds - eps {
                silenceRun += gap
                emitted.append(contentsOf: closeOpenIfSilent())
                // 缺口前的语音只能从旧缓冲里切：换缓冲之前全部发出（拿住等合并的短段也一样，
                // 隔着断流没东西可并）。留到之后再发，切片会落在新时间轴上，切出空 PCM。
                if let pending = held {
                    held = nil
                    queued.append(pending)
                }
                emitted.append(contentsOf: flushReady(force: true))
                samples = incoming
                origin = startSeconds
                frameCursor = 0
                silenceRun = 0
            } else {
                let zeros = Int((gap * Self.sampleRate).rounded())
                if zeros > 0 {
                    samples.append(contentsOf: repeatElement(0, count: zeros))
                }
                samples.append(contentsOf: incoming)
            }
        } else if startSeconds + eps < availableEnd {
            let overlap = availableEnd - startSeconds
            let skip = min(incoming.count, max(0, Int((overlap * Self.sampleRate).rounded())))
            if skip < incoming.count {
                samples.append(contentsOf: incoming[skip...])
            }
        } else {
            samples.append(contentsOf: incoming)
        }
        emitted.append(contentsOf: drainFrames())
        return SegmenterOutput(segments: emitted, atSilence: atSilence)
    }

    /// 流结束不会再来下一段。短段不用再干等 1.5s，否则文件尾的最后一句永远发不出去。
    func finish() -> [EmittedCloudSegment] {
        var emitted: [EmittedCloudSegment] = []
        if open != nil {
            silenceRun = max(silenceRun, Self.silenceSeconds)
            emitted.append(contentsOf: closeOpenIfSilent())
        }
        if let pending = held {
            emitted.append(contentsOf: take(pending))
            held = nil
        }
        emitted.append(contentsOf: flushReady(force: true))
        return emitted
    }

    private var atSilence: Bool {
        open == nil && silenceRun >= Self.silenceSeconds - eps
    }

    private func drainFrames() -> [EmittedCloudSegment] {
        var emitted: [EmittedCloudSegment] = []
        let frame = max(1, Int(Self.sampleRate * Self.frameSeconds))
        while samples.count - frameCursor >= frame {
            let index = frameCursor
            frameCursor += frame
            let t0 = origin + Double(index) / Self.sampleRate
            let t1 = t0 + Self.frameSeconds
            var sum = 0.0
            for sample in samples[index..<(index + frame)] {
                let value = Double(sample)
                sum += value * value
            }
            let rms = (sum / Double(frame)).squareRoot()
            // Float 常量 0.005 变成 Double 会略小，严格 >= 会把刚好踩线的帧判成静音。
            // 1e-7 只补这个表示误差，0.004 仍然远低于门限。
            if rms >= Self.rmsThreshold - 1e-7 {
                emitted.append(contentsOf: onVoiced(t0: t0, t1: t1))
            } else {
                emitted.append(contentsOf: onSilent())
            }
            emitted.append(contentsOf: flushReady())
        }
        trim()
        return emitted
    }

    private func onVoiced(t0: Double, t1: Double) -> [EmittedCloudSegment] {
        silenceRun = 0
        var emitted: [EmittedCloudSegment] = []
        if open == nil {
            if let pending = held {
                let gap = t0 - pending.speechA1
                let short = pending.speechA1 - pending.speechA0 < Self.mergeShortSeconds - eps
                if short, gap > Self.mergeGapFloor, gap < Self.mergeGapSeconds, pending.reason != "max" {
                    open = OpenSpeech(a0: pending.speechA0, last: t1)
                    held = nil
                } else {
                    emitted.append(contentsOf: take(pending))
                    held = nil
                    open = OpenSpeech(a0: t0, last: t1)
                }
            } else {
                open = OpenSpeech(a0: t0, last: t1)
            }
        } else {
            open?.last = t1
        }
        while let current = open, current.last - current.a0 >= Self.maxSeconds - eps {
            let cut = current.a0 + Self.maxSeconds
            queued.append(HeldSpeech(speechA0: current.a0, speechA1: cut, reason: "max"))
            if current.last - cut <= eps {
                open = nil
            } else {
                open = OpenSpeech(a0: cut, last: current.last)
            }
        }
        return emitted
    }

    private func onSilent() -> [EmittedCloudSegment] {
        silenceRun += Self.frameSeconds
        var emitted = closeOpenIfSilent()
        emitted.append(contentsOf: emitHeldIfMergeWindowPassed())
        return emitted
    }

    private func closeOpenIfSilent() -> [EmittedCloudSegment] {
        guard let current = open, silenceRun >= Self.silenceSeconds - eps else { return [] }
        open = nil
        let pending = HeldSpeech(speechA0: current.a0, speechA1: current.last, reason: "silence")
        if current.last - current.a0 >= Self.mergeShortSeconds - eps {
            return take(pending)
        }
        // 短段先拿住：1.5s 内若还有下一段，要并成一次上传，否则按 10s 起跳白烧额度。
        if let previous = held {
            held = pending
            return take(previous)
        }
        held = pending
        return []
    }

    private func emitHeldIfMergeWindowPassed() -> [EmittedCloudSegment] {
        guard let pending = held, silenceRun >= Self.mergeGapSeconds - eps else { return [] }
        held = nil
        return take(pending)
    }

    private func take(_ pending: HeldSpeech) -> [EmittedCloudSegment] {
        queued.append(pending)
        return flushReady()
    }

    private func flushReady(force: Bool = false) -> [EmittedCloudSegment] {
        let availableEnd = origin + Double(samples.count) / Self.sampleRate
        var emitted: [EmittedCloudSegment] = []
        var still: [HeldSpeech] = []
        for pending in queued {
            let readyAt = pending.speechA1 + Self.audioPad
            if force || availableEnd + eps >= readyAt {
                emitted.append(emit(pending, availableEnd: availableEnd))
            } else {
                still.append(pending)
            }
        }
        queued = still
        return emitted
    }

    private func emit(_ pending: HeldSpeech, availableEnd: Double) -> EmittedCloudSegment {
        let start = max(origin, pending.speechA0 - Self.audioPad)
        let end = min(availableEnd, pending.speechA1 + Self.audioPad)
        let floats = slice(from: start, to: max(start, end))
        let pcm = Self.pcm16(floats)
        let id = nextID
        nextID += 1
        let originOut = floats.isEmpty ? start : start
        return EmittedCloudSegment(
            id: id,
            speechA0: pending.speechA0,
            speechA1: pending.speechA1,
            audioOrigin: originOut,
            pcm16: pcm,
            reason: pending.reason
        )
    }

    private func slice(from start: Double, to end: Double) -> [Float] {
        guard end > start, !samples.isEmpty else { return [] }
        let i0 = max(0, min(samples.count, Int(((start - origin) * Self.sampleRate).rounded())))
        let i1 = max(i0, min(samples.count, Int(((end - origin) * Self.sampleRate).rounded())))
        return Array(samples[i0..<i1])
    }

    private func trim() {
        // 段还没开口时也留下 0.2s，否则前垫在收段前就被裁掉，切片会从 a0 而不是 a0-0.2 开始。
        var keep = origin + Double(frameCursor) / Self.sampleRate - Self.audioPad
        if let open {
            keep = min(keep, open.a0 - Self.audioPad)
        }
        if let held {
            keep = min(keep, held.speechA0 - Self.audioPad)
        }
        for pending in queued {
            keep = min(keep, pending.speechA0 - Self.audioPad)
        }
        var drop = Int(((keep - origin) * Self.sampleRate).rounded(.down))
        drop = max(0, min(drop, frameCursor))
        guard drop > 0 else { return }
        samples.removeFirst(drop)
        origin += Double(drop) / Self.sampleRate
        frameCursor -= drop
    }

    private static func pcm16(_ floats: [Float]) -> Data {
        var data = Data(capacity: floats.count * 2)
        for sample in floats {
            let clamped = max(-1 as Float, min(1, sample))
            var unit = Int16((clamped * 32_767).rounded()).littleEndian
            withUnsafeBytes(of: &unit) { data.append(contentsOf: $0) }
        }
        return data
    }

    private static func floats16k(
        from audio: TimedAudio,
        converter: inout AVAudioConverter?,
        converterKey: inout String?
    ) -> [Float] {
        let buffer = audio.buffer
        let format = buffer.format
        if format.sampleRate == sampleRate,
           format.channelCount == 1,
           format.commonFormat == .pcmFormatFloat32,
           let channel = buffer.floatChannelData?[0] {
            return Array(UnsafeBufferPointer(start: channel, count: Int(buffer.frameLength)))
        }
        let key = "\(format.sampleRate)-\(format.channelCount)-\(format.commonFormat.rawValue)"
        if converterKey != key {
            let outputFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: sampleRate, channels: 1, interleaved: false)
            converter = outputFormat.flatMap { AVAudioConverter(from: format, to: $0) }
            converterKey = key
        }
        guard let converter else { return [] }
        let outputFormat = converter.outputFormat
        guard outputFormat.sampleRate > 0 else { return [] }
        let ratio = outputFormat.sampleRate / max(format.sampleRate, 1)
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * ratio) + 1024
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else { return [] }
        var didProvide = false
        var convertError: NSError?
        let status = converter.convert(to: output, error: &convertError) { _, outStatus in
            if didProvide {
                outStatus.pointee = .noDataNow
                return nil
            }
            didProvide = true
            outStatus.pointee = .haveData
            return buffer
        }
        if convertError != nil || status == .error || output.frameLength == 0 {
            return []
        }
        guard let channel = output.floatChannelData?[0] else { return [] }
        return Array(UnsafeBufferPointer(start: channel, count: Int(output.frameLength)))
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        func feed(_ segmenter: CloudSegmenter, _ amplitude: Float, _ seconds: Double, _ clock: inout Double) -> [EmittedCloudSegment] {
            let count = Int((seconds * sampleRate).rounded())
            let samples = [Float](repeating: amplitude, count: count)
            let output = segmenter.append(samples: samples, startSeconds: clock)
            clock += Double(count) / sampleRate
            return output.segments
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            var got = feed(segmenter, 0.05, 1.0, &clock)
            got += feed(segmenter, 0, 0.1, &clock)
            clock += 1.0
            got += feed(segmenter, 0, 0.6, &clock)
            got += segmenter.finish()
            check(got.count == 1, "缺口前的短段应发出一段，实际 \(got.count)")
            check(got.allSatisfy { !$0.pcm16.isEmpty }, "缺口前的短段不得切出空 PCM")
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            var got = feed(segmenter, 0.05, 2.0, &clock)
            got += feed(segmenter, 0, 0.1, &clock)
            clock += 5.0
            got += feed(segmenter, 0, 0.6, &clock)
            got += segmenter.finish()
            check(got.count == 1, "缺口前的长段应发出一段，实际 \(got.count)")
            check(got.allSatisfy { !$0.pcm16.isEmpty }, "缺口前的长段不得切出空 PCM")
            if let segment = got.first {
                check(segment.pcm16.count >= Int(2.0 * sampleRate) * 2, "缺口前的长段应带上完整语音，字节 \(segment.pcm16.count)")
            }
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 10.0
            var got = feed(segmenter, 0.05, 2.0, &clock)
            check(got.isEmpty, "长段要等静音才收，不能边说边发")
            got = feed(segmenter, 0, 0.6, &clock)
            check(got.count == 1, "2s 语音后静音 0.6s 应收成一段，实际 \(got.count)")
            if let segment = got.first {
                check(abs(segment.speechA0 - 10) < 0.03, "段起点应落在样本时钟 10s，实际 \(segment.speechA0)")
                check(abs(segment.speechA1 - 12) < 0.03, "段终点应落在样本时钟 12s，实际 \(segment.speechA1)")
                check(segment.reason == "silence", "静音收段 reason 应为 silence")
                let expectedSamples = Int((2.2 * sampleRate).rounded())
                check(segment.pcm16.count == expectedSamples * 2, "音频应按 [a0-0.2, a1+0.2] 截取，字节 \(segment.pcm16.count)")
                check(abs(segment.audioOrigin - 10) < 0.03, "段首没有更早的样本时，切片起点钳在样本时钟，实际 \(segment.audioOrigin)")
            }
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 10.0
            _ = feed(segmenter, 0, 0.3, &clock)
            _ = feed(segmenter, 0.05, 2.0, &clock)
            let got = feed(segmenter, 0, 0.6, &clock)
            guard let segment = got.first else {
                check(false, "有前导静音时仍应收到一段")
                return failures
            }
            check(abs(segment.speechA0 - 10.3) < 0.03, "有声起点应跳过前导静音，实际 \(segment.speechA0)")
            check(abs(segment.audioOrigin - 10.1) < 0.03, "有前导音频时切片从 a0-0.2 开始，实际 \(segment.audioOrigin)")
            let padded = Int((2.4 * sampleRate).rounded())
            check(segment.pcm16.count == padded * 2, "前后各 0.2s 垫，字节 \(segment.pcm16.count)")
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            _ = feed(segmenter, 0.05, 0.4, &clock)
            _ = feed(segmenter, 0, 0.8, &clock)
            _ = feed(segmenter, 0.05, 0.4, &clock)
            let got = feed(segmenter, 0, 0.6, &clock)
            check(got.count == 1, "短段间隔 0.8s 应合并成一段，实际 \(got.count)")
            if let segment = got.first {
                check(abs(segment.speechA0 - 0) < 0.03 && abs(segment.speechA1 - 1.6) < 0.03,
                      "合并后应覆盖两段语音，实际 \(segment.speechA0)-\(segment.speechA1)")
            }
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            let first = feed(segmenter, 0.05, 0.4, &clock) + feed(segmenter, 0, 1.5, &clock)
            check(first.count == 1, "间隔满 1.5s 的短段应单独发出，实际 \(first.count)")
            let second = feed(segmenter, 0.05, 0.4, &clock) + feed(segmenter, 0, 1.5, &clock)
            check(second.count == 1, "下一段不应再粘回上一段")
            if let a = first.first, let b = second.first {
                check(b.speechA0 > a.speechA1 + 1.0, "两段之间应留下 ≥1.5s 的空档")
            }
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            var got = feed(segmenter, 0.05, 15.0, &clock)
            check(got.isEmpty, "刚好 15s 时后垫的 0.2s 还没到，不应提前发")
            got = feed(segmenter, 0, 0.2, &clock)
            check(got.count == 1 && got.first?.reason == "max", "15s 应强切，实际 \(got.map(\.reason))")
            if let segment = got.first {
                check(abs(segment.speechA1 - segment.speechA0 - 15) < 0.03, "强切长度应为 15s")
            }
            let rest = feed(segmenter, 0.05, 0.4, &clock) + feed(segmenter, 0, 1.5, &clock)
            check(rest.count == 1 && rest.first?.reason == "silence", "强切之后的尾巴应单独成段")
            if let tail = rest.first, let head = got.first {
                check(tail.speechA0 + 0.03 >= head.speechA1, "强切接缝间隔为 0，不得合并")
                check(tail.id != head.id, "强切和尾巴不是同一段")
            }
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            _ = feed(segmenter, 0.004, 0.5, &clock)
            _ = feed(segmenter, 0.005, 2.0, &clock)
            let got = feed(segmenter, 0, 0.6, &clock)
            check(got.count == 1, "低于 0.005 的能量不算语音")
            if let segment = got.first {
                check(abs(segment.speechA0 - 0.5) < 0.03, "有声起点应在阈值之上的那一拍，实际 \(segment.speechA0)")
            }
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            let got = feed(segmenter, 0.05, 2.0, &clock) + feed(segmenter, 0, 0.6, &clock)
            guard let segment = got.first else {
                check(false, "校正前应能查到已发出段")
                return failures
            }
            let expectedSamples = Int((2.2 * sampleRate).rounded())
            check(segment.pcm16.count == expectedSamples * 2, "校正不得改动已发音频")
            check(abs(segment.audioOrigin - 0) < 0.03, "校正不得改动切片起点")
            check(abs(segment.speechA0 - 0) < 0.03 && abs(segment.speechA1 - 2) < 0.03,
                  "已发出段界保持能量包络，final 不再改写")
        }

        do {
            let segmenter = CloudSegmenter()
            // 0.004 落在旧的整块 RMS 0.003 和新门限 0.005 之间。分段器必须把它当成静音。
            let quiet = segmenter.append(
                samples: [Float](repeating: 0.004, count: Int((0.8 * sampleRate).rounded())),
                startSeconds: 0
            )
            check(quiet.segments.isEmpty, "0.004 低于 0.005，不是语音")
            check(quiet.atSilence, "0.004 持续 0.8s 应算静音，节点释放消费这一判定")
            let voiced = segmenter.append(
                samples: [Float](repeating: 0.02, count: Int(sampleRate)),
                startSeconds: 0.8
            )
            check(!voiced.atSilence, "有声帧应清掉静音")
        }

        do {
            let segmenter = CloudSegmenter()
            var clock = 0.0
            let early = feed(segmenter, 0.05, 0.4, &clock)
            check(early.isEmpty, "流没结束时短段还要等可能的下一段")
            let tail = segmenter.finish()
            check(tail.count == 1, "流结束应把拿住的短段发出去，实际 \(tail.count)")
        }

        return failures
    }
}

// MARK: - 分级选择

/// 云端和节点只在这里会合。判据只有 CloudRouteContext.tier() 一份，P9 夹具盯的就是它。
enum TieredProviders {
    static func select(_ context: CloudRouteContext) -> ProviderTier {
        let decided = context.tier()
        let cloud = ClosureProvider(tier: .cloud) { decided == .cloud }
        let node = ClosureProvider(tier: .node) { decided == .node }
        if cloud.available() { return cloud.tier }
        if node.available() { return node.tier }
        return .local
    }
}

private struct ClosureProvider: Provider {
    let tier: ProviderTier
    let isAvailable: () -> Bool
    func available() -> Bool { isAvailable() }
}

protocol CloudTranscribing: AnyObject, Sendable {
    func transcribe(pcm16Mono16k: Data, a0: Double, duration: Double, language: String, segmentID: Int?) async throws -> CloudTranscriptionResult
    func cancelInflightRequests()
}

extension CloudClient: CloudTranscribing {}

struct CloudSwitchSnapshot: Equatable {
    var hasKey: Bool
    var enabled: Bool
    /// 只交给 CloudClient 建连。失败信息里不准带上它。
    var key: String?
}

struct CloudP5Line: Equatable {
    var t0: Double
    var t1: Double
    var text: String
}

struct CloudControlEffect: Equatable {
    var mode: SubtitleMode
    var nodeTierEnabled: Bool
    var lines: [CloudP5Line]
    var discard: Bool
    var cancelInflight: Bool
    /// Tier0 / 云端状态变化时才为 true。Tier1 期间不写状态行。
    var publishStatus: Bool
    /// Tier1 把状态行交给 NodeRouter。
    var yieldStatusToNode: Bool
}

struct CloudIngestEffect: Equatable {
    var uploads: [EmittedCloudSegment]
    var control: CloudControlEffect
    var atSilence: Bool
}

/// 云端上传调度。分段器给出段，这里按 P7' 决定发不发、何时从节点切回来。
/// 不写 nodes.json：云端不是节点。
final class CloudUploadController: @unchecked Sendable {
    static let cloudNodeID = "cloud:groq"

    private let ledger: QuotaLedger
    private let readSwitch: () -> CloudSwitchSnapshot
    private let makeClient: (String) -> any CloudTranscribing
    private let language: String
    private let lock = NSLock()

    private var segmenter = CloudSegmenter()
    private var context: CloudRouteContext
    private var client: (any CloudTranscribing)?
    private var lastKey: String?
    private var epoch = 0
    private var segmentEpoch: [Int: Int] = [:]
    private var accepted = Set<Int>()
    private var probeID: Int?
    private var cooldownDeadline: Date?
    private var returnGate = MigrationGate()
    private var cloudGateOpen = false
    private var inSilence = false
    private var lastMode: SubtitleMode?
    private var sawInvalidKey = false
    /// 在飞段的能量包络。resolve 后删掉，取消时清空，避免无界增长。
    private var segmentBounds: [Int: CloudSpeechBounds] = [:]
    private static let boundsLimit = 64

    init(
        localeID: String,
        ledger: QuotaLedger,
        readSwitch: @escaping () -> CloudSwitchSnapshot,
        makeClient: @escaping (String) -> any CloudTranscribing
    ) {
        self.ledger = ledger
        self.readSwitch = readSwitch
        self.makeClient = makeClient
        language = Self.groqLanguage(localeID: localeID)
        let initial = readSwitch()
        lastKey = initial.key
        // 构造时已有 key 也要先比对证明，初次额度查询不能确认另一个 key 的旧边界。
        try? ledger.observeKey(initial.key.map(Self.quotaKeyFingerprint))
        let cloudOn = initial.hasKey && initial.enabled
        context = CloudRouteContext(RouteInitial(
            hasKey: initial.hasKey,
            cloudEnabled: initial.enabled && initial.hasKey,
            cloudState: cloudOn ? .available : .off,
            nodeEnabled: false,
            nodeHealthy: false,
            nodeIsSelf: false,
            consecutiveFailures: 0,
            backoffIndex: 0
        ))
        if let key = initial.key, cloudOn {
            client = makeClient(key)
        }
    }

    var groqLanguageCode: String { language }

    /// 429 限流冷却中。节点路由在冷却期接手状态行，HybridEngine 靠这个标志补后缀。
    var isRateLimited: Bool {
        lock.lock()
        defer { lock.unlock() }
        return context.cloudState == .cooldownRateLimited
    }

    func ingest(samples: [Float], startSeconds: Double, now: Date, node: NodeTierFacts) -> CloudIngestEffect {
        lock.lock()
        defer { lock.unlock() }
        let cancel = refreshSwitchLocked()
        expireIfNeeded(now)
        applyNodeFacts(node)
        ledger.applyQuotaEvent(to: &context, at: now)
        let output = segmenter.append(samples: samples, startSeconds: startSeconds)
        let uploads = takeUploads(output.segments)
        let control = reconcileLocked(atSilence: output.atSilence, node: node, lines: [], discard: false, cancel: cancel)
        return CloudIngestEffect(uploads: uploads, control: control, atSilence: output.atSilence)
    }

    func ingest(audio: TimedAudio, now: Date, node: NodeTierFacts) -> CloudIngestEffect {
        lock.lock()
        defer { lock.unlock() }
        let cancel = refreshSwitchLocked()
        expireIfNeeded(now)
        applyNodeFacts(node)
        ledger.applyQuotaEvent(to: &context, at: now)
        let output = segmenter.append(audio: audio)
        let uploads = takeUploads(output.segments)
        let control = reconcileLocked(atSilence: output.atSilence, node: node, lines: [], discard: false, cancel: cancel)
        return CloudIngestEffect(uploads: uploads, control: control, atSilence: output.atSilence)
    }

    func finish(now: Date, node: NodeTierFacts) -> CloudIngestEffect {
        lock.lock()
        defer { lock.unlock() }
        let cancel = refreshSwitchLocked()
        expireIfNeeded(now)
        applyNodeFacts(node)
        let segments = segmenter.finish()
        let uploads = takeUploads(segments)
        let control = reconcileLocked(atSilence: true, node: node, lines: [], discard: false, cancel: cancel)
        return CloudIngestEffect(uploads: uploads, control: control, atSilence: true)
    }

    /// 真正发请求前再查一次开关和额度。返回 nil 表示这段留在本机，不再重发。
    func prepareSend(_ segment: EmittedCloudSegment, now: Date) -> (any CloudTranscribing)? {
        lock.lock()
        defer { lock.unlock() }
        // refresh 在需要时已经取消在飞请求并推进代次，这里再调一次会把同一次关闭记成两次。
        _ = refreshSwitchLocked()
        expireIfNeeded(now)
        ledger.applyQuotaEvent(to: &context, at: now)
        // 先判断再占位。shouldUpload 会拒绝已占位的段，插在判断前面会把第一次发送也拒掉。
        guard !accepted.contains(segment.id) else { return nil }
        let allowed = shouldUploadLocked(segment, consumeProbe: false)
            || (probeID == segment.id && context.tier() == .cloud)
        guard allowed, context.tier() == .cloud else { return nil }
        do {
            _ = try ledger.recordRequest(duration: segment.duration, at: now)
        } catch {
            accepted.insert(segment.id)
            return nil
        }
        ledger.applyQuotaEvent(to: &context, at: now)
        guard context.tier() == .cloud, let client else {
            accepted.insert(segment.id)
            return nil
        }
        accepted.insert(segment.id)
        segmentEpoch[segment.id] = epoch
        if context.cloudState == .probe {
            probeID = segment.id
        }
        return client
    }

    func resolveSuccess(id: Int, result: CloudTranscriptionResult, now: Date, node: NodeTierFacts) -> CloudControlEffect {
        lock.lock()
        defer { lock.unlock() }
        let cancel = refreshSwitchLocked()
        expireIfNeeded(now)
        applyNodeFacts(node)
        if id == probeID { probeID = nil }
        let bounds = segmentBounds.removeValue(forKey: id)
        if dropResolvedResultLocked(cancel: cancel) || segmentEpoch[id] != epoch {
            return reconcileLocked(atSilence: false, node: node, lines: [], discard: true, cancel: cancel)
        }
        var quotaHeaders: [AnyHashable: Any] = [:]
        if let remaining = result.remainingRequests {
            quotaHeaders["x-ratelimit-remaining-requests"] = NSNumber(value: remaining)
        }
        if let resetSeconds = result.resetRequestsS {
            quotaHeaders["x-ratelimit-reset-requests"] = "\(resetSeconds)s"
        }
        if !quotaHeaders.isEmpty {
            try? ledger.note(
                quotaHeaders,
                at: now,
                keyFingerprint: result.keyFingerprint,
                sentAt: result.sentAt
            )
        }
        context.apply(RouteEvent(type: "segment_success", status: nil, retryAfter: nil, usagePercent: nil))
        clearCooldownIfResumed()
        let lines = Self.p5Lines(result: result, bounds: bounds)
        return reconcileLocked(atSilence: false, node: node, lines: lines, discard: false, cancel: false)
    }

    func resolveFailure(id: Int, error: Error, now: Date, node: NodeTierFacts) -> CloudControlEffect {
        lock.lock()
        defer { lock.unlock() }
        let cancel = refreshSwitchLocked()
        expireIfNeeded(now)
        applyNodeFacts(node)
        if id == probeID { probeID = nil }
        segmentBounds.removeValue(forKey: id)
        if dropResolvedResultLocked(cancel: cancel) || segmentEpoch[id] != epoch || Self.isCancellation(error) {
            return reconcileLocked(atSilence: false, node: node, lines: [], discard: true, cancel: cancel)
        }
        if let http = error as? CloudClientError, case .httpStatus(let status, let retryAfter, _, let quota) = http {
            let wasRateLimited = context.cloudState == .cooldownRateLimited
            var quotaHeaders: [AnyHashable: Any] = [:]
            if let remaining = quota?.remainingRequests {
                quotaHeaders["x-ratelimit-remaining-requests"] = NSNumber(value: remaining)
            }
            if let resetSeconds = quota?.resetRequestsS {
                quotaHeaders["x-ratelimit-reset-requests"] = "\(resetSeconds)s"
            }
            if !quotaHeaders.isEmpty {
                try? ledger.note(
                    quotaHeaders,
                    at: now,
                    keyFingerprint: quota?.keyFingerprint,
                    sentAt: quota?.sentAt
                )
            }
            context.apply(RouteEvent(type: "http_status", status: status, retryAfter: retryAfter, usagePercent: nil))
            noteCooldown(now)
            if status == 429 && !wasRateLimited {
                // 只在进入冷却时打一行；key 和正文都不进日志。
                Self.writeCooldownLog(status: status, retryAfter: context.cooldownSeconds ?? 60)
            }
            return reconcileLocked(atSilence: false, node: node, lines: [], discard: false, cancel: false)
        }
        context.apply(RouteEvent(type: "segment_failure", status: nil, retryAfter: nil, usagePercent: nil))
        noteCooldown(now)
        return reconcileLocked(atSilence: false, node: node, lines: [], discard: false, cancel: false)
    }

    func bounds(for id: Int) -> CloudSpeechBounds? {
        lock.lock()
        defer { lock.unlock() }
        return segmentBounds[id]
    }

    func attemptCount(_ id: Int) -> Int {
        lock.lock()
        defer { lock.unlock() }
        return accepted.contains(id) ? 1 : 0
    }

    /// 启动时先给出状态行和节点层开关，不必等第一段音频。不发请求。
    func snapshot(node: NodeTierFacts) -> CloudControlEffect {
        lock.lock()
        defer { lock.unlock() }
        let cancel = refreshSwitchLocked()
        applyNodeFacts(node)
        ledger.applyQuotaEvent(to: &context, at: Date())
        return reconcileLocked(atSilence: false, node: node, lines: [], discard: false, cancel: cancel)
    }

    /// 暂停/停止：在飞结果作废，但不记成段失败，冷却也不清掉。
    func cancelInflight() {
        lock.lock()
        defer { lock.unlock() }
        epoch += 1
        probeID = nil
        segmentBounds.removeAll()
        client?.cancelInflightRequests()
    }

    /// 冷却日志的唯一样式，selftest 直接断言这行，避免文案只存在于 write 调用里。
    static func cooldownLogLine(status: Int, retryAfter: Int) -> String {
        "rslite.cloud cooldown reason=\(status) retry_after=\(retryAfter)"
    }

    static func writeCooldownLog(status: Int, retryAfter: Int) {
        FileHandle.standardError.write(Data((cooldownLogLine(status: status, retryAfter: retryAfter) + "\n").utf8))
    }

    static func groqLanguage(localeID: String) -> String {
        let head = localeID.split(whereSeparator: { $0 == "-" || $0 == "_" }).first.map(String.init) ?? localeID
        let lower = head.lowercased()
        return lower.isEmpty ? "de" : lower
    }

    /// Groq 的 segments 已由 CloudClient 按音频起点映回样本时钟。没有 segments 时用校正后的能量包络。
    static func p5Lines(result: CloudTranscriptionResult, bounds: CloudSpeechBounds?) -> [CloudP5Line] {
        let lines = result.segments.filter { !$0.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty }
        if !lines.isEmpty {
            return lines.map { CloudP5Line(t0: $0.t0, t1: $0.t1, text: $0.text) }
        }
        let text = result.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, let bounds, bounds.speechA1 >= bounds.speechA0 else { return [] }
        return [CloudP5Line(t0: bounds.speechA0, t1: bounds.speechA1, text: text)]
    }

    private func takeUploads(_ segments: [EmittedCloudSegment]) -> [EmittedCloudSegment] {
        let uploads = segments.filter { shouldUploadLocked($0) }
        for segment in uploads {
            rememberBounds(segment)
        }
        return uploads
    }

    private func rememberBounds(_ segment: EmittedCloudSegment) {
        segmentBounds[segment.id] = CloudSpeechBounds(
            speechA0: segment.speechA0,
            speechA1: segment.speechA1,
            audioOrigin: segment.audioOrigin,
            pcmCount: segment.pcm16.count
        )
        while segmentBounds.count > Self.boundsLimit {
            guard let oldest = segmentBounds.keys.min() else { break }
            segmentBounds.removeValue(forKey: oldest)
        }
    }

    /// 开关已关或 key 已清：结果不上屏，也不记成段成功/失败。
    private func dropResolvedResultLocked(cancel: Bool) -> Bool {
        if cancel { return true }
        if !context.hasKey || !context.cloudEnabled || context.cloudState == .off {
            return true
        }
        return false
    }

    private func shouldUploadLocked(_ segment: EmittedCloudSegment, consumeProbe: Bool = true) -> Bool {
        guard !accepted.contains(segment.id) else { return false }
        guard context.tier() == .cloud else { return false }
        if context.cloudState == .probe {
            if probeID != nil { return false }
            if consumeProbe { probeID = segment.id }
            return true
        }
        return context.cloudState == .available
    }

    private static func quotaKeyFingerprint(_ key: String) -> String {
        SHA256.hash(data: Data(key.utf8)).map { String(format: "%02x", $0) }.joined()
    }

    private func refreshSwitchLocked() -> Bool {
        let snap = readSwitch()
        var cancel = false
        let clearingKey = !snap.hasKey
        if clearingKey {
            if context.hasKey || context.cloudState != .off {
                context.apply(RouteEvent(type: "key_cleared", status: nil, retryAfter: nil, usagePercent: nil))
                cancel = context.cancelInflight
            }
            lastKey = nil
        } else if !snap.enabled {
            if context.cloudEnabled && context.cloudState != .off {
                context.apply(RouteEvent(type: "switch_off", status: nil, retryAfter: nil, usagePercent: nil))
                cancel = context.cancelInflight
            }
        } else if lastKey != snap.key {
            // 必须早于调用方随后执行的额度轮询，不记请求、不推进墙钟也不剪历史。
            try? ledger.observeKey(snap.key.map(Self.quotaKeyFingerprint))
            context.apply(RouteEvent(type: "key_modified", status: nil, retryAfter: nil, usagePercent: nil))
            lastKey = snap.key
            // 换 client 之前作废旧 key 的在飞请求：推进代次，旧 key 的迟到 401 就过不了
            // segmentEpoch 检查，不会把新 key 打成无效（之后 key 不变，key_modified 不会再来救）。
            // 不能交给末尾的 `if cancel`——那时 client 已经换成新的，会取消错对象。
            epoch += 1
            probeID = nil
            segmentBounds.removeAll()
            client?.cancelInflightRequests()
            client = makeClient(snap.key ?? "")
            clearCooldownIfResumed()
        } else if context.cloudState == .off {
            context.apply(RouteEvent(type: "switch_on", status: nil, retryAfter: nil, usagePercent: nil))
            if client == nil, let key = snap.key {
                client = makeClient(key)
            }
            clearCooldownIfResumed()
        }
        if cancel {
            // 代次一变，在飞响应就算回来也进不了 P5。先取消，再丢掉 client，避免取消落空。
            epoch += 1
            probeID = nil
            segmentBounds.removeAll()
            client?.cancelInflightRequests()
        }
        if clearingKey {
            client = nil
        }
        return cancel
    }

    private func expireIfNeeded(_ now: Date) {
        guard let deadline = cooldownDeadline, now >= deadline else { return }
        guard context.cloudState == .cooldownBackoff
                || context.cloudState == .cooldownRateLimited
                || context.cloudState == .cooldownQuota else {
            cooldownDeadline = nil
            return
        }
        cooldownDeadline = nil
        context.apply(RouteEvent(type: "cooldown_expired", status: nil, retryAfter: nil, usagePercent: nil))
    }

    private func noteCooldown(_ now: Date) {
        guard let seconds = context.cooldownSeconds else { return }
        cooldownDeadline = now.addingTimeInterval(TimeInterval(seconds))
    }

    private func clearCooldownIfResumed() {
        if context.cloudState == .available {
            cooldownDeadline = nil
        }
    }

    private func applyNodeFacts(_ node: NodeTierFacts) {
        context.nodeEnabled = node.enabled
        context.nodeHealthy = node.healthy && !node.isSelf
        context.nodeIsSelf = node.isSelf
    }

    private func reconcileLocked(
        atSilence: Bool,
        node: NodeTierFacts,
        lines: [CloudP5Line],
        discard: Bool,
        cancel: Bool
    ) -> CloudControlEffect {
        let silenceEdge: Bool
        if atSilence {
            silenceEdge = !inSilence
            inSilence = true
        } else {
            silenceEdge = false
            inSilence = false
        }
        let desired = TieredProviders.select(context)
        if desired == .cloud, let active = node.activeNodeID {
            if silenceEdge {
                let margin = RouteWeights.default.migrationMargin
                // 云端不是 nodes.json 里的节点。分数只用来过 MigrationGate 的领先阈值。
                let cloud = RouteScore(id: Self.cloudNodeID, score: margin, reason: "tier0")
                let current = RouteScore(id: active, score: 0, reason: "tier1")
                // 节点→云端沿用 MigrationGate：静音点上连续两轮才放行，避免一句中间来回切。
                cloudGateOpen = returnGate.observe(best: cloud, current: current)
            }
        } else {
            // 节点会话已经不在，或当前不想回云端。留下的两轮计数会让下一次回云端少等一轮。
            _ = returnGate.observe(best: nil, current: nil)
            cloudGateOpen = false
        }
        let waitingOnNode = desired == .cloud && node.activeNodeID != nil && !cloudGateOpen
        let baseMode = presentationMode(desired: desired, waitingOnNode: waitingOnNode, node: node)
        // 429 冷却期间不换路由语义，只给状态行加冷却后缀。
        let mode = context.cloudState == .cooldownRateLimited ? .rateLimited(baseMode) : baseMode
        let cloudLabel = mode.base == .cloud || mode.base == .cloudKeyInvalid
        let nodeOwnsStatus = !cloudLabel && (desired == .node || waitingOnNode)
        let publish = Self.shouldPublishStatus(nodeOwnsStatus: nodeOwnsStatus, mode: mode, last: lastMode)
        if nodeOwnsStatus {
            lastMode = nil
        } else if publish {
            lastMode = mode
        }
        return CloudControlEffect(
            mode: mode,
            nodeTierEnabled: desired != .cloud || waitingOnNode,
            lines: lines,
            discard: discard,
            cancelInflight: cancel,
            publishStatus: publish,
            yieldStatusToNode: nodeOwnsStatus
        )
    }

    /// Tier1 不写状态行。同一云端状态也不每帧重写。
    static func shouldPublishStatus(nodeOwnsStatus: Bool, mode: SubtitleMode, last: SubtitleMode?) -> Bool {
        !nodeOwnsStatus && last != mode
    }

    private func presentationMode(desired: ProviderTier, waitingOnNode: Bool, node: NodeTierFacts) -> SubtitleMode {
        if context.cloudState == .disabledInvalidKey {
            return .cloudKeyInvalid
        }
        if waitingOnNode || (desired == .node && node.activeNodeID != nil) {
            return .hybrid(node.activeNodeID ?? "")
        }
        if desired == .cloud {
            return .cloud
        }
        return .local
    }

    private static func isCancellation(_ error: Error) -> Bool {
        if error is CancellationError { return true }
        if let cloud = error as? CloudClientError, case .cancelled = cloud { return true }
        if let url = error as? URLError, url.code == .cancelled { return true }
        let nsError = error as NSError
        return nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        let root = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("rslite-cloud-upload-\(UUID().uuidString)")
        try? FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        defer { try? FileManager.default.removeItem(at: root) }
        let ledger = QuotaLedger(url: root.appendingPathComponent("quota-ledger.json"))
        let fake = FakeCloud()
        let box = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let controller = CloudUploadController(
            localeID: "de-DE",
            ledger: ledger,
            readSwitch: { box.snapshot },
            makeClient: { key in
                box.seenKeys.append(key)
                return fake
            }
        )
        check(controller.groqLanguageCode == "de", "de-DE 应交给 Groq 语言码 de")
        check(box.seenKeys == ["gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"], "有 key 时应建客户端，且不读环境变量")
        let idle = NodeTierFacts(enabled: true, healthy: true, activeNodeID: nil, isSelf: false)
        let now = Date(timeIntervalSince1970: 1_700_000_000)
        var clock = 0.0
        func push(_ amplitude: Float, _ seconds: Double) -> CloudIngestEffect {
            let count = Int((seconds * 16_000).rounded())
            let effect = controller.ingest(samples: [Float](repeating: amplitude, count: count), startSeconds: clock, now: now, node: idle)
            clock += Double(count) / 16_000
            return effect
        }
        var effect = push(0.05, 2.0)
        effect = push(0, 0.6)
        check(effect.uploads.count == 1, "有 key 时静音收段应上传，实际 \(effect.uploads.count)")
        check(effect.control.mode == .cloud, "有 key 时状态应为云端·Groq，实际 \(effect.control.mode.label)")
        check(effect.control.nodeTierEnabled == false, "云端档不应去连 nodes.json 里的节点")
        guard let segment = effect.uploads.first, let client = controller.prepareSend(segment, now: now) else {
            check(false, "准备发送应返回客户端")
            return failures
        }
        check(client === fake, "发送应走注入的客户端，而不是真网络")
        check(controller.prepareSend(segment, now: now) == nil, "同一段失败前也不许重发")
        let kept = controller.resolveFailure(id: segment.id, error: CloudClientError.network("offline"), now: now, node: idle)
        check(kept.lines.isEmpty && kept.mode == .cloud, "单次失败应留在云端并保留本机版")
        check(controller.attemptCount(segment.id) == 1, "失败段的发送次数应停在 1")

        _ = push(0.05, 2.0)
        let second = push(0, 0.6)
        check(second.uploads.count == 1 && second.uploads.first?.id != segment.id, "下一段仍应上传，且不是失败的那一段")
        if let next = second.uploads.first {
            _ = controller.prepareSend(next, now: now)
            _ = controller.resolveFailure(id: next.id, error: CloudClientError.network("offline"), now: now, node: idle)
        }
        _ = push(0.05, 2.0)
        let third = push(0, 0.6)
        if let next = third.uploads.first {
            _ = controller.prepareSend(next, now: now)
            let fell = controller.resolveFailure(id: next.id, error: CloudClientError.network("offline"), now: now.addingTimeInterval(1), node: idle)
            check(fell.mode == .local, "连续 3 次失败应降到本机，实际 \(fell.mode.label)")
            check(fell.nodeTierEnabled, "降级后应允许节点层接手")
            check(fell.mode != .cloud, "降级后不应继续标云端")
            check(fell.publishStatus == false && fell.yieldStatusToNode, "降级到节点时状态行交给 NodeRouter，云端不写")
        } else {
            check(false, "第三次应变出新段")
        }

        let active = NodeTierFacts(enabled: true, healthy: true, activeNodeID: "mini2", isSelf: false)
        let hybrid = controller.ingest(samples: [Float](repeating: 0, count: 16_000), startSeconds: clock, now: now.addingTimeInterval(2), node: active)
        clock += 1
        check(hybrid.control.mode == .hybrid("mini2"), "节点会话建立后状态应为混合·节点名，实际 \(hybrid.control.mode.label)")
        check(hybrid.control.publishStatus == false, "节点连接期间云端不改写混合/本机")

        if let retryID = third.uploads.first?.id {
            let limited = controller.resolveFailure(
                id: retryID,
                error: CloudClientError.httpStatus(429, retryAfter: 3, xGroqID: nil),
                now: now.addingTimeInterval(3),
                node: active
            )
            check(limited.mode == .rateLimited(.hybrid("mini2")),
                  "429 之后不应停在可上传的云端档，实际 \(limited.mode.label)")
        } else {
            check(false, "429 用例应先有一段已接受的发送")
        }
        let tooSoon = controller.ingest(samples: [Float](repeating: 0.05, count: 16_000), startSeconds: clock, now: now.addingTimeInterval(4), node: idle)
        clock += 1
        check(tooSoon.uploads.isEmpty, "retry-after 未到不应上传")
        _ = controller.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: clock, now: now.addingTimeInterval(6), node: idle)
        clock += 2
        let probed = controller.ingest(samples: [Float](repeating: 0, count: 32_000), startSeconds: clock, now: now.addingTimeInterval(7), node: idle)
        clock += 2
        check(probed.uploads.count == 1, "冷却到期后的下一个段边界应试探一次，实际 \(probed.uploads.count)")
        if let probe = probed.uploads.first {
            _ = controller.prepareSend(probe, now: now.addingTimeInterval(7))
            let back = controller.resolveSuccess(
                id: probe.id,
                result: CloudTranscriptionResult(text: "Hallo", segments: [CloudSegment(t0: probe.audioOrigin, t1: probe.audioOrigin + 1, text: "Hallo")], statusCode: 200, xGroqID: "req", remainingRequests: 1990),
                now: now.addingTimeInterval(7),
                node: active
            )
            check(back.lines.count == 1 && back.lines[0].text == "Hallo", "成功结果应进 P5")
            check(back.mode == .hybrid("mini2"), "试探成功后要等静音点才离开节点，实际 \(back.mode.label)")
            check(ledger.usage(at: now.addingTimeInterval(7)).dayRequests == 10, "remaining-requests 应校正每日请求数")
            check(ledger.usage(at: now.addingTimeInterval(7)).minuteRequests > 0, "每日剩余不应改写 1 分钟窗口")
            let firstSilence = controller.ingest(samples: [Float](repeating: 0, count: 16_000), startSeconds: clock, now: now.addingTimeInterval(8), node: active)
            clock += 1
            check(firstSilence.control.mode == .hybrid("mini2"), "第一次静音点还不应切回云端")
            _ = controller.ingest(samples: [Float](repeating: 0.05, count: 8_000), startSeconds: clock, now: now.addingTimeInterval(9), node: active)
            clock += 0.5
            let secondSilence = controller.ingest(samples: [Float](repeating: 0, count: 16_000), startSeconds: clock, now: now.addingTimeInterval(10), node: active)
            clock += 1
            check(secondSilence.control.mode == .cloud, "第二次静音点应切回云端·Groq，实际 \(secondSilence.control.mode.label)")
            check(secondSilence.control.nodeTierEnabled == false, "切回云端后应放开节点会话")
            check(secondSilence.control.publishStatus, "切回云端时应写一次状态行")
        }

        box.snapshot.enabled = false
        let off = controller.ingest(samples: [Float](repeating: 0, count: 1_600), startSeconds: clock, now: now.addingTimeInterval(11), node: idle)
        check(off.control.cancelInflight, "开关关应取消在飞请求")
        check(fake.cancelled == 1, "开关关应打到 CloudClient.cancelInflightRequests")
        check(off.control.mode == .local, "开关关后状态应回到本机")
        if let stale = probed.uploads.first {
            let discarded = controller.resolveSuccess(
                id: stale.id,
                result: CloudTranscriptionResult(text: "丢弃", segments: [], statusCode: 200, xGroqID: nil, remainingRequests: nil),
                now: now.addingTimeInterval(12),
                node: idle
            )
            check(discarded.discard && discarded.lines.isEmpty, "开关关之后回来的响应应丢弃")
        }

        // F4：换 key 时旧 key 的在飞请求迟到回 401，不能把新 key 打成「云端 key 无效」。
        do {
            let keyA = "gsk_selftest_fake_key_old_abcdefghijklmnopqrstuv"
            let keyB = "gsk_selftest_fake_key_new_abcdefghijklmnopqrstuv"
            let oldFake = FakeCloud()
            let newFake = FakeCloud()
            let rotBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: keyA))
            let rot = CloudUploadController(
                localeID: "de-DE",
                ledger: QuotaLedger(url: root.appendingPathComponent("quota-keyrot.json")),
                readSwitch: { rotBox.snapshot },
                makeClient: { key in key == keyA ? oldFake : newFake }
            )
            var rotClock = 0.0
            func rotPush(_ amplitude: Float, _ seconds: Double) -> CloudIngestEffect {
                let count = Int((seconds * 16_000).rounded())
                let effect = rot.ingest(samples: [Float](repeating: amplitude, count: count), startSeconds: rotClock, now: now, node: idle)
                rotClock += Double(count) / 16_000
                return effect
            }
            _ = rotPush(0.05, 2.0)
            let oldSeg = rotPush(0, 0.6).uploads.first
            if let oldSeg, rot.prepareSend(oldSeg, now: now) === oldFake {
                rotBox.snapshot.key = keyB
                let late = rot.resolveFailure(
                    id: oldSeg.id,
                    error: CloudClientError.httpStatus(401, retryAfter: nil, xGroqID: nil),
                    now: now,
                    node: idle
                )
                check(late.mode != .cloudKeyInvalid, "换 key 后旧 key 的迟到 401 不应把状态打成 key 无效，实际 \(late.mode.label)")
                check(late.discard, "旧 key 的迟到回执应丢弃")
                check(oldFake.cancelled == 1, "换 key 应取消旧 client 的在飞请求，实际 \(oldFake.cancelled)")
                check(newFake.cancelled == 0, "换 key 不应取消新 client")
                _ = rotPush(0.05, 2.0)
                let fresh = rotPush(0, 0.6)
                check(fresh.control.mode == .cloud, "新 key 应照常走云端，实际 \(fresh.control.mode.label)")
                if let next = fresh.uploads.first {
                    check(rot.prepareSend(next, now: now) === newFake, "新 key 的下一段应走新 client")
                } else {
                    check(false, "新 key 的下一段应上传")
                }
            } else {
                check(false, "换 key 用例：旧 key 应先发出一段")
            }
        }

        // Review F5：真正的换 key 必须先丢旧恢复证明，再做发请求前的额度检查。
        do {
            let keyA = "gsk_selftest_quota_key_old_abcdefghijklmnopqrstuv"
            let keyB = "gsk_selftest_quota_key_new_abcdefghijklmnopqrstuv"
            let rotatedLedger = QuotaLedger(url: root.appendingPathComponent("quota-key-before-send.json"), now: now)
            let rotatedSwitch = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: keyA))
            let rotated = CloudUploadController(localeID: "de-DE", ledger: rotatedLedger, readSwitch: { rotatedSwitch.snapshot }, makeClient: { _ in FakeCloud() })
            try rotatedLedger.note(["x-ratelimit-remaining-requests": "5", "x-ratelimit-reset-requests": "100s"], at: now, keyFingerprint: "old-fingerprint", sentAt: now, monotonicNow: 0)
            _ = rotated.ingest(samples: [], startSeconds: 0, now: now, node: idle)
            rotatedSwitch.snapshot.key = keyB
            let unsent = EmittedCloudSegment(id: 987, speechA0: 0, speechA1: 2, audioOrigin: 0, pcm16: Data(repeating: 0, count: 64_000), reason: "selftest")
            check(rotated.prepareSend(unsent, now: now.addingTimeInterval(101)) == nil, "F5：换 key 后发请求前不能确认旧 key 边界并上传")
            check(rotatedLedger.usage(at: now.addingTimeInterval(101)).dayRequests == 1_995, "F5：发请求前换 key 观察不能伪造本地请求或丢高水位")
        } catch {
            failures.append("F5 换 key 发请求前检查异常：\(error)")
        }

        let rateLedger = QuotaLedger(url: root.appendingPathComponent("quota-rate.json"))
        let rateBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let rate = CloudUploadController(
            localeID: "de-DE",
            ledger: rateLedger,
            readSwitch: { rateBox.snapshot },
            makeClient: { _ in FakeCloud() }
        )
        var rateClock = 0.0
        func pushRate(_ amplitude: Float, _ seconds: Double, _ at: Date) -> CloudIngestEffect {
            let count = Int((seconds * 16_000).rounded())
            let effect = rate.ingest(samples: [Float](repeating: amplitude, count: count), startSeconds: rateClock, now: at, node: idle)
            rateClock += Double(count) / 16_000
            return effect
        }
        _ = pushRate(0.05, 2, now)
        let rateClosed = pushRate(0, 0.6, now)
        if let seg = rateClosed.uploads.first, rate.prepareSend(seg, now: now) != nil {
            let limited = rate.resolveFailure(
                id: seg.id,
                error: CloudClientError.httpStatus(429, retryAfter: 3, xGroqID: nil),
                now: now,
                node: idle
            )
            check(limited.mode.base == .local, "429 且没有节点会话时应回到本机，实际 \(limited.mode.label)")
            check(limited.mode.label == DesignTokens.Text.statusLocal + DesignTokens.Text.statusCloudCooldownSuffix,
                  "429 冷却期间状态行应带冷却后缀，实际 \(limited.mode.label)")
            _ = pushRate(0.05, 2, now.addingTimeInterval(2))
            let early = pushRate(0, 0.6, now.addingTimeInterval(2))
            check(early.uploads.isEmpty, "retry-after 整数秒未到不应上传")
            _ = pushRate(0.05, 2, now.addingTimeInterval(3))
            let again = pushRate(0, 0.6, now.addingTimeInterval(3))
            check(again.uploads.count == 1, "retry-after 到期后的下一段应试探一次，实际 \(again.uploads.count)")
        } else {
            check(false, "429 用例应先准备好一段已接受的上传")
        }

        let httpQuotaLedger = QuotaLedger(url: root.appendingPathComponent("quota-http-429.json"))
        let httpQuota = CloudUploadController(
            localeID: "de-DE",
            ledger: httpQuotaLedger,
            readSwitch: { rateBox.snapshot },
            makeClient: { _ in FakeCloud() }
        )
        _ = httpQuota.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: 0, now: now, node: idle)
        let httpClosed = httpQuota.ingest(samples: [Float](repeating: 0, count: 9_600), startSeconds: 2, now: now, node: idle)
        if let segment = httpClosed.uploads.first, httpQuota.prepareSend(segment, now: now) != nil {
            _ = httpQuota.resolveFailure(
                id: segment.id,
                error: CloudClientError.httpStatus(429, retryAfter: 3, xGroqID: nil, quota: CloudQuotaHeaders(remainingRequests: 0, resetRequestsS: 100)),
                now: now,
                node: idle
            )
            check(httpQuotaLedger.usage(at: now).dayRequests == 2_000, "429 错误响应的 remaining 应校正每日额度")
        } else {
            check(false, "带配额头的 429 用例应先准备好一段上传")
        }

        let invalidLedger = QuotaLedger(url: root.appendingPathComponent("quota-invalid.json"))
        let invalidBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let invalid = CloudUploadController(localeID: "de-DE", ledger: invalidLedger, readSwitch: { invalidBox.snapshot }, makeClient: { _ in FakeCloud() })
        var invalidClock = 0.0
        let speech = invalid.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: invalidClock, now: now, node: idle)
        invalidClock += 2
        let closed = invalid.ingest(samples: [Float](repeating: 0, count: 12_800), startSeconds: invalidClock, now: now, node: idle)
        if let seg = closed.uploads.first ?? speech.uploads.first {
            _ = invalid.prepareSend(seg, now: now)
            let bad = invalid.resolveFailure(id: seg.id, error: CloudClientError.httpStatus(401, retryAfter: nil, xGroqID: "x"), now: now, node: idle)
            check(bad.mode == .cloudKeyInvalid, "401 状态行应为云端 key 无效，实际 \(bad.mode.label)")
        } else {
            check(false, "401 用例应先有一段可失败的上传")
        }

        var route = CloudRouteContext(RouteInitial(hasKey: true, cloudEnabled: true, cloudState: .available, nodeEnabled: true, nodeHealthy: true, nodeIsSelf: false, consecutiveFailures: 0, backoffIndex: 0))
        check(TieredProviders.select(route) == .cloud, "Provider 选择应与状态机一致：云端优先")
        route.cloudState = .cooldownBackoff
        check(TieredProviders.select(route) == .node, "云端冷却时应落到节点")
        route.nodeIsSelf = true
        check(TieredProviders.select(route) == .local, "不连自己，节点是自己时落到本机")
        check(CloudUploadController.cloudNodeID == "cloud:groq", "云端 P5 来源标识固定，不写入节点清单")
        check(SubtitleMode.cloud.label == DesignTokens.Text.statusCloud, "云端状态文案")
        check(SubtitleMode.hybrid("mini2").label == DesignTokens.Text.statusHybridPrefix + "mini2", "节点状态文案")
        check(SubtitleMode.local.label == DesignTokens.Text.statusLocal, "本机状态文案")
        check(SubtitleMode.cloudKeyInvalid.label == DesignTokens.Text.statusCloudKeyInvalid, "key 无效文案")
        check(CloudUploadController.cooldownLogLine(status: 429, retryAfter: 3) == "rslite.cloud cooldown reason=429 retry_after=3",
              "冷却日志格式")
        check(SubtitleMode.rateLimited(.local).label == DesignTokens.Text.statusLocal + DesignTokens.Text.statusCloudCooldownSuffix,
              "冷却后缀接在实际模式之后")

        let empty = CloudTranscriptionResult(text: "Nur Text", segments: [], statusCode: 200, xGroqID: nil, remainingRequests: nil)
        let corrected = CloudSpeechBounds(speechA0: 1.2, speechA1: 3.4, audioOrigin: 1.0, pcmCount: 100)
        let fallback = CloudUploadController.p5Lines(result: empty, bounds: corrected)
        check(fallback.count == 1 && abs(fallback[0].t0 - 1.2) < 1e-9 && abs(fallback[0].t1 - 3.4) < 1e-9,
              "没有 Groq 句时间时 P5 用校正后的段界")

        var lastLabel: SubtitleMode? = .cloud
        var published: [String] = []
        let flicker: [(Bool, SubtitleMode)] = [
            (true, .local),
            (true, .hybrid("mini2")),
            (true, .local),
            (true, .hybrid("mini2")),
        ]
        for frame in flicker {
            if Self.shouldPublishStatus(nodeOwnsStatus: frame.0, mode: frame.1, last: lastLabel) {
                published.append(frame.1.label)
                lastLabel = frame.1
            } else if frame.0 {
                lastLabel = nil
            }
        }
        check(published.isEmpty, "降级到节点、节点连接中不出现本机↔混合来回闪")
        check(!Self.shouldPublishStatus(nodeOwnsStatus: false, mode: .cloud, last: .cloud), "同一云端状态不每帧刷")
        check(Self.shouldPublishStatus(nodeOwnsStatus: false, mode: .cloudKeyInvalid, last: .cloud), "key 无效要写状态行")

        let clearFake = FakeCloud()
        let clearBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let clearController = CloudUploadController(
            localeID: "de-DE",
            ledger: QuotaLedger(url: root.appendingPathComponent("quota-clear.json")),
            readSwitch: { clearBox.snapshot },
            makeClient: { _ in clearFake }
        )
        clearBox.snapshot = CloudSwitchSnapshot(hasKey: false, enabled: true, key: nil)
        let cleared = clearController.ingest(samples: [Float](repeating: 0, count: 1_600), startSeconds: 0, now: now, node: idle)
        check(cleared.control.cancelInflight, "清 key 应取消在飞请求")
        check(clearFake.cancelled == 1, "清 key 应在丢掉 client 之前调用 cancelInflightRequests")

        let lateFake = FakeCloud()
        let lateBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let late = CloudUploadController(
            localeID: "de-DE",
            ledger: QuotaLedger(url: root.appendingPathComponent("quota-late.json")),
            readSwitch: { lateBox.snapshot },
            makeClient: { _ in lateFake }
        )
        var lateClock = 0.0
        _ = late.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: lateClock, now: now, node: idle)
        lateClock += 2
        let lateClosed = late.ingest(samples: [Float](repeating: 0, count: 12_800), startSeconds: lateClock, now: now, node: idle)
        if let seg = lateClosed.uploads.first, late.prepareSend(seg, now: now) != nil {
            lateBox.snapshot.enabled = false
            let lateResult = late.resolveSuccess(
                id: seg.id,
                result: CloudTranscriptionResult(text: "迟到", segments: [CloudSegment(t0: 0, t1: 1, text: "迟到")], statusCode: 200, xGroqID: nil, remainingRequests: nil),
                now: now,
                node: idle
            )
            check(lateResult.discard && lateResult.lines.isEmpty, "尾段返回时开关已关则不上屏")
            check(lateFake.cancelled == 1, "resolve 发现开关已关应取消在飞请求")
        } else {
            check(false, "F2 用例应先有一段已接受的上传")
        }

        let keyLateFake = FakeCloud()
        let keyLateBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let keyLate = CloudUploadController(
            localeID: "de-DE",
            ledger: QuotaLedger(url: root.appendingPathComponent("quota-key-late.json")),
            readSwitch: { keyLateBox.snapshot },
            makeClient: { _ in keyLateFake }
        )
        _ = keyLate.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: 0, now: now, node: idle)
        let keyLateClosed = keyLate.ingest(samples: [Float](repeating: 0, count: 12_800), startSeconds: 2, now: now, node: idle)
        if let seg = keyLateClosed.uploads.first, keyLate.prepareSend(seg, now: now) != nil {
            keyLateBox.snapshot = CloudSwitchSnapshot(hasKey: false, enabled: true, key: nil)
            let dropped = keyLate.resolveSuccess(
                id: seg.id,
                result: CloudTranscriptionResult(text: "迟到", segments: [CloudSegment(t0: 0, t1: 1, text: "迟到")], statusCode: 200, xGroqID: nil, remainingRequests: nil),
                now: now,
                node: idle
            )
            check(dropped.discard && dropped.lines.isEmpty, "尾段返回时 key 已清则不上屏")
            check(keyLateFake.cancelled == 1, "resolve 发现 key 已清应先取消在飞请求")
        } else {
            check(false, "清 key 的 F2 用例应先有一段已接受的上传")
        }

        let resumeFake = FakeCloud()
        let resumeBox = SwitchBox(snapshot: CloudSwitchSnapshot(hasKey: true, enabled: true, key: "gsk_selftest_fake_key_abcdefghijklmnopqrstuvwxyz"))
        let resumed = CloudUploadController(
            localeID: "de-DE",
            ledger: QuotaLedger(url: root.appendingPathComponent("quota-resume.json")),
            readSwitch: { resumeBox.snapshot },
            makeClient: { _ in resumeFake }
        )
        var resumeClock = 0.0
        _ = resumed.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: resumeClock, now: now, node: idle)
        resumeClock += 2
        let beforePause = resumed.ingest(samples: [Float](repeating: 0, count: 12_800), startSeconds: resumeClock, now: now, node: idle)
        resumeClock += 0.8
        check(beforePause.uploads.count == 1, "暂停前应有一段可上传")
        resumed.cancelInflight()
        _ = resumed.ingest(samples: [Float](repeating: 0.05, count: 32_000), startSeconds: resumeClock, now: now, node: idle)
        resumeClock += 2
        let afterResume = resumed.ingest(samples: [Float](repeating: 0, count: 12_800), startSeconds: resumeClock, now: now, node: idle)
        check(afterResume.uploads.count == 1, "暂停取消在飞之后，新的段仍应上传")
        if let seg = afterResume.uploads.first {
            check(resumed.prepareSend(seg, now: now) != nil, "恢复后的段应能继续交给客户端")
        } else {
            check(false, "恢复后应发出新段")
        }
        return failures
    }
}

private final class SwitchBox: @unchecked Sendable {
    var snapshot: CloudSwitchSnapshot
    var seenKeys: [String] = []
    init(snapshot: CloudSwitchSnapshot) { self.snapshot = snapshot }
}

private final class FakeCloud: CloudTranscribing, @unchecked Sendable {
    var cancelled = 0
    func transcribe(pcm16Mono16k: Data, a0: Double, duration: Double, language: String, segmentID: Int?) async throws -> CloudTranscriptionResult {
        CloudTranscriptionResult(text: "", segments: [], statusCode: 200, xGroqID: nil, remainingRequests: nil)
    }
    func cancelInflightRequests() { cancelled += 1 }
}
