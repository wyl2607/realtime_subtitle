import Foundation

/// TK-001 词时间分段的纯计算。
///
/// 线上规则以 RFC「分段器两端不同」为准，这里不另设旋钮：末词结束之后静音 ≥0.6s 收段，
/// 单段 ≤15s 强切，短于 1.5s 且与下一段间隔 <1.5s 则合并。
/// 判定门槛也是 RFC / TK 卡片写死的：边界绝对误差 p90 ≤ 0.3s，且段尾判定延迟中位数 ≤ 1.0s。
///
/// 迟到的结果不能把已经到点的收段往回改：决策时刻 = max(知道这个词尾的到达时刻, 词尾+0.6s)。
/// 否则文件结束前才到的 final 会被算成「词尾后 0.6s 就收了」，延迟被量短。
enum ProbeWordTime {
    static let silenceS = 0.6
    static let maxS = 15.0
    static let mergeShortS = 1.5
    static let mergeGapS = 1.5
    static let gateBoundaryP90 = 0.3
    static let gateLatencyP50 = 1.0
    /// 文件放完后再喂这么多静音。直播没有 EOF，收段只能靠静音门槛；不补这段，延迟会被文件结尾截短。
    static let tailS = 2.0
    static let chunkS = 0.1
    /// 回放落后实时超过这个值，延迟分布不能当结论（和 CLAUDE.md 第 7 节「别在抢 GPU 时测延迟」同一类污染）。
    static let paceLagLimitS = 0.2

    struct Word {
        var t0: Double
        var t1: Double
        var nChars: Int
    }

    struct Obs {
        var arrival: Double
        var isFinal: Bool
        var words: [Word]
    }

    struct Seg {
        var a0: Double
        var a1: Double
        var decision: Double
        var reason: String
    }

    struct RefSpan {
        var id: String
        var t0: Double
        var t1: Double
        var source: String
    }

    struct Score {
        var refToPred: [Double]
        var predToRef: [Double]
        var latencies: [Double]
    }

    struct EnergyRow {
        var rms: Double
        var boundaryP90: Double?
        var latencyP50: Double?
        var nPred: Int
        /// 各文件要拼成一条分布再取分位。只留 p90 的话，60 个文件会变成「各文件 p90 的最大值」。
        var score: Score
    }

    private struct OpenHyp {
        var a0: Double
        var last: Double
        var infoAt: Double
    }

    private static let eps = 1e-9

    /// 把一次识别回放里按到达顺序看到的词，收成云端上传用的段。
    /// 宽 span（t0 钉在更早的段首）在静音收段之后不会被当成新句子：它的 t0 过不了「不早于上一段结尾」这道门。
    /// 15s 强切之后例外——同一句还在继续，t0 仍是句首，只能靠 t1 往前长。
    static func segment(_ results: [Obs]) -> [Seg] {
        var open: OpenHyp?
        var closed: [Seg] = []
        var lastReason: String?

        func forceSlice(_ hyp: OpenHyp) -> OpenHyp? {
            var a0 = hyp.a0
            let last = hyp.last
            let info = hyp.infoAt
            while last - a0 >= maxS - eps {
                let cut = a0 + maxS
                closed.append(Seg(a0: a0, a1: cut, decision: max(info, cut), reason: "max"))
                lastReason = "max"
                a0 = cut
            }
            if last - a0 > eps {
                return OpenHyp(a0: a0, last: last, infoAt: info)
            }
            return nil
        }

        func closeSilence() {
            guard let cur = open else {
                return
            }
            open = forceSlice(cur)
            if let left = open {
                closed.append(Seg(
                    a0: left.a0,
                    a1: left.last,
                    decision: max(left.infoAt, left.last + silenceS),
                    reason: "silence"
                ))
                lastReason = "silence"
                open = nil
            }
        }

        func begin(from words: [Word], arrival: Double) {
            let floor = closed.last?.a1 ?? -.infinity
            if lastReason == "max", !words.isEmpty {
                let t0 = words.map(\.t0).min() ?? floor
                let t1 = words.map(\.t1).max() ?? floor
                // 强切没有真静音。宽 span 的 t0 仍停在句首，只要 t1 越过切点就属于下一块；
                // t0 已经落到切点之后一个静音门槛外，则是新的一句，不能再粘回切点。
                if t1 > floor + 1e-6, t0 < floor + silenceS {
                    open = forceSlice(OpenHyp(a0: floor, last: t1, infoAt: arrival))
                    return
                }
            }
            let fresh = words.filter { $0.t0 >= floor - 0.05 }
            for group in groups(of: fresh) {
                if open != nil {
                    closeSilence()
                }
                let g0 = group.map(\.t0).min() ?? 0
                let g1 = group.map(\.t1).max() ?? g0
                open = forceSlice(OpenHyp(a0: g0, last: g1, infoAt: arrival))
            }
        }

        for result in results.sorted(by: { $0.arrival < $1.arrival }) {
            let words = result.words.filter { $0.t1 >= $0.t0 && $0.t0.isFinite && $0.t1.isFinite }
            if words.isEmpty {
                if open != nil, result.arrival >= (open?.last ?? 0) + silenceS {
                    closeSilence()
                }
                continue
            }
            if let cur = open {
                let silenceDue = cur.last + silenceS
                let inTime = result.arrival < silenceDue - eps
                let t0 = words.map(\.t0).min() ?? cur.a0
                if inTime, t0 < cur.last + silenceS {
                    let pieces = groups(of: words)
                    if let first = pieces.first, (first.map(\.t0).min() ?? t0) < cur.last + silenceS {
                        let floor = closed.last?.a1 ?? -.infinity
                        var newA0 = cur.a0
                        if t0 < cur.a0, t0 >= floor - 0.05 {
                            newA0 = t0
                        }
                        let firstEnd = first.map(\.t1).max() ?? cur.last
                        open = forceSlice(OpenHyp(a0: newA0, last: firstEnd, infoAt: result.arrival))
                        for group in pieces.dropFirst() {
                            if open != nil {
                                closeSilence()
                            }
                            let g0 = group.map(\.t0).min() ?? 0
                            let g1 = group.map(\.t1).max() ?? g0
                            open = forceSlice(OpenHyp(a0: g0, last: g1, infoAt: result.arrival))
                        }
                    } else {
                        closeSilence()
                        begin(from: words, arrival: result.arrival)
                    }
                    continue
                }
                closeSilence()
            }
            begin(from: words, arrival: result.arrival)
        }
        closeSilence()
        return merge(closed)
    }

    private static func groups(of words: [Word]) -> [[Word]] {
        let sorted = words.sorted { lhs, rhs in
            if lhs.t0 == rhs.t0 {
                return lhs.t1 < rhs.t1
            }
            return lhs.t0 < rhs.t0
        }
        var out: [[Word]] = []
        var cur: [Word] = []
        for word in sorted {
            if let prev = cur.last, word.t0 - prev.t1 >= silenceS {
                out.append(cur)
                cur = [word]
            } else {
                cur.append(word)
            }
        }
        if !cur.isEmpty {
            out.append(cur)
        }
        return out
    }

    /// 合并发生在收段之后：间隔为 0 的是 15s 强切的接缝，没有静音，不能再粘回去。
    /// 最后一段若仍短于 1.5s，决策要再等到间隔满 1.5s，才知道后面没有可合并的下一段。
    private static func merge(_ segs: [Seg]) -> [Seg] {
        var out: [Seg] = []
        for seg in segs {
            if let last = out.last {
                let gap = seg.a0 - last.a1
                let short = (last.a1 - last.a0) < mergeShortS
                if short, gap > 0.02, gap < mergeGapS, last.reason != "max" {
                    out[out.count - 1] = Seg(a0: last.a0, a1: seg.a1, decision: seg.decision, reason: "silence")
                    continue
                }
            }
            out.append(seg)
        }
        if var last = out.last, (last.a1 - last.a0) < mergeShortS, last.reason != "max" {
            last.decision = max(last.decision, last.a1 + mergeGapS)
            out[out.count - 1] = last
        }
        return out
    }

    static func score(preds: [Seg], refs: [RefSpan]) -> Score {
        let predStarts = preds.map(\.a0)
        let predEnds = preds.map(\.a1)
        let refStarts = refs.map(\.t0)
        let refEnds = refs.map(\.t1)
        var score = Score(refToPred: [], predToRef: [], latencies: [])
        score.refToPred += refStarts.map { nearest($0, predStarts) }
        score.refToPred += refEnds.map { nearest($0, predEnds) }
        score.predToRef += predStarts.map { nearest($0, refStarts) }
        score.predToRef += predEnds.map { nearest($0, refEnds) }
        for ref in refs {
            guard let pred = preds.min(by: { abs($0.a1 - ref.t1) < abs($1.a1 - ref.t1) }) else {
                continue
            }
            score.latencies.append(pred.decision - ref.t1)
        }
        return score
    }

    private static func nearest(_ x: Double, _ ys: [Double]) -> Double {
        guard let y = ys.min(by: { abs($0 - x) < abs($1 - x) }) else {
            return .infinity
        }
        return abs(y - x)
    }

    /// 两个方向都看：只对人工边界会把「一句切成很多段」算成 0 误差。
    static func boundaryP90(_ score: Score) -> Double? {
        let values = score.refToPred + score.predToRef
        let finite = values.filter(\.isFinite)
        guard finite.count == values.count, !finite.isEmpty else {
            return nil
        }
        return RsBench.percentile(finite, 0.9)
    }

    static func verdict(boundaryP90: Double?, latencyP50: Double?, paceLagP90: Double, missingWordTime: Bool) -> (name: String, reason: String) {
        if paceLagP90 > paceLagLimitS {
            return ("incomplete", "回放落后实时 p90 \(fmt(paceLagP90))s，延迟不可用")
        }
        if missingWordTime {
            return ("incomplete", "结果里没有词级 audioTimeRange")
        }
        guard let boundaryP90, let latencyP50 else {
            return ("incomplete", "参照或预测段为空，出不了分布")
        }
        let boundaryOK = boundaryP90 <= gateBoundaryP90
        let latencyOK = latencyP50 <= gateLatencyP50
        if boundaryOK && latencyOK {
            return ("word-time", "边界 p90 \(fmt(boundaryP90))s ≤ \(gateBoundaryP90)s，段尾延迟中位 \(fmt(latencyP50))s ≤ \(gateLatencyP50)s")
        }
        var why: [String] = []
        if !boundaryOK {
            why.append("边界 p90 \(fmt(boundaryP90))s > \(gateBoundaryP90)s")
        }
        if !latencyOK {
            why.append("段尾延迟中位 \(fmt(latencyP50))s > \(gateLatencyP50)s")
        }
        return ("a3-prime", why.joined(separator: "；"))
    }

    static func fmt(_ x: Double) -> String {
        String(format: "%.3f", x)
    }

    /// 句内首尾静音修剪，得到「这一句人声从哪到哪」。
    /// 不是分段器：内部停顿仍算同一句。阈值钉在底噪之上，是因为 FLEURS 有的句子很轻
    /// （帧 RMS 中位约 0.002、底噪约 0.0015），固定 0.02 会把整句切没。
    static func speechExtent(samples: [Float], sampleRate: Double) -> (t0: Double, t1: Double)? {
        let frameS = 0.02
        let frame = max(1, Int(sampleRate * frameS))
        guard samples.count >= frame else {
            return nil
        }
        var rms: [Double] = []
        var index = 0
        while index + frame <= samples.count {
            var sum = 0.0
            for sample in samples[index..<(index + frame)] {
                let x = Double(sample)
                sum += x * x
            }
            rms.append((sum / Double(frame)).squareRoot())
            index += frame
        }
        guard !rms.isEmpty else {
            return nil
        }
        let noise = RsBench.percentile(rms, 0.10)
        let threshold = max(noise * 2.2, noise + 0.0012)
        let minFrames = 4
        var runs: [(Int, Int)] = []
        var runStart: Int?
        for (offset, value) in rms.enumerated() {
            if value >= threshold {
                if runStart == nil {
                    runStart = offset
                }
            } else if let start = runStart {
                runs.append((start, offset))
                runStart = nil
            }
        }
        if let start = runStart {
            runs.append((start, rms.count))
        }
        var first: Int?
        var last: Int?
        for (start, end) in runs where end - start >= minFrames {
            if first == nil {
                first = start
            }
            last = end
        }
        guard let first, let last else {
            return nil
        }
        let duration = Double(samples.count) / sampleRate
        return (Double(first) * frameS, min(duration, Double(last) * frameS))
    }

    static func floats(from samples: ArraySlice<Int16>) -> [Float] {
        samples.map { Float($0) / 32768 }
    }

    /// 在拼接文件里找每一条原始 wav 的起点。接缝是样本级的，用能量猜会把句内轻声吃掉。
    static func findClip(_ needle: [Int16], in haystack: [Int16], from: Int) -> Int? {
        guard !needle.isEmpty, from >= 0 else {
            return nil
        }
        let limit = haystack.count - needle.count
        if from > limit {
            return nil
        }
        let probe = min(800, needle.count)
        let first = needle[0]
        var offset = from
        while offset <= limit {
            if haystack[offset] == first, matches(needle, haystack, at: offset, probe: probe) {
                return offset
            }
            offset += 1
        }
        return nil
    }

    private static func matches(_ needle: [Int16], _ haystack: [Int16], at offset: Int, probe: Int) -> Bool {
        for index in 0..<probe where haystack[offset + index] != needle[index] {
            return false
        }
        if probe == needle.count {
            return true
        }
        for index in probe..<needle.count where haystack[offset + index] != needle[index] {
            return false
        }
        return true
    }

    /// A3' 的能量门限扫描。静音仍是 0.6s（RFC），不在这里另找一个静音值。
    /// 能量路径每个有声帧在帧尾就「到达」，所以它的延迟基本上就是 0.6s；扫的是哪档 RMS 最贴参照边界。
    /// final 校正不在这次扫描里——能量段不会再被 final 的词尾拽回去。
    static let energyThresholds: [Double] = [0.003, 0.005, 0.008, 0.012, 0.02]

    static func energyObservations(samples: [Float], sampleRate: Double, threshold: Double) -> [Obs] {
        let frame = max(1, Int(sampleRate * 0.02))
        var out: [Obs] = []
        var index = 0
        var frameIndex = 0
        while index + frame <= samples.count {
            var sum = 0.0
            for sample in samples[index..<(index + frame)] {
                let x = Double(sample)
                sum += x * x
            }
            let rms = (sum / Double(frame)).squareRoot()
            let t0 = Double(frameIndex) * 0.02
            let t1 = t0 + 0.02
            if rms >= threshold {
                out.append(Obs(
                    arrival: t1,
                    isFinal: true,
                    words: [Word(t0: t0, t1: t1, nChars: 0)]
                ))
            }
            index += frame
            frameIndex += 1
        }
        return out
    }

    static func energySweep(samples: [Float], sampleRate: Double, refs: [RefSpan]) -> [EnergyRow] {
        energyThresholds.map { threshold in
            let preds = segment(energyObservations(samples: samples, sampleRate: sampleRate, threshold: threshold))
            let scored = score(preds: preds, refs: refs)
            return EnergyRow(
                rms: threshold,
                boundaryP90: boundaryP90(scored),
                latencyP50: scored.latencies.isEmpty ? nil : RsBench.percentile(scored.latencies, 0.5),
                nPred: preds.count,
                score: scored
            )
        }
    }

    /// 边界 p90 最小的一档；打平取更低的 RMS（轻声句子不容易被切没）。没有预测段的档不参与。
    static func suggestThreshold(_ rows: [EnergyRow]) -> Double? {
        let usable = rows.filter { $0.nPred > 0 && $0.boundaryP90 != nil }
        return usable.min { lhs, rhs in
            let lp = lhs.boundaryP90 ?? .infinity
            let rp = rhs.boundaryP90 ?? .infinity
            if abs(lp - rp) > 1e-9 {
                return lp < rp
            }
            return lhs.rms < rhs.rms
        }?.rms
    }

    static func selfTest() throws {
        var failures: [String] = []
        func check(_ name: String, _ ok: Bool, _ detail: String) {
            if !ok {
                failures.append("\(name): \(detail)")
            }
        }
        func near(_ value: Double, _ want: Double, tol: Double = 0.03) -> Bool {
            abs(value - want) <= tol
        }

        let continuous = segment(growing(to: 5))
        check("连续语音应是一段", continuous.count == 1, "\(continuous)")
        if let seg = continuous.first {
            check("连续语音 a1", near(seg.a1, 5), "\(seg.a1)")
            check("连续语音决策=词尾+0.6", near(seg.decision, 5.6), "\(seg.decision)")
            check("连续语音原因", seg.reason == "silence", seg.reason)
        }
        let passScore = score(preds: continuous, refs: [RefSpan(id: "a", t0: 0, t1: 5, source: "test")])
        let passBoundary = boundaryP90(passScore)
        let passLatency = RsBench.percentile(passScore.latencies, 0.5)
        let passVerdict = verdict(boundaryP90: passBoundary, latencyP50: passLatency, paceLagP90: 0, missingWordTime: false)
        check("准时词尾应采用词时间", passVerdict.name == "word-time", passVerdict.reason)

        let late = segment([Obs(arrival: 8, isFinal: true, words: [Word(t0: 0, t1: 5, nChars: 1)])])
        check("迟到 final 只有一段", late.count == 1, "\(late)")
        if let seg = late.first {
            check("迟到 final 决策是到达时刻", near(seg.decision, 8), "\(seg.decision)")
        }
        let lateScore = score(preds: late, refs: [RefSpan(id: "a", t0: 0, t1: 5, source: "test")])
        let lateVerdict = verdict(
            boundaryP90: boundaryP90(lateScore),
            latencyP50: RsBench.percentile(lateScore.latencies, 0.5),
            paceLagP90: 0,
            missingWordTime: false
        )
        check("迟到 final 应改走 A3'", lateVerdict.name == "a3-prime", lateVerdict.reason)

        let split = segment(growing(to: 3) + growing(from: 4.2, to: 7))
        check("句间静音应切开", split.count == 2, "\(split)")
        if split.count == 2 {
            check("第二句 a0", near(split[1].a0, 4.2), "\(split[1].a0)")
            check("第二句 a1", near(split[1].a1, 7), "\(split[1].a1)")
            check("第二句决策", near(split[1].decision, 7.6), "\(split[1].decision)")
        }

        var wide = growing(to: 3)
        for end in [4.2, 4.6, 5.2, 6.2, 7.0] {
            wide.append(Obs(arrival: end + 0.05, isFinal: false, words: [Word(t0: 0, t1: end, nChars: 1)]))
        }
        let wideSegs = segment(wide)
        check("宽 span 不重置时切不出第二句", wideSegs.count == 1, "\(wideSegs)")
        let wideScore = score(
            preds: wideSegs,
            refs: [
                RefSpan(id: "a", t0: 0, t1: 3, source: "test"),
                RefSpan(id: "b", t0: 4.2, t1: 7, source: "test"),
            ]
        )
        let wideVerdict = verdict(
            boundaryP90: boundaryP90(wideScore),
            latencyP50: wideScore.latencies.isEmpty ? nil : RsBench.percentile(wideScore.latencies, 0.5),
            paceLagP90: 0,
            missingWordTime: false
        )
        check("漏切第二句应改走 A3'", wideVerdict.name == "a3-prime", wideVerdict.reason)

        var mergeObs = [Obs(arrival: 0.45, isFinal: false, words: [Word(t0: 0, t1: 0.4, nChars: 1)])]
        // 不用 stride(through:by:)：0.4 的二进制误差会把 4.0 丢掉，段尾就停在 3.8。
        for end in [1.4, 1.8, 2.2, 2.6, 3.0, 3.4, 3.8, 4.0] {
            mergeObs.append(Obs(arrival: end + 0.05, isFinal: false, words: [Word(t0: 1.2, t1: end, nChars: 1)]))
        }
        let merged = segment(mergeObs)
        check("短段与下一段间隔 <1.5s 应合并", merged.count == 1, "\(merged)")
        if let seg = merged.first {
            check("合并后 a1", near(seg.a1, 4), "\(seg.a1)")
            check("合并后决策跟后段", near(seg.decision, 4.6), "\(seg.decision)")
        }

        let cut = segment(growing(to: 16))
        check("15s 强切后还有尾段", cut.count == 2, "\(cut)")
        if cut.count == 2 {
            check("强切 a1", near(cut[0].a1, 15, tol: 0.05), "\(cut[0].a1)")
            check("强切原因", cut[0].reason == "max", cut[0].reason)
            check("强切尾段不被粘回去", near(cut[1].a0, 15, tol: 0.05) && near(cut[1].a1, 16, tol: 0.05), "\(cut[1])")
        }

        let restarted = segment(growing(to: 15) + growing(from: 17, to: 19))
        check("强切后隔开的新句从新 t0 起", restarted.count == 2, "\(restarted)")
        if restarted.count == 2 {
            check("新句 a0", near(restarted[1].a0, 17), "\(restarted[1].a0)")
            check("新句 a1", near(restarted[1].a1, 19), "\(restarted[1].a1)")
        }

        let gapped = segment([Obs(
            arrival: 9,
            isFinal: true,
            words: [
                Word(t0: 0, t1: 1, nChars: 1),
                Word(t0: 1, t1: 3, nChars: 1),
                Word(t0: 4.2, t1: 5, nChars: 1),
                Word(t0: 5, t1: 7, nChars: 1),
            ]
        )])
        check("同一次 final 里的词间静音要切开", gapped.count == 2, "\(gapped)")
        if gapped.count == 2 {
            check("final 两段决策都是到达时刻", near(gapped[0].decision, 9) && near(gapped[1].decision, 9), "\(gapped)")
        }

        let isolated = segment([Obs(arrival: 0.5, isFinal: false, words: [Word(t0: 0, t1: 0.4, nChars: 1)])])
        check("孤立短段要等满合并窗口", isolated.count == 1 && near(isolated[0].decision, 1.9), "\(isolated)")

        let lagVerdict = verdict(boundaryP90: 0, latencyP50: 0.6, paceLagP90: 0.5, missingWordTime: false)
        check("回放落后不能当通过", lagVerdict.name == "incomplete", lagVerdict.reason)

        var samples = [Float](repeating: 0, count: 16000)
        samples += [Float](repeating: 0.05, count: 48000)
        samples += [Float](repeating: 0, count: 16000)
        let extent = speechExtent(samples: samples, sampleRate: 16000)
        check("能量修剪起点", extent != nil && near(extent?.t0 ?? -1, 1, tol: 0.05), "\(String(describing: extent))")
        check("能量修剪终点", extent != nil && near(extent?.t1 ?? -1, 4, tol: 0.05), "\(String(describing: extent))")

        let clip = [Int16](repeating: 7, count: 400)
        let haystack = clip + [Int16](repeating: 0, count: 50) + clip
        let first = findClip(clip, in: haystack, from: 0)
        let second = findClip(clip, in: haystack, from: (first ?? 0) + clip.count)
        check("PCM 对齐", first == 0 && second == clip.count + 50, "\(String(describing: first)) \(String(describing: second))")

        let refs = [RefSpan(id: "s", t0: extent?.t0 ?? 0, t1: extent?.t1 ?? 0, source: "test")]
        let sweep = energySweep(samples: samples, sampleRate: 16000, refs: refs)
        let suggested = suggestThreshold(sweep)
        check("能量扫描能给出门限", suggested != nil, "\(sweep)")
        if let suggested, let row = sweep.first(where: { $0.rms == suggested }) {
            check("建议门限的边界 p90 可用", (row.boundaryP90 ?? 9) <= gateBoundaryP90, "\(row)")
        }

        check("门槛常量未被改写", gateBoundaryP90 == 0.3 && gateLatencyP50 == 1.0 && silenceS == 0.6, "")

        if !failures.isEmpty {
            throw BenchError.missing(failures.joined(separator: "\n"))
        }
    }

    private static func growing(from start: Double = 0, to end: Double, step: Double = 0.2, lag: Double = 0.05) -> [Obs] {
        var out: [Obs] = []
        var cursor = start
        while cursor < end - eps {
            cursor = min(end, ((cursor + step) * 100000).rounded() / 100000)
            out.append(Obs(
                arrival: cursor + lag,
                isFinal: false,
                words: [Word(t0: start, t1: cursor, nChars: 1)]
            ))
        }
        return out
    }
}
