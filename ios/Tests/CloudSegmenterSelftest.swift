import Foundation

/// CloudSegmenter 自测：静音收段、最长段强切、尾部 flush、短段合并与能量门限。
/// 数值取自 mac rslite 的同一组用例，切点必须逐毫秒对齐。
func runCloudSegmenterSelftest(_ check: @escaping (Bool, String) -> Void) {
    let sampleRate = CloudSegmenter.sampleRate

    func feed(
        _ segmenter: CloudSegmenter,
        _ amplitude: Float,
        _ seconds: Double,
        _ clock: inout Double
    ) -> [EmittedCloudSegment] {
        let count = Int((seconds * sampleRate).rounded())
        let samples = [Float](repeating: amplitude, count: count)
        let output = segmenter.append(samples: samples, startSeconds: clock)
        clock += Double(count) / sampleRate
        return output.segments
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
            return
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
            check(abs(segment.speechA0) < 0.03 && abs(segment.speechA1 - 1.6) < 0.03,
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
        let quiet = segmenter.append(
            samples: [Float](repeating: 0.004, count: Int((0.8 * sampleRate).rounded())),
            startSeconds: 0
        )
        check(quiet.segments.isEmpty, "0.004 低于 0.005，不是语音")
        check(quiet.atSilence, "0.004 持续 0.8s 应算静音")
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
        check(tail.first?.reason == "silence", "流结束前的短段仍按静音收段")
    }
}
