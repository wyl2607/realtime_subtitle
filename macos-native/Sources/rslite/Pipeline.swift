@preconcurrency import AVFoundation
import CoreMedia
import Foundation
@preconcurrency import Speech
@preconcurrency import Translation

enum PipelineError: Error, CustomStringConvertible {
    case unsupported(String)
    case unavailable(String)

    var description: String {
        switch self {
        case .unsupported(let message), .unavailable(let message):
            return message
        }
    }
}

struct PipelineConfig: Sendable {
    let sourceSpec: String
    let sourceLocaleID: String
    let targetLanguageID: String
}

struct PipelineCallbacks: Sendable {
    var onVolatile: @Sendable (String) -> Void = { _ in }
    /// t0/t1：句子在统一样本时钟（AudioFanout）上的起止秒数；拿不到时为 nil，这样的句子不参与替换（RFC P4）。
    var onFinal: @Sendable (Int, String, Double?, Double?) -> Void = { _, _, _, _ in }
    var onTranslation: @Sendable (Int, String) -> Void = { _, _ in }
    /// 本机翻译自动重试一次后仍失败：句子 id + 原因（"error" / "unavailable"＝语言包未装）。
    /// 该句进入失败句缓冲，之后可经 retryTranslation(id:) 手动重试。
    var onTranslationFailed: @Sendable (Int, String) -> Void = { _, _ in }
    var onStatus: @Sendable (String) -> Void = { _ in }
    var onFinished: @Sendable () -> Void = {}
    /// 每轮识别启动前调用：NodeClient 在这里 subscribe，必须赶在 fanout.start() 之前订阅，
    /// 否则漏掉开头的音频；暂停/恢复会重新创建订阅，所以每轮都会回调。
    var onFanoutReady: @Sendable (AudioFanout) -> Void = { _ in }
}

@available(macOS 27.0, *)
final class Pipeline: @unchecked Sendable {
    private let config: PipelineConfig
    private let callbacks: PipelineCallbacks
    private let translator: TranslationQueue

    /// 一路采集的统一出口。本机 B 和节点客户端都从这里订阅，共用同一个样本时钟。
    let fanout: AudioFanout
    private var analyzer: SpeechAnalyzer?
    private var resultTask: Task<Void, Never>?
    private var analysisTask: Task<Void, Never>?
    private var finishContinuation: CheckedContinuation<Void, Never>?
    private var nextID = 1
    // 只在 resultTask 里串行读写
    private var committer = SentenceCommitter()
    private var didCheckAssets = false
    private var isPaused = false
    private var isStopped = false
    private var hasFinished = false

    init(config: PipelineConfig, callbacks: PipelineCallbacks) {
        self.config = config
        self.callbacks = callbacks
        fanout = AudioFanout(sourceSpec: config.sourceSpec)
        translator = TranslationQueue(
            sourceID: config.sourceLocaleID,
            targetID: config.targetLanguageID,
            onStatus: callbacks.onStatus,
            onTranslation: callbacks.onTranslation,
            onTranslationFailed: callbacks.onTranslationFailed
        )
    }

    func start() async throws {
        isStopped = false
        isPaused = false
        hasFinished = false
        try await startRun()
    }

    func pause() async {
        isPaused = true
        await stopCurrentRun()
        callbacks.onStatus("已暂停")
    }

    func resume() async {
        guard isPaused, !isStopped else {
            return
        }
        do {
            isPaused = false
            try await startRun()
            callbacks.onStatus("已继续")
        } catch {
            callbacks.onStatus("恢复失败：\(error.localizedDescription)")
        }
    }

    func stop() async {
        isStopped = true
        await stopCurrentRun()
        translator.cancel()
        finishContinuation?.resume()
        finishContinuation = nil
    }

    func waitUntilFinished() async {
        guard !hasFinished else {
            return
        }
        await withCheckedContinuation { continuation in
            finishContinuation = continuation
        }
    }

    private func startRun() async throws {
        let locale = Locale(identifier: config.sourceLocaleID)
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            throw PipelineError.unsupported("不支持识别语言：\(config.sourceLocaleID)")
        }

        let transcriber = SpeechTranscriber(
            locale: supported,
            transcriptionOptions: [],
            // fastResults：FLEURS 5 句实时回放，草稿首字 4–12s → 0.1–1.8s，定稿 4.8–6.7s → 2.2–4.4s，
            // 本进程 CPU 0.42s → 0.59s/71s。和 Whisper 版同时跑会抢资源而跟不上实时（实测 117s 才跑完 67s 音频）
            reportingOptions: [.volatileResults, .fastResults],
            // audioTimeRange：每个词带起止时间，换算成句子的 [t0,t1] 供混合替换使用
            attributeOptions: [.audioTimeRange]
        )

        if !didCheckAssets {
            try await ensureSpeechAssets(transcriber: transcriber)
            didCheckAssets = true
        }
        translator.warmUpIfIdle()

        let stream = fanout.subscribe()
        callbacks.onFanoutReady(fanout)
        do {
            try fanout.start()
        } catch {
            fanout.stop()
            throw error
        }
        guard let sourceFormat = fanout.format else {
            fanout.stop()
            throw PipelineError.unavailable("音频来源没有给出格式")
        }
        let modules: [any SpeechModule] = [transcriber]
        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules,
            considering: sourceFormat
        ) else {
            fanout.stop()
            throw PipelineError.unavailable("找不到兼容的识别音频格式")
        }

        let analyzer = SpeechAnalyzer(modules: modules)
        committer = SentenceCommitter()
        self.analyzer = analyzer

        resultTask = Task { [weak self] in
            guard let self else {
                return
            }
            do {
                for try await result in transcriber.results {
                    await self.accept(result)
                }
            } catch {
                self.callbacks.onStatus("识别结果读取失败：\(error.localizedDescription)")
            }
        }

        analysisTask = Task { [weak self, stream, sourceFormat, analyzerFormat, analyzer] in
            guard let self else {
                return
            }
            do {
                let inputs = AnalyzerInputSequence(
                    sourceStream: stream,
                    sourceFormat: sourceFormat,
                    analyzerFormat: analyzerFormat
                )
                _ = try await analyzer.analyzeSequence(inputs)
                try await analyzer.finalizeAndFinishThroughEndOfInput()
                await self.waitForResults()
                await self.finishRun()
            } catch is CancellationError {
                await analyzer.cancelAndFinishNow()
            } catch {
                self.callbacks.onStatus("识别失败：\(error.localizedDescription)")
                await self.finishRun()
            }
        }
    }

    private func stopCurrentRun() async {
        fanout.stop()
        analysisTask?.cancel()
        resultTask?.cancel()
        if let analyzer {
            await analyzer.cancelAndFinishNow()
        }
        analyzer = nil
        analysisTask = nil
        resultTask = nil
    }

    private func finishRun() async {
        fanout.stop()
        analyzer = nil
        analysisTask = nil
        await translator.waitUntilIdle()
        if !isPaused && !isStopped {
            hasFinished = true
            finishContinuation?.resume()
            finishContinuation = nil
            callbacks.onFinished()
        }
    }

    private func waitForResults() async {
        let task = resultTask
        await task?.value
        resultTask = nil
    }

    private func accept(_ result: SpeechTranscriber.Result) async {
        let raw = String(result.text.characters)
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return
        }
        let spans = Self.timedSpans(of: result.text, trimmedLeading: raw.prefix { $0.isWhitespace || $0.isNewline }.count)

        if result.isFinal {
            let sentences = committer.final(text)
            commit(sentences, text: text, spans: spans)
            callbacks.onVolatile("")
        } else {
            translator.warmUpIfIdle()
            let (sentences, remainder) = committer.volatile(text)
            commit(sentences, text: text, spans: spans)
            callbacks.onVolatile(remainder)
        }
    }

    private func commit(_ sentences: [String], text: String, spans: [TimedSpan]) {
        let times = SentenceTiming.times(for: sentences, in: text, spans: spans)
        for (sentence, time) in zip(sentences, times) {
            commit(sentence, t0: time?.t0, t1: time?.t1)
        }
    }

    /// 把识别结果里每个带 audioTimeRange 的 run 转成字符偏移（相对去掉首部空白后的文字）+ 秒。
    /// 时间基准：analyzer 的输入第一块带了 AudioFanout 的时钟读数，之后顺着数，
    /// 所以这里读到的秒数本身就在统一样本时钟上，不需要再换算。
    private static func timedSpans(of text: AttributedString, trimmedLeading: Int) -> [TimedSpan] {
        var spans: [TimedSpan] = []
        for run in text.runs {
            guard let range = run.audioTimeRange else {
                continue
            }
            let start = text.characters.distance(from: text.startIndex, to: run.range.lowerBound) - trimmedLeading
            let length = text.characters.distance(from: run.range.lowerBound, to: run.range.upperBound)
            let t0 = range.start.seconds
            let t1 = range.end.seconds
            guard t0.isFinite, t1.isFinite else {
                continue
            }
            spans.append(TimedSpan(start: start, end: start + length, t0: t0, t1: t1))
        }
        return spans
    }

    private func commit(_ sentence: String, t0: Double?, t1: Double?) {
        let id = nextID
        nextID += 1
        callbacks.onFinal(id, sentence, t0, t1)
        translator.enqueue(id: id, text: sentence)
    }

    /// 云端句不走本机 Speech 的句子编号。调用方自备不与本机 id 冲突的 key，译文仍从 onTranslation 回来。
    func enqueueTranslation(id: Int, text: String) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        translator.enqueue(id: id, text: trimmed)
    }

    /// 手动重试一条翻译失败句。id 不在最近 50 条失败句里返回 false。
    @discardableResult
    func retryTranslation(id: Int) -> Bool {
        translator.retryTranslation(id: id)
    }

    /// 失败句结局 + 手动重试的纯逻辑自检：注入翻译闭包，不联网、不建窗口、不真等。
    static func selfTest() async -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }

        // 失败句环形缓冲上限 50，超出丢最旧
        let store = FailedSentenceStore()
        for id in 1...60 {
            store.record(id: id, text: "sentence-\(id)")
        }
        check(store.count == FailedSentenceStore.capacity, "失败句环形缓冲上限为 50")
        check(store.text(for: 1) == nil, "超出上限后最旧的失败句被丢弃")
        check(store.text(for: 60) == "sentence-60", "最新的失败句仍保留在缓冲里")

        final class Probe: @unchecked Sendable {
            private let lock = NSLock()
            private var attempts = 0
            private var translations: [Int: String] = [:]
            private var reported: [(Int, String)] = []

            func nextAttempt() -> Int { lock.withLockValue { attempts += 1; return attempts } }
            func record(id: Int, text: String) { lock.withLockVoid { translations[id] = text } }
            func report(id: Int, reason: String) { lock.withLockVoid { reported.append((id, reason)) } }
            func attemptCount() -> Int { lock.withLockValue { attempts } }
            func translation(id: Int) -> String? { lock.withLockValue { translations[id] } }
            func reportedList() -> [(Int, String)] { lock.withLockValue { reported } }
        }

        // 第一次失败自动重试一次，第二次成功 -> 走 onTranslation，不上报失败
        let retryProbe = Probe()
        let retryQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in },
            onTranslation: { id, text in retryProbe.record(id: id, text: text) },
            onTranslationFailed: { id, reason in retryProbe.report(id: id, reason: reason) },
            attempt: { text in
                if retryProbe.nextAttempt() == 1 {
                    throw PipelineError.unavailable("临时失败")
                }
                return "译文:\(text)"
            },
            sleep: { _ in }
        )
        retryQueue.enqueue(id: 7, text: "Hallo")
        await retryQueue.waitUntilIdle()
        check(retryProbe.translation(id: 7) == "译文:Hallo", "第一次失败重试成功：译文经 onTranslation 回来")
        check(retryProbe.reportedList().isEmpty, "重试成功后不得调用 onTranslationFailed")
        check(retryProbe.attemptCount() == 2, "自动重试只发生一次（共两次尝试）")
        check(retryQueue.retryTranslation(id: 999) == false, "retryTranslation 未知 id 返回 false")

        // 两次都失败 -> 以 unavailable 上报一次，并留待手动重试
        let failProbe = Probe()
        let failQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in },
            onTranslation: { id, text in failProbe.record(id: id, text: text) },
            onTranslationFailed: { id, reason in failProbe.report(id: id, reason: reason) },
            attempt: { _ in
                _ = failProbe.nextAttempt()
                throw PipelineError.unsupported("语言包未安装")
            },
            sleep: { _ in }
        )
        failQueue.enqueue(id: 9, text: "Guten Tag")
        await failQueue.waitUntilIdle()
        let failReported = failProbe.reportedList()
        check(failReported.count == 1 && failReported[0].0 == 9 && failReported[0].1 == TranslationQueue.reasonUnavailable,
              "语言包未安装上报一次，原因为 unavailable")
        check(failProbe.attemptCount() == 1, "语言包未安装是确定性失败：不重试，只尝试一次")
        check(failProbe.translation(id: 9) == nil, "失败句不上屏译文")

        // 手动重试：已知失败 id 返回 true，成功后从失败缓冲移除
        let manualProbe = Probe()
        let manualQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in },
            onTranslation: { id, text in manualProbe.record(id: id, text: text) },
            onTranslationFailed: { id, reason in manualProbe.report(id: id, reason: reason) },
            attempt: { text in
                if manualProbe.nextAttempt() <= 2 {
                    throw PipelineError.unavailable("暂时失败")
                }
                return "手动:\(text)"
            },
            sleep: { _ in }
        )
        manualQueue.enqueue(id: 11, text: "Text")
        await manualQueue.waitUntilIdle()
        check(manualProbe.reportedList().count == 1, "自动重试后仍失败：上报一次")
        check(manualQueue.retryTranslation(id: 11) == true, "已知失败 id 返回 true")
        await manualQueue.waitUntilIdle()
        check(manualProbe.translation(id: 11) == "手动:Text", "手动重试成功走 onTranslation")
        check(manualQueue.retryTranslation(id: 11) == false, "成功后该 id 从失败缓冲移除")

        final class WarmProbe: @unchecked Sendable {
            private let lock = NSLock()
            private var attempts: [String] = []
            private var translations = 0
            private var failures = 0
            private var statuses = 0

            func attempt(_ text: String) { lock.withLockVoid { attempts.append(text) } }
            func translation() { lock.withLockVoid { translations += 1 } }
            func failure() { lock.withLockVoid { failures += 1 } }
            func status() { lock.withLockVoid { statuses += 1 } }
            func attemptedTexts() -> [String] { lock.withLockValue { attempts } }
            func callbackCounts() -> (translations: Int, failures: Int, statuses: Int) {
                lock.withLockValue { (translations, failures, statuses) }
            }
        }

        final class TestClock: @unchecked Sendable {
            private let lock = NSLock()
            private var value: UInt64 = 0

            func now() -> UInt64 { lock.withLockValue { value } }
            func advance(seconds: UInt64) {
                lock.withLockVoid { value += seconds * 1_000_000_000 }
            }
        }

        final class AsyncGate: @unchecked Sendable {
            private let lock = NSLock()
            private var isOpen = false
            private var waiter: CheckedContinuation<Void, Never>?

            func wait() async {
                await withCheckedContinuation { continuation in
                    let resumeNow = lock.withLockValue {
                        if isOpen { return true }
                        waiter = continuation
                        return false
                    }
                    if resumeNow { continuation.resume() }
                }
            }

            func open() {
                let continuation = lock.withLockValue {
                    isOpen = true
                    defer { waiter = nil }
                    return waiter
                }
                continuation?.resume()
            }
        }

        // 预热只为让 translationd 在定稿前装入模型，不能污染任何正式句回调。
        let warmClock = TestClock()
        let warmProbe = WarmProbe()
        let warmQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in warmProbe.status() },
            onTranslation: { _, _ in warmProbe.translation() },
            onTranslationFailed: { _, _ in warmProbe.failure() },
            attempt: { text in
                warmProbe.attempt(text)
                return "预热:\(text)"
            },
            sleep: { _ in },
            clock: { warmClock.now() }
        )
        warmQueue.warmUpIfIdle()
        await warmQueue.waitUntilIdle()
        check(warmProbe.attemptedTexts() == ["Hallo."], "从未翻译时应排入一次预热")
        let warmCallbacks = warmProbe.callbackCounts()
        check(warmCallbacks.translations == 0 && warmCallbacks.failures == 0 && warmCallbacks.statuses == 0,
              "预热成功不得触发正式回调或状态行")
        warmQueue.warmUpIfIdle()
        await warmQueue.waitUntilIdle()
        check(warmProbe.attemptedTexts().count == 1, "预热刚完成时不应重复预热")
        warmClock.advance(seconds: 61)
        warmQueue.warmUpIfIdle()
        await warmQueue.waitUntilIdle()
        check(warmProbe.attemptedTexts().count == 2, "闲置超过 60 秒后应再次预热")

        let warmErrorProbe = WarmProbe()
        let warmErrorQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in warmErrorProbe.status() },
            onTranslation: { _, _ in warmErrorProbe.translation() },
            onTranslationFailed: { _, _ in warmErrorProbe.failure() },
            attempt: { text in
                warmErrorProbe.attempt(text)
                throw PipelineError.unsupported("语言包未安装")
            },
            sleep: { _ in },
            clock: { 0 }
        )
        warmErrorQueue.warmUpIfIdle()
        await warmErrorQueue.waitUntilIdle()
        let warmErrorCallbacks = warmErrorProbe.callbackCounts()
        check(warmErrorProbe.attemptedTexts() == ["Hallo."], "预热错误仍只尝试一次")
        check(warmErrorCallbacks.translations == 0 && warmErrorCallbacks.failures == 0 && warmErrorCallbacks.statuses == 0,
              "预热错误必须静默")

        let pendingGate = AsyncGate()
        let pendingProbe = WarmProbe()
        let pendingQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in pendingProbe.status() },
            onTranslation: { _, _ in pendingProbe.translation() },
            onTranslationFailed: { _, _ in pendingProbe.failure() },
            attempt: { text in
                pendingProbe.attempt(text)
                await pendingGate.wait()
                return "正式:\(text)"
            },
            sleep: { _ in },
            clock: { 0 }
        )
        pendingQueue.enqueue(id: 13, text: "formal")
        pendingQueue.warmUpIfIdle()
        pendingGate.open()
        await pendingQueue.waitUntilIdle()
        check(pendingProbe.attemptedTexts() == ["formal"], "有 pending 时不得排入预热")

        let formalClock = TestClock()
        let formalProbe = WarmProbe()
        let formalQueue = TranslationQueue(
            sourceID: "de-DE",
            targetID: "zh-Hans",
            onStatus: { _ in formalProbe.status() },
            onTranslation: { _, _ in formalProbe.translation() },
            onTranslationFailed: { _, _ in formalProbe.failure() },
            attempt: { text in
                formalProbe.attempt(text)
                return "正式:\(text)"
            },
            sleep: { _ in },
            clock: { formalClock.now() }
        )
        formalQueue.enqueue(id: 14, text: "formal")
        await formalQueue.waitUntilIdle()
        formalQueue.warmUpIfIdle()
        await formalQueue.waitUntilIdle()
        check(formalProbe.attemptedTexts() == ["formal"], "正式翻译完成后 60 秒内不得预热")

        return failures
    }

    private func ensureSpeechAssets(transcriber: SpeechTranscriber) async throws {
        let status = await AssetInventory.status(forModules: [transcriber])
        callbacks.onStatus("识别资源状态：\(status)")
        guard status != .installed else {
            return
        }
        guard let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) else {
            return
        }

        let progress = request.progress
        let reporter = Task { [callbacks] in
            while !progress.isFinished {
                callbacks.onStatus(String(format: "识别模型下载中：%.0f%%", progress.fractionCompleted * 100))
                try? await Task.sleep(for: .seconds(1))
            }
        }

        do {
            try await request.downloadAndInstall()
            reporter.cancel()
            callbacks.onStatus("识别模型已安装")
        } catch {
            reporter.cancel()
            throw error
        }
    }

    fileprivate static func convert(
        _ sourceBuffer: AVAudioPCMBuffer,
        using converter: AVAudioConverter,
        to outputFormat: AVAudioFormat
    ) throws -> AVAudioPCMBuffer {
        let ratio = outputFormat.sampleRate / sourceBuffer.format.sampleRate
        let capacity = max(1, AVAudioFrameCount(Double(sourceBuffer.frameLength) * ratio) + 1024)
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            throw PipelineError.unavailable("无法分配转换后的音频缓冲区")
        }

        var didProvideInput = false
        var convertError: NSError?
        let status = converter.convert(to: output, error: &convertError) { _, outStatus in
            if didProvideInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            didProvideInput = true
            outStatus.pointee = .haveData
            return sourceBuffer
        }

        if let convertError {
            throw convertError
        }
        if status == .error {
            throw PipelineError.unavailable("音频格式转换失败")
        }
        return output
    }
}

@available(macOS 27.0, *)
struct AnalyzerInputSequence: AsyncSequence, Sendable {
    typealias Element = AnalyzerInput
    typealias AsyncIterator = Iterator

    private let sourceStream: SendableTimedStream
    private let sourceFormat: AVAudioFormat
    private let analyzerFormat: AVAudioFormat

    init(sourceStream: AsyncStream<TimedAudio>, sourceFormat: AVAudioFormat, analyzerFormat: AVAudioFormat) {
        self.sourceStream = SendableTimedStream(stream: sourceStream)
        self.sourceFormat = sourceFormat
        self.analyzerFormat = analyzerFormat
    }

    func makeAsyncIterator() -> Iterator {
        Iterator(
            sourceIterator: sourceStream.stream.makeAsyncIterator(),
            converter: AVAudioConverter(from: sourceFormat, to: analyzerFormat),
            analyzerFormat: analyzerFormat
        )
    }

    struct Iterator: AsyncIteratorProtocol {
        var sourceIterator: AsyncStream<TimedAudio>.Iterator
        let converter: AVAudioConverter?
        let analyzerFormat: AVAudioFormat
        // 第一块显式带起点（统一样本时钟）；之后让 analyzer 自己顺着数，
        // 重采样每块的帧数会差几个样本，逐块都标时间反而会出现微小重叠/空洞。
        // 例外：B 路订阅是 bufferingNewest，慢了会丢块，analyzer 照旧顺着数就会把
        // 之后所有时间整体前移（t0/t1 与节点对不上）。所以用 ContinuityTracker 检测
        // 跳变，跳变的那一块重新显式打起点。
        var continuity = ContinuityTracker()

        mutating func next() async throws -> AnalyzerInput? {
            guard let converter else {
                throw PipelineError.unavailable("无法创建音频格式转换器")
            }
            while let timed = await sourceIterator.next() {
                let verdict = continuity.observe(start: timed.startSeconds, duration: timed.duration)
                if let gap = verdict.gapSeconds {
                    // 丢块后 converter 里可能还扣着丢块前的尾样本，不清掉会被打上丢块后的时间戳。
                    // 只打时长，不含任何识别文字
                    converter.reset()
                    FileHandle.standardError.write(Data(
                        "rslite: B 路丢块，重设时间戳（跳变 \(String(format: "%.3f", gap))s）\n".utf8))
                }
                let converted = try Pipeline.convert(timed.buffer, using: converter, to: analyzerFormat)
                if converted.frameLength > 0 {
                    // 待打的起点一直带到第一块非空输出（重采样器可能先扣着样本、输出为空）
                    if let startSeconds = continuity.takePendingExplicitStart() {
                        let start = CMTime(seconds: startSeconds, preferredTimescale: 1_000_000)
                        return AnalyzerInput(buffer: converted, bufferStartTime: start)
                    }
                    return AnalyzerInput(buffer: converted)
                }
            }
            return nil
        }
    }
}

/// 检测送进 analyzer 的音频块在统一样本时钟上是否连续。
/// 预期起点 = 上一块起点 + 上一块时长（都在源采样率侧算，不受重采样帧数误差影响）。
/// 容差取 10ms（一个 10ms 帧）：样本时钟是整数帧累加，正常连续时偏差只有浮点误差
/// （远小于 1ms）；真正丢一块至少丢掉一个 10ms 以上的缓冲，容差再大就会漏检短丢块，
/// 再小则可能被浮点噪声误报。
struct ContinuityTracker {
    static let tolerance = 0.010

    struct Verdict {
        var needsExplicitStart: Bool
        /// 仅当检测到跳变时有值：实际起点 - 预期起点（秒）。
        var gapSeconds: Double?
    }

    private var expectedNext: Double?
    /// 下一块非空输出要显式设的 bufferStartTime（首块或跳变块的起点）。
    /// 在 takePendingExplicitStart() 取走前一直保留：转换后若输出为空，这次重设不能丢。
    /// 期间又检测到新跳变就以最新起点覆盖。
    private var pendingExplicitStart: Double?

    mutating func observe(start: Double, duration: Double) -> Verdict {
        defer { expectedNext = start + duration }
        guard let expected = expectedNext else {
            pendingExplicitStart = start
            return Verdict(needsExplicitStart: true, gapSeconds: nil)
        }
        let gap = start - expected
        if abs(gap) > Self.tolerance {
            pendingExplicitStart = start
            return Verdict(needsExplicitStart: true, gapSeconds: gap)
        }
        return Verdict(needsExplicitStart: pendingExplicitStart != nil, gapSeconds: nil)
    }

    /// 取走待设的显式起点（取一次清空）；没有待设就返回 nil。
    mutating func takePendingExplicitStart() -> Double? {
        defer { pendingExplicitStart = nil }
        return pendingExplicitStart
    }
}

struct SendableTimedStream: @unchecked Sendable {
    let stream: AsyncStream<TimedAudio>
}

/// 翻译失败句的环形缓冲：最多保留最近 50 条（id → 原文），供 retryTranslation 手动重试；
/// 超出上限丢最旧的一条。只做记账，不碰 Translation 框架，便于 selftest 直接验证上限语义。
final class FailedSentenceStore: @unchecked Sendable {
    static let capacity = 50

    private let lock = NSLock()
    private var order: [Int] = []
    private var texts: [Int: String] = [:]

    var count: Int {
        lock.withLockValue { order.count }
    }

    func text(for id: Int) -> String? {
        lock.withLockValue { texts[id] }
    }

    func record(id: Int, text: String) {
        lock.withLockVoid {
            if texts[id] != nil {
                order.removeAll { $0 == id }
            }
            texts[id] = text
            order.append(id)
            while order.count > Self.capacity {
                let oldest = order.removeFirst()
                texts.removeValue(forKey: oldest)
            }
        }
    }

    func remove(id: Int) {
        lock.withLockVoid {
            guard texts.removeValue(forKey: id) != nil else {
                return
            }
            order.removeAll { $0 == id }
        }
    }
}

@available(macOS 26.0, *)
final class TranslationQueue: @unchecked Sendable {
    /// onTranslationFailed 的原因码，与 spec 003 P2 约定一致。
    static let reasonError = "error"
    static let reasonUnavailable = "unavailable"
    private static let warmIdleSeconds: UInt64 = 60
    private static let warmIdleNanoseconds = warmIdleSeconds * 1_000_000_000

    private let source: Locale.Language
    private let target: Locale.Language
    private let key: String
    private let onStatus: @Sendable (String) -> Void
    private let onTranslation: @Sendable (Int, String) -> Void
    private let onTranslationFailed: @Sendable (Int, String) -> Void
    /// 可注入的翻译尝试 / 重试间隔 / 等待：selftest 用它绕开 Translation 框架（不联网、不真等）。
    private let overrideAttempt: (@Sendable (String) async throws -> String)?
    private let retryDelay: Duration
    private let sleep: @Sendable (Duration) async -> Void
    private let clock: @Sendable () -> UInt64
    private let failures = FailedSentenceStore()

    private var sessions: [String: TranslationSession] = [:]
    private let queue = DispatchQueue(label: "rslite.translation.queue")
    private var chain = Task<Void, Never> {}
    private var pending = 0
    private var lastCompletion: UInt64?
    private var idleWaiters: [CheckedContinuation<Void, Never>] = []
    private var cancelled = false
    /// 只在串行 chain 里读写；翻译失败对状态行只说一次，失败句结局一律走 onTranslationFailed。
    private var didAnnounceFailure = false

    init(
        sourceID: String,
        targetID: String,
        onStatus: @escaping @Sendable (String) -> Void,
        onTranslation: @escaping @Sendable (Int, String) -> Void,
        onTranslationFailed: @escaping @Sendable (Int, String) -> Void = { _, _ in },
        attempt: (@Sendable (String) async throws -> String)? = nil,
        retryDelay: Duration = .seconds(1),
        sleep: (@Sendable (Duration) async -> Void)? = nil,
        clock: @escaping @Sendable () -> UInt64 = { DispatchTime.now().uptimeNanoseconds }
    ) {
        source = Locale.Language(identifier: sourceID)
        target = Locale.Language(identifier: targetID)
        key = "\(sourceID)->\(targetID)"
        self.onStatus = onStatus
        self.onTranslation = onTranslation
        self.onTranslationFailed = onTranslationFailed
        overrideAttempt = attempt
        self.retryDelay = retryDelay
        self.sleep = sleep ?? { try? await Task.sleep(for: $0) }
        self.clock = clock
    }

    func enqueue(id: Int, text: String) {
        queue.sync {
            guard !cancelled else {
                return
            }
            scheduleLocked { [weak self] in
                await self?.translateOne(id: id, text: text)
            }
        }
    }

    /// 只在闲置时排入串行队列，避免定稿翻译与预热争用同一条链。
    func warmUpIfIdle() {
        queue.sync {
            guard !cancelled, pending == 0, shouldWarmUpLocked() else {
                return
            }
            scheduleLocked { [weak self] in
                await self?.warmUp()
            }
        }
    }

    /// 手动重试一条失败句。id 不在最近 50 条失败句里返回 false。
    @discardableResult
    func retryTranslation(id: Int) -> Bool {
        queue.sync {
            guard !cancelled, let text = failures.text(for: id) else {
                return false
            }
            scheduleLocked { [weak self] in
                await self?.retryOne(id: id, text: text)
            }
            return true
        }
    }

    private func scheduleLocked(_ work: @escaping @Sendable () async -> Void) {
        pending += 1
        let previous = chain
        chain = Task { [weak self] in
            await previous.value
            await work()
            self?.completeOne()
        }
    }

    private func translateOne(id: Int, text: String) async {
        var lastError: Error?
        for pass in 0..<2 {
            do {
                let output = try await perform(text)
                failures.remove(id: id)
                onTranslation(id, output)
                return
            } catch {
                lastError = error
                // 语言包没装是确定性失败，重试只会让串行队列里后面每一句都多等 1 秒。
                if Self.reason(for: error) == Self.reasonUnavailable {
                    break
                }
                if pass == 0 {
                    await sleep(retryDelay)
                }
            }
        }
        let reason = Self.reason(for: lastError)
        failures.record(id: id, text: text)
        announceFailure(reason: reason)
        onTranslationFailed(id, reason)
    }

    private func retryOne(id: Int, text: String) async {
        do {
            let output = try await perform(text)
            failures.remove(id: id)
            onTranslation(id, output)
        } catch {
            let reason = Self.reason(for: error)
            failures.record(id: id, text: text)
            onTranslationFailed(id, reason)
        }
    }

    /// 预热失败不代表正式句失败；正式翻译仍沿用原有错误处理。
    private func warmUp() async {
        _ = try? await perform("Hallo.")
    }

    /// 生产走 Apple 翻译；selftest 注入 overrideAttempt 时只做纯编排。
    private func perform(_ text: String) async throws -> String {
        if let overrideAttempt {
            return try await overrideAttempt(text)
        }
        let session = try await session()
        let response = try await session.translate(text)
        var output = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
        if Self.isSimplifiedChinese(target) {
            output = Self.toSimplified(output)
        }
        return output
    }

    static func reason(for error: Error?) -> String {
        guard let error else {
            return reasonError
        }
        if let pipelineError = error as? PipelineError, case .unsupported = pipelineError {
            return reasonUnavailable
        }
        return reasonError
    }

    private func announceFailure(reason: String) {
        guard !didAnnounceFailure else {
            return
        }
        didAnnounceFailure = true
        onStatus(reason == Self.reasonUnavailable
            ? "翻译语言包未安装，部分句子未翻译"
            : "翻译失败，部分句子未翻译")
    }

    func waitUntilIdle() async {
        let shouldWait = queue.sync { pending > 0 }
        guard shouldWait else {
            return
        }
        await withCheckedContinuation { continuation in
            let shouldResumeNow = queue.sync {
                if pending > 0 {
                    idleWaiters.append(continuation)
                    return false
                }
                return true
            }
            if shouldResumeNow {
                continuation.resume()
            }
        }
    }

    func cancel() {
        queue.sync {
            cancelled = true
            for session in sessions.values {
                session.cancel()
            }
            sessions.removeAll()
        }
        resumeIdleIfNeeded(force: true)
    }

    private func session() async throws -> TranslationSession {
        if let existing = queue.sync(execute: { sessions[key] }) {
            return existing
        }

        let status = await LanguageAvailability().status(from: source, to: target)
        guard status == .installed else {
            throw PipelineError.unsupported(
                "语言对未安装，请到「系统设置 → 通用 → 语言与地区 → 翻译语言」下载后重试"
            )
        }

        let session = TranslationSession(installedSource: source, target: target)
        try await session.prepareTranslation()
        queue.sync {
            sessions[key] = session
        }
        return session
    }

    private func completeOne() {
        queue.sync {
            lastCompletion = clock()
            pending -= 1
        }
        resumeIdleIfNeeded()
    }

    private func shouldWarmUpLocked() -> Bool {
        guard let lastCompletion else {
            return true
        }
        return clock() - lastCompletion > Self.warmIdleNanoseconds
    }

    private func resumeIdleIfNeeded(force: Bool = false) {
        let waiters = queue.sync {
            guard force || pending == 0 else {
                return [CheckedContinuation<Void, Never>]()
            }
            let waiters = idleWaiters
            idleWaiters.removeAll()
            return waiters
        }
        guard !waiters.isEmpty else {
            return
        }
        for waiter in waiters {
            waiter.resume()
        }
    }

    private static func isSimplifiedChinese(_ lang: Locale.Language) -> Bool {
        lang.languageCode?.identifier == "zh" && lang.script?.identifier == "Hans"
    }

    // 系统翻译目标为简体时偶尔混入繁体，用 ICU 做最后一层规范化。
    private static func toSimplified(_ value: String) -> String {
        value.applyingTransform(StringTransform("Hant-Hans"), reverse: false) ?? value
    }
}
