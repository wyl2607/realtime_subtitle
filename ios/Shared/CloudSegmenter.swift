import Foundation

/// 分段规则从 mac rslite/CloudSegmenter.swift 原样移植（只保留 16k 单声道样本入口，去掉 AVFoundation 转换）：
/// 20ms 帧 RMS ≥ 0.005 算有声、静音 ≥ 0.6s 收段、段长 ≤ 15s 强切、短于 1.5s 且间隔在 (0.02s, 1.5s) 才合并。
/// 这些数字不是旋钮——四端必须对同一段音频切在同一条边界上。
/// 广播扩展只有 ~50MB：trim() 保证缓冲最多覆盖当前段加 0.2s 前后垫，不缓存历史多段。
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
    /// 这一拍末尾是否已处于 ≥0.6s 的静音。上传调度只在静音沿上切层。
    var atSilence: Bool
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
    private let eps = 1e-6

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
            // 缺的样本按静音计。大缺口不把整段零填进缓冲，否则断网几分钟会把内存撑爆。
            if gap >= Self.silenceSeconds - eps {
                silenceRun += gap
                emitted.append(contentsOf: closeOpenIfSilent())
                emitted.append(contentsOf: emitHeldIfMergeWindowPassed())
                emitted.append(contentsOf: flushReady())
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
        return EmittedCloudSegment(
            id: id,
            speechA0: pending.speechA0,
            speechA1: pending.speechA1,
            audioOrigin: start,
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
}
