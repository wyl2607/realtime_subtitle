import AVFoundation
import CoreMedia
import Foundation
import Speech

/// TK-001 探针入口。真机测量（SpeechTranscriber）由协调者在桌面会话里跑；
/// `--selftest` 只跑分段数学，不碰麦克风、不加载识别模型。
enum ProbeSeg {
    struct Args {
        var values: [String: String]
        var flags: Set<String>
    }

    static func run(_ args: [String]) async throws {
        let parsed = try parse(args)
        if parsed.flags.contains("--selftest") {
            try ProbeWordTime.selfTest()
            print("probe-seg selftest: ok")
            return
        }
        if #available(macOS 27.0, *) {
            try await Live.run(parsed)
        } else {
            throw BenchError.unsupported("probe-seg 需要 macOS 27 的 SpeechTranscriber（与 rsbench asr 相同）。")
        }
    }

    static func parse(_ args: [String]) throws -> Args {
        var values: [String: String] = [:]
        var flags: Set<String> = []
        var index = 0
        while index < args.count {
            let key = args[index]
            guard key.hasPrefix("--") else {
                throw BenchError.usage("unexpected argument: \(key)")
            }
            if key == "--selftest" {
                flags.insert(key)
                index += 1
                continue
            }
            guard index + 1 < args.count else {
                throw BenchError.usage("missing value for \(key)")
            }
            values[key] = args[index + 1]
            index += 2
        }
        return Args(values: values, flags: flags)
    }
}

@available(macOS 27.0, *)
private enum Live {
    struct Hit {
        var fileID: String
        var isFinal: Bool
        var arrival: Double
        var rangeT0: Double?
        var rangeT1: Double?
        var finalization: Double?
        var words: [ProbeWordTime.Word]
    }

    actor Log {
        var hits: [Hit] = []
        var lags: [Double] = []
        var timedOut = false
        func add(_ hit: Hit) { hits.append(hit) }
        func addLag(_ value: Double) { lags.append(value) }
        func markTimedOut() { timedOut = true }
    }

    static func run(_ args: ProbeSeg.Args) async throws {
        let outPath = try RsBench.require(args.values, "--out")
        let localeID = try RsBench.require(args.values, "--locale")
        let limit = args.values["--n"].flatMap(Int.init)
        let bounds = try args.values["--bounds"].map { try loadBounds(URL(fileURLWithPath: $0)) }
        let wavPath = args.values["--wav"]
        let wavDir = args.values["--wav-dir"]
        let clipsDir = args.values["--clips"]
        if wavPath != nil && wavDir != nil {
            throw BenchError.usage("pass only one of --wav and --wav-dir")
        }
        if clipsDir != nil && wavPath == nil {
            throw BenchError.usage("--clips 只和 --wav 一起用（拼接文件 + 各句 wav）")
        }

        let locale = Locale(identifier: localeID)
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw BenchError.unsupported("unsupported speech locale: \(localeID)")
        }
        try await RsBench.ensureSpeechAssets(locale: supported)

        let outURL = URL(fileURLWithPath: outPath)
        try Data().write(to: outURL)
        let handle = try FileHandle(forWritingTo: outURL)
        defer { try? handle.close() }

        var files: [(id: String, url: URL)] = []
        if let wavDir {
            let refs = try RsBench.readRefs(URL(fileURLWithPath: try RsBench.require(args.values, "--refs")))
            let chosen = limit.map { Array(refs.prefix($0)) } ?? refs
            let dir = URL(fileURLWithPath: wavDir, isDirectory: true)
            for ref in chosen {
                let url = dir.appendingPathComponent(ref.id).appendingPathExtension("wav")
                if !FileManager.default.fileExists(atPath: url.path) {
                    throw BenchError.missing("missing wav for \(ref.id): \(url.path)")
                }
                files.append((ref.id, url))
            }
        } else if let wavPath {
            files = [("concat", URL(fileURLWithPath: wavPath))]
        } else {
            throw BenchError.usage(RsBench.usage)
        }

        var refToPred: [Double] = []
        var predToRef: [Double] = []
        var latencies: [Double] = []
        var lags: [Double] = []
        var volatileWidths: [Double] = []
        var finalWidths: [Double] = []
        var nResults = 0
        var nVolatile = 0
        var nFinal = 0
        var nMissingWords = 0
        var nRef = 0
        var nPred = 0
        var refSource = "none"
        var sweepRows: [ProbeWordTime.EnergyRow] = []
        var clockSkew: [Double] = []

        for (ordinal, file) in files.enumerated() {
            fputs("probe-seg: \(file.id) \(ordinal + 1)/\(files.count)\n", stderr)
            let pcm = try loadMonoS16(file.url)
            let refs = try references(
                fileID: file.id,
                pcm: pcm,
                singleWav: wavPath != nil && wavDir == nil,
                clipsDir: clipsDir,
                refsPath: args.values["--refs"],
                limit: limit,
                bounds: bounds
            )
            refSource = refs.first?.source ?? refSource
            let played = try await transcribe(
                url: file.url,
                fileID: file.id,
                pcm: pcm,
                locale: supported
            )
            lags += played.lags
            clockSkew.append(played.analyzerDuration - pcm.duration)

            for ref in refs {
                nRef += 1
                try RsBench.writeJSONLine([
                    "ev": "ref",
                    "file": file.id,
                    "id": ref.id,
                    "t0": RsBench.round3(ref.t0),
                    "t1": RsBench.round3(ref.t1),
                    "source": ref.source,
                ], to: handle)
            }
            for hit in played.hits {
                nResults += 1
                if hit.isFinal {
                    nFinal += 1
                } else {
                    nVolatile += 1
                }
                if hit.words.isEmpty {
                    nMissingWords += 1
                }
                for word in hit.words {
                    let width = word.t1 - word.t0
                    if hit.isFinal {
                        finalWidths.append(width)
                    } else {
                        volatileWidths.append(width)
                    }
                }
                try writeHit(hit, to: handle)
            }

            let obs = played.hits.map {
                ProbeWordTime.Obs(arrival: $0.arrival, isFinal: $0.isFinal, words: $0.words)
            }
            let preds = ProbeWordTime.segment(obs)
            nPred += preds.count
            for seg in preds {
                try RsBench.writeJSONLine([
                    "ev": "segment",
                    "file": file.id,
                    "a0": RsBench.round3(seg.a0),
                    "a1": RsBench.round3(seg.a1),
                    "decision_s": RsBench.round3(seg.decision),
                    "reason": seg.reason,
                ], to: handle)
            }
            let scored = ProbeWordTime.score(preds: preds, refs: refs)
            refToPred += scored.refToPred
            predToRef += scored.predToRef
            latencies += scored.latencies

            let floats = ProbeWordTime.floats(from: pcm.samples[...])
            let sweep = ProbeWordTime.energySweep(samples: floats, sampleRate: pcm.sampleRate, refs: refs)
            if sweepRows.isEmpty {
                sweepRows = sweep
            } else {
                sweepRows = mergeSweep(sweepRows, sweep)
            }
            let lagP90 = played.lags.isEmpty ? 0 : RsBench.percentile(played.lags, 0.9)
            fputs("probe-seg: \(file.id) done results=\(played.hits.count) segs=\(preds.count) lag_p90=\(ProbeWordTime.fmt(lagP90))\n", stderr)
        }

        let pooled = ProbeWordTime.Score(refToPred: refToPred, predToRef: predToRef, latencies: latencies)
        let boundary = ProbeWordTime.boundaryP90(pooled)
        let latencyP50 = latencies.isEmpty ? nil : RsBench.percentile(latencies, 0.5)
        let latencyP90 = latencies.isEmpty ? nil : RsBench.percentile(latencies, 0.9)
        let lagP90 = lags.isEmpty ? 0 : RsBench.percentile(lags, 0.9)
        let missingAll = nResults > 0 && nMissingWords == nResults
        let skew = clockSkew.map(abs).max() ?? 0
        var verdict = ProbeWordTime.verdict(
            boundaryP90: boundary,
            latencyP50: latencyP50,
            paceLagP90: lagP90,
            missingWordTime: missingAll
        )
        if skew > 0.05, verdict.name != "incomplete" {
            verdict = ("incomplete", "识别时钟和文件时长差了 \(ProbeWordTime.fmt(skew))s，边界对不齐")
        }
        let suggestion = ProbeWordTime.suggestThreshold(sweepRows)

        var summary: [String: Any] = [
            "ev": "summary",
            "n_files": files.count,
            "n_results": nResults,
            "n_volatile": nVolatile,
            "n_final": nFinal,
            "n_results_without_word_time": nMissingWords,
            "n_ref": nRef,
            "n_pred": nPred,
            "ref_source": refSource,
            "volatile_run_width_p50": stat(volatileWidths, 0.5),
            "volatile_run_width_p90": stat(volatileWidths, 0.9),
            "final_run_width_p50": stat(finalWidths, 0.5),
            "final_run_width_p90": stat(finalWidths, 0.9),
            "boundary_ref_to_pred_p50": stat(refToPred.filter(\.isFinite), 0.5),
            "boundary_ref_to_pred_p90": stat(refToPred.filter(\.isFinite), 0.9),
            "boundary_pred_to_ref_p50": stat(predToRef.filter(\.isFinite), 0.5),
            "boundary_pred_to_ref_p90": stat(predToRef.filter(\.isFinite), 0.9),
            "boundary_p90": boundary.map(RsBench.round3) ?? NSNull(),
            "end_latency_p50": latencyP50.map(RsBench.round3) ?? NSNull(),
            "end_latency_p90": latencyP90.map(RsBench.round3) ?? NSNull(),
            "pace_lag_p90": RsBench.round3(lagP90),
            "clock_skew_s": RsBench.round3(skew),
            "gate_boundary_s": ProbeWordTime.gateBoundaryP90,
            "gate_latency_s": ProbeWordTime.gateLatencyP50,
            "verdict": verdict.name,
            "verdict_reason": verdict.reason,
            "a3_suggest_rms": suggestion.map { $0 } ?? NSNull(),
            "a3_energy_sweep": sweepRows.map { row -> [String: Any] in
                [
                    "rms": row.rms,
                    "boundary_p90": row.boundaryP90.map(RsBench.round3) ?? NSNull(),
                    "latency_p50": row.latencyP50.map(RsBench.round3) ?? NSNull(),
                    "n_pred": row.nPred,
                ]
            },
            "percentile": "nearest-rank index=Int((n-1)*q+0.5)，与 rsbench 既有 percentile 相同",
        ]
        if suggestion == nil {
            summary["a3_suggest_rms"] = NSNull()
        }
        try RsBench.writeJSONLine(summary, to: handle)
        fputs("probe-seg: verdict \(verdict.name) \(verdict.reason)\n", stderr)
    }

    /// 多文件时把每一档的原始误差拼成一条分布，再取 p90 / 中位。
    /// 先取各文件 p90 再取最大，会被单句离群点抬高，A3' 的门限建议会跟着偏。
    private static func mergeSweep(_ lhs: [ProbeWordTime.EnergyRow], _ rhs: [ProbeWordTime.EnergyRow]) -> [ProbeWordTime.EnergyRow] {
        zip(lhs, rhs).map { left, right in
            let score = ProbeWordTime.Score(
                refToPred: left.score.refToPred + right.score.refToPred,
                predToRef: left.score.predToRef + right.score.predToRef,
                latencies: left.score.latencies + right.score.latencies
            )
            return ProbeWordTime.EnergyRow(
                rms: left.rms,
                boundaryP90: ProbeWordTime.boundaryP90(score),
                latencyP50: score.latencies.isEmpty ? nil : RsBench.percentile(score.latencies, 0.5),
                nPred: left.nPred + right.nPred,
                score: score
            )
        }
    }

    private static func stat(_ values: [Double], _ q: Double) -> Any {
        guard !values.isEmpty else {
            return NSNull()
        }
        return RsBench.round3(RsBench.percentile(values, q))
    }

    private static func writeHit(_ hit: Hit, to handle: FileHandle) throws {
        var record: [String: Any] = [
            "ev": "result",
            "file": hit.fileID,
            "kind": hit.isFinal ? "final" : "volatile",
            "arrival_s": RsBench.round3(hit.arrival),
            "words": hit.words.map { word -> [String: Any] in
                ["t0": RsBench.round3(word.t0), "t1": RsBench.round3(word.t1), "n": word.nChars]
            },
        ]
        record["range_t0"] = hit.rangeT0.map(RsBench.round3) ?? NSNull()
        record["range_t1"] = hit.rangeT1.map(RsBench.round3) ?? NSNull()
        record["finalization_s"] = hit.finalization.map(RsBench.round3) ?? NSNull()
        try RsBench.writeJSONLine(record, to: handle)
    }

    private static func references(
        fileID: String,
        pcm: PCM,
        singleWav: Bool,
        clipsDir: String?,
        refsPath: String?,
        limit: Int?,
        bounds: [Bound]?
    ) throws -> [ProbeWordTime.RefSpan] {
        if let bounds {
            if singleWav {
                return bounds.map {
                    ProbeWordTime.RefSpan(id: $0.id ?? fileID, t0: $0.t0, t1: $0.t1, source: "bounds")
                }
            }
            let matched = bounds.filter { $0.id == fileID }
            if matched.isEmpty {
                throw BenchError.missing("bounds 里没有 \(fileID)。逐文件模式的每一行都要有 id")
            }
            return matched.map {
                ProbeWordTime.RefSpan(id: fileID, t0: $0.t0, t1: $0.t1, source: "bounds")
            }
        }
        if singleWav {
            guard let clipsDir, let refsPath else {
                throw BenchError.usage("拼接文件要给 --clips 和 --refs（按原始 wav 对齐句界），或者给 --bounds")
            }
            return try alignReferences(
                haystack: pcm.samples,
                sampleRate: pcm.sampleRate,
                clipsDir: URL(fileURLWithPath: clipsDir, isDirectory: true),
                refs: RsBench.readRefs(URL(fileURLWithPath: refsPath)),
                limit: limit
            )
        }
        let floats = ProbeWordTime.floats(from: pcm.samples[...])
        if let extent = ProbeWordTime.speechExtent(samples: floats, sampleRate: pcm.sampleRate) {
            return [ProbeWordTime.RefSpan(id: fileID, t0: extent.t0, t1: extent.t1, source: "acoustic-trim")]
        }
        return [ProbeWordTime.RefSpan(id: fileID, t0: 0, t1: pcm.duration, source: "file-extent")]
    }

    /// concat5 这类文件是若干句 wav 直接拼接出来的。参照边界 = 每一句在拼接轴上的人声起止，
    /// 不是能量 VAD 自己切出来的段——否则被测的词时间分段是在和自己的近亲比。
    private static func alignReferences(
        haystack: [Int16],
        sampleRate: Double,
        clipsDir: URL,
        refs: [RefLine],
        limit: Int?
    ) throws -> [ProbeWordTime.RefSpan] {
        let chosen = limit.map { Array(refs.prefix($0)) } ?? refs
        var cursor = 0
        var spans: [ProbeWordTime.RefSpan] = []
        for ref in chosen {
            let url = clipsDir.appendingPathComponent(ref.id).appendingPathExtension("wav")
            let clip = try loadMonoS16(url)
            guard let offset = ProbeWordTime.findClip(clip.samples, in: haystack, from: cursor) else {
                throw BenchError.missing("在拼接音频里找不到 \(ref.id)（从样本 \(cursor) 往后）")
            }
            let piece = Array(clip.samples)
            let floats = ProbeWordTime.floats(from: piece[...])
            let origin = Double(offset) / sampleRate
            if let extent = ProbeWordTime.speechExtent(samples: floats, sampleRate: clip.sampleRate) {
                spans.append(ProbeWordTime.RefSpan(
                    id: ref.id,
                    t0: origin + extent.t0,
                    t1: origin + extent.t1,
                    source: "clip-align+acoustic-trim"
                ))
            } else {
                spans.append(ProbeWordTime.RefSpan(
                    id: ref.id,
                    t0: origin,
                    t1: origin + clip.duration,
                    source: "clip-align"
                ))
            }
            cursor = offset + clip.samples.count
        }
        return spans
    }

    private struct Bound {
        var id: String?
        var t0: Double
        var t1: Double
    }

    /// JSONSerialization 的数字是 NSNumber。写成整数的 0 不能只靠 `as? Double`，否则手写的 bounds 整行被拒。
    private static func jsonNumber(_ value: Any?) -> Double? {
        if value is Bool {
            return nil
        }
        if let number = value as? NSNumber {
            return number.doubleValue
        }
        return nil
    }

    private static func loadBounds(_ url: URL) throws -> [Bound] {
        let text = try String(contentsOf: url, encoding: .utf8)
        var out: [Bound] = []
        for (lineNo, raw) in text.split(whereSeparator: \.isNewline).enumerated() {
            let line = String(raw).trimmingCharacters(in: .whitespaces)
            if line.isEmpty {
                continue
            }
            let object = try JSONSerialization.jsonObject(with: Data(line.utf8))
            guard let dict = object as? [String: Any] else {
                throw BenchError.usage("\(url.path):\(lineNo + 1) 不是 JSON 对象")
            }
            guard let t0 = jsonNumber(dict["t0"]), let t1 = jsonNumber(dict["t1"]), t1 > t0 else {
                throw BenchError.usage("\(url.path):\(lineNo + 1) 需要 t0 < t1")
            }
            let id: String?
            if let text = dict["id"] as? String {
                id = text
            } else if let number = dict["id"] as? Int {
                id = String(number)
            } else if let number = dict["id"] as? NSNumber, dict["id"] is NSNumber {
                id = number.stringValue
            } else {
                id = nil
            }
            out.append(Bound(id: id, t0: t0, t1: t1))
        }
        if out.isEmpty {
            throw BenchError.missing("bounds 文件是空的: \(url.path)")
        }
        return out
    }

    struct PCM {
        var samples: [Int16]
        var sampleRate: Double
        var duration: Double { Double(samples.count) / sampleRate }
    }

    /// 探针只接受 16 kHz mono s16。素材本来就是这个格式；在这里做隐式重采样会让样本时钟和参照对不齐。
    static func loadMonoS16(_ url: URL) throws -> PCM {
        // processingFormat 默认总是 Float32，要按 Int16 打开才能原样拿到 s16 样本。
        let file = try AVAudioFile(forReading: url, commonFormat: .pcmFormatInt16, interleaved: false)
        let format = file.processingFormat
        guard file.fileFormat.commonFormat == .pcmFormatInt16, format.channelCount == 1, format.sampleRate == 16000 else {
            throw BenchError.unsupported(
                "\(url.lastPathComponent) 需要 16 kHz mono s16，实际 \(format.sampleRate) Hz / \(format.channelCount) ch / \(format.commonFormat.rawValue)"
            )
        }
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(file.length)) else {
            throw BenchError.missing("无法为 \(url.lastPathComponent) 分配缓冲")
        }
        try file.read(into: buffer)
        let count = Int(buffer.frameLength)
        guard let channel = buffer.int16ChannelData else {
            throw BenchError.missing("\(url.lastPathComponent) 没有 int16 数据")
        }
        let samples = Array(UnsafeBufferPointer(start: channel[0], count: count))
        return PCM(samples: samples, sampleRate: format.sampleRate)
    }

    struct Played {
        var hits: [Hit]
        var lags: [Double]
        var analyzerDuration: Double
    }

    /// 按墙钟把样本喂进去，到达时刻才是「听到这里时结果才来」。
    /// 整段一次 analyzeSequence(from:) 会跑得比实时快，延迟会变成负数或只剩计算时间。
    static func transcribe(url: URL, fileID: String, pcm: PCM, locale: Locale) async throws -> Played {
        let transcriber = SpeechTranscriber(
            locale: locale,
            transcriptionOptions: [],
            // 与 rslite Pipeline 同一组选项。少了 fastResults，量到的就不是准备上线的那条路径。
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )
        let sourceFormat = AVAudioFormat(
            commonFormat: .pcmFormatFloat32,
            sampleRate: pcm.sampleRate,
            channels: 1,
            interleaved: false
        )
        guard let sourceFormat else {
            throw BenchError.missing("无法建立 16 kHz float 格式")
        }
        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: [transcriber],
            considering: sourceFormat
        ) else {
            throw BenchError.unsupported("找不到 SpeechAnalyzer 能吃的格式")
        }
        let floats = ProbeWordTime.floats(from: pcm.samples[...])
        var rendered = try render(floats, sourceFormat: sourceFormat, analyzerFormat: analyzerFormat)
        let analyzerRate = analyzerFormat.sampleRate
        let fileFrames = rendered.count
        let tailFrames = Int(analyzerRate * ProbeWordTime.tailS)
        rendered += [Float](repeating: 0, count: tailFrames)

        let log = Log()
        let cursor = Cursor()
        let (stream, continuation) = AsyncStream<AnalyzerInput>.makeStream()
        let budget = pcm.duration + ProbeWordTime.tailS + 60

        let producer = Task { () -> Void in
            // 超时取消或组缓冲失败时也要结束流，否则 analyzeSequence 会一直等下一块。
            defer { continuation.finish() }
            let clock = ContinuousClock()
            let started = clock.now
            let chunkFrames = max(1, Int(analyzerRate * ProbeWordTime.chunkS))
            var frame = 0
            var first = true
            while frame < rendered.count {
                if Task.isCancelled {
                    break
                }
                let end = min(rendered.count, frame + chunkFrames)
                let startS = Double(frame) / analyzerRate
                let deadline = started.advanced(by: .seconds(startS))
                let now = clock.now
                if now < deadline {
                    try await Task.sleep(until: deadline, clock: clock)
                } else {
                    await log.addLag(deadline.duration(to: now).seconds)
                }
                await cursor.set(Double(end) / analyzerRate)
                let buffer = try makeBuffer(
                    rendered[frame..<end],
                    format: analyzerFormat
                )
                if first {
                    let start = CMTime(seconds: 0, preferredTimescale: 1_000_000)
                    continuation.yield(AnalyzerInput(buffer: buffer, bufferStartTime: start))
                    first = false
                } else {
                    continuation.yield(AnalyzerInput(buffer: buffer))
                }
                frame = end
            }
        }

        let resultTask = Task { () -> Void in
            for try await result in transcriber.results {
                let arrival = await cursor.now()
                await log.add(hit(from: result, fileID: fileID, arrival: arrival))
            }
        }

        let analyzer = SpeechAnalyzer(modules: [transcriber])
        let watchdog = Task {
            try await Task.sleep(for: .seconds(budget))
            await log.markTimedOut()
            producer.cancel()
            resultTask.cancel()
            await analyzer.cancelAndFinishNow()
        }

        do {
            _ = try await analyzer.analyzeSequence(stream)
            try await analyzer.finalizeAndFinishThroughEndOfInput()
            try await resultTask.value
            watchdog.cancel()
            // producer 里的 sleep / makeBuffer 会抛错；取消时也要 finish 流，见任务开头的 defer。
            try await producer.value
        } catch {
            watchdog.cancel()
            producer.cancel()
            resultTask.cancel()
            if await log.timedOut {
                throw BenchError.unsupported("probe-seg 超时（\(fileID)）：识别流在回放结束 \(Int(budget - pcm.duration))s 后还没关")
            }
            throw error
        }

        if await log.timedOut {
            throw BenchError.unsupported("probe-seg 超时（\(fileID)）")
        }
        return Played(
            hits: await log.hits,
            lags: await log.lags,
            analyzerDuration: Double(fileFrames) / analyzerRate
        )
    }

    private static func hit(from result: SpeechTranscriber.Result, fileID: String, arrival: Double) -> Hit {
        let text = result.text
        var words: [ProbeWordTime.Word] = []
        for run in text.runs {
            guard let range = run.audioTimeRange else {
                continue
            }
            guard let t0 = seconds(range.start), let t1 = seconds(range.end), t1 >= t0 else {
                continue
            }
            let nChars = text.characters.distance(from: run.range.lowerBound, to: run.range.upperBound)
            words.append(ProbeWordTime.Word(t0: t0, t1: t1, nChars: nChars))
        }
        return Hit(
            fileID: fileID,
            isFinal: result.isFinal,
            arrival: arrival,
            rangeT0: seconds(result.range.start),
            rangeT1: seconds(result.range.end),
            finalization: seconds(result.resultsFinalizationTime),
            words: words
        )
    }

    private static func seconds(_ time: CMTime) -> Double? {
        guard time.isValid, time.isNumeric else {
            return nil
        }
        let value = time.seconds
        return value.isFinite ? value : nil
    }

    private static func render(_ samples: [Float], sourceFormat: AVAudioFormat, analyzerFormat: AVAudioFormat) throws -> [Float] {
        if analyzerFormat.sampleRate == sourceFormat.sampleRate,
           analyzerFormat.channelCount == 1,
           analyzerFormat.commonFormat == .pcmFormatFloat32 {
            return samples
        }
        guard let source = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: AVAudioFrameCount(samples.count)) else {
            throw BenchError.missing("无法分配源缓冲")
        }
        source.frameLength = AVAudioFrameCount(samples.count)
        if let channel = source.floatChannelData {
            for (index, sample) in samples.enumerated() {
                channel[0][index] = sample
            }
        }
        guard let converter = AVAudioConverter(from: sourceFormat, to: analyzerFormat) else {
            throw BenchError.unsupported("无法转换到识别格式")
        }
        let ratio = analyzerFormat.sampleRate / sourceFormat.sampleRate
        let capacity = AVAudioFrameCount(Double(samples.count) * ratio + 1024)
        guard let output = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: capacity) else {
            throw BenchError.missing("无法分配识别缓冲")
        }
        var fed = false
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, outStatus in
            if fed {
                outStatus.pointee = .endOfStream
                return nil
            }
            fed = true
            outStatus.pointee = .haveData
            return source
        }
        if let error {
            throw error
        }
        if status == .error {
            throw BenchError.unsupported("音频转换失败")
        }
        return extractMono(output)
    }

    private static func extractMono(_ buffer: AVAudioPCMBuffer) -> [Float] {
        let count = Int(buffer.frameLength)
        if let channel = buffer.floatChannelData {
            return Array(UnsafeBufferPointer(start: channel[0], count: count))
        }
        if let channel = buffer.int16ChannelData {
            return (0..<count).map { Float(channel[0][$0]) / 32768 }
        }
        return []
    }

    private static func makeBuffer(_ samples: ArraySlice<Float>, format: AVAudioFormat) throws -> AVAudioPCMBuffer {
        guard let buffer = AVAudioPCMBuffer(pcmFormat: format, frameCapacity: AVAudioFrameCount(samples.count)) else {
            throw BenchError.missing("无法分配分块缓冲")
        }
        buffer.frameLength = AVAudioFrameCount(samples.count)
        if let channel = buffer.floatChannelData {
            for (index, sample) in samples.enumerated() {
                channel[0][index] = sample
            }
            return buffer
        }
        if let channel = buffer.int16ChannelData {
            for (index, sample) in samples.enumerated() {
                let clipped = max(-1, min(1, sample))
                channel[0][index] = Int16(clipped * 32767)
            }
            return buffer
        }
        throw BenchError.unsupported("识别格式既不是 float32 也不是 int16")
    }
}

private actor Cursor {
    private var fed = 0.0
    func set(_ value: Double) { fed = value }
    func now() -> Double { fed }
}
