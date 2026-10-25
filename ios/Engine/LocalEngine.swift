import Foundation
import AVFoundation
import Speech
import Translation
import os

/// 一句识别结果在分析音频时间轴上的位置（秒）。
///
/// 语音识别不给词级时间（volatile 结果只有「段首→当前」的宽区间），
/// 只有整句的区间可用；P5 的覆盖判定要的就是这个区间。
struct LocalSentenceRange: Equatable {
    var start: Double
    var end: Double
}

@available(iOS 26.0, *)
final class LocalEngine {
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "local-engine")

    /// 引擎模式。
    enum Mode {
        /// 本机识别 + 本机翻译（默认）。
        case recognizeAndTranslate
        /// 只做翻译：云端档用。云端只给原文，译文要用本机 TranslationSession。
        case translateOnly
    }

    private var analyzer: SpeechAnalyzer?
    private var transcriber: SpeechTranscriber?
    private var translationSession: TranslationSession?
    private var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?
    private var analysisTask: Task<Void, Never>?
    private var resultTask: Task<Void, Never>?
    /// 识别不能等翻译模型准备好；结果到达时再等这个任务，首句既不丢译文也不挡原文。
    private var translationReady: Task<TranslationSession?, Never>?
    /// 最近一句的翻译任务；下一句接在它后面。
    private var translationTail: Task<Void, Never>?
    /// 守 translationReady / translationTail 和 stop() 对 stopped 的写入。
    /// 本类没有任何隔离：stop() 跑在调用方线程，启动和结果循环跑在全局执行器上，
    /// 不加锁时两边会同时读写同一个 Task 引用。
    private let taskLock = NSLock()
    private var nextID = 1
    private var isRunning = false
    /// stop() 之后一律不再回调。
    ///
    /// 不能拿 isRunning 当这个闸门：要到 startRecognition() 末尾才会置真，
    /// 在那之前发出的 asr_unsupported / asr_assets_missing / analyzer_format_failed /
    /// tx_unavailable 都是启动期失败，用 isRunning 守会把它们全部吞掉。
    private var stopped = false

    private let mode: Mode
    private let onFinal: (Int, String, LocalSentenceRange?) -> Void
    private let onTranslation: (Int, String) -> Void
    /// 这一句已经收尾：译文落地了，或者翻译失败了。
    /// 切回节点必须等这一刻——早一步切走会把还在路上的译文取消掉，
    /// 用户看到的就是「最后一句只剩原文」。
    private let onSentenceSettled: (Int) -> Void
    private let onStatus: (String) -> Void

    private let sourceLocale = Locale.Language(identifier: "de")
    private let targetLocale = Locale.Language(identifier: "zh-Hans")
    private let sourceFormat = AVAudioFormat(
        commonFormat: .pcmFormatInt16,
        sampleRate: 16_000,
        channels: 1,
        interleaved: true
    )!
    private var analyzerFormat: AVAudioFormat?
    // 每帧新建 converter 会丢掉重采样器状态；格式相同时直接送原 buffer
    private var converter: AVAudioConverter?

    init(
        mode: Mode = .recognizeAndTranslate,
        onFinal: @escaping (Int, String, LocalSentenceRange?) -> Void,
        onTranslation: @escaping (Int, String) -> Void,
        onSentenceSettled: @escaping (Int) -> Void,
        onStatus: @escaping (String) -> Void
    ) {
        self.mode = mode
        self.onFinal = onFinal
        self.onTranslation = onTranslation
        self.onSentenceSettled = onSentenceSettled
        self.onStatus = onStatus
    }

    func start() async {
        guard !isRunning, !stopped else { return }
        switch mode {
        case .translateOnly:
            await startTranslationOnly()
        case .recognizeAndTranslate:
            await startRecognition()
        }
    }

    private func emitStatus(_ status: String) {
        guard !stopped else { return }
        onStatus(status)
    }

    private func emitFinal(_ id: Int, _ text: String, _ range: LocalSentenceRange?) {
        guard !stopped else { return }
        onFinal(id, text, range)
    }

    private func emitTranslation(_ id: Int, _ text: String) {
        guard !stopped else { return }
        onTranslation(id, text)
    }

    private func emitSettled(_ id: Int) {
        guard !stopped else { return }
        onSentenceSettled(id)
    }

    /// 只翻译：起一个 TranslationSession 就完事，不碰语音识别资源。
    ///
    /// 云端档必须先确认这条路能用：云端只给原文，出不了译文就等于没字幕。
    /// 翻译不可用时回调 `tx_unavailable`，上层据此把云端这一档关掉。
    private func startTranslationOnly() async {
        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        guard !stopped else { return }
        guard txStatus == .installed else {
            emitStatus("tx_unavailable")
            return
        }
        do {
            let session = TranslationSession(installedSource: sourceLocale, target: targetLocale)
            try await session.prepareTranslation()
            guard !stopped else { return }
            self.translationSession = session
            isRunning = true
            emitStatus("tx_ready")
        } catch {
            logger.error("TranslationSession prepare failed: \(error.localizedDescription)")
            emitStatus("tx_prepare_failed")
        }
    }

    private func startRecognition() async {
        let locale = Locale(identifier: "de-DE")
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            emitStatus("asr_unsupported")
            return
        }
        guard !stopped else { return }

        let transcriber = SpeechTranscriber(
            locale: supported,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )

        let assetStatus = await AssetInventory.status(forModules: [transcriber])
        guard !stopped else { return }
        if assetStatus != .installed {
            emitStatus("asr_assets_missing")
            return
        }

        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        guard !stopped else { return }
        if txStatus != .installed {
            emitStatus("tx_unavailable")
        }

        let inputStream = AsyncStream<AnalyzerInput> { continuation in
            self.inputContinuation = continuation
        }

        let modules: [any SpeechModule] = [transcriber]

        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules,
            considering: sourceFormat
        ) else {
            emitStatus("analyzer_format_failed")
            return
        }
        guard !stopped else { return }
        self.analyzerFormat = analyzerFormat

        let analyzer = SpeechAnalyzer(modules: modules)
        self.analyzer = analyzer
        self.transcriber = transcriber
        isRunning = true

        if txStatus == .installed {
            // 翻译模型首次准备可能很久，独立执行才能让识别任务立即消费已到的音频。
            // 放在识别器预热和结果任务之前创建：两边的准备并行，最早的结果也一定看得到这个任务。
            let ready = Task { [weak self] () -> TranslationSession? in
                guard let self, !self.stopped else { return nil }
                let session = TranslationSession(installedSource: self.sourceLocale, target: self.targetLocale)
                do {
                    try await session.prepareTranslation()
                    guard !self.stopped else { return nil }
                    self.translationSession = session
                    return session
                } catch {
                    guard !self.stopped else { return nil }
                    self.logger.error("TranslationSession prepare failed: \(error.localizedDescription)")
                    self.emitStatus("tx_prepare_failed")
                    return nil
                }
            }
            // 和 stop() 在同一把锁里核对 stopped：否则 stop() 刚清完，这里又挂上一个没人取消的任务。
            let kept = taskLock.withLock { () -> Bool in
                guard !stopped else { return false }
                translationReady = ready
                return true
            }
            if !kept { ready.cancel() }
        }

        do {
            try await analyzer.prepareToAnalyze(in: analyzerFormat)
        } catch {
            // 预热失败时后续分析仍会自行加载模型，不能为此放弃已经可用的识别链路。
            logger.error("SpeechAnalyzer prepare failed: \(error.localizedDescription)")
        }
        guard !stopped else { return }

        resultTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await result in transcriber.results {
                    await self.handleResult(result)
                }
            } catch is CancellationError {
                // stop() 取消结果流不是识别失败：显式本机档会把它当成 fatal 直接收场。
            } catch {
                self.emitStatus("asr_result_failed: \(error.localizedDescription)")
            }
        }

        analysisTask = Task { [weak self, inputStream, analyzer] in
            guard let self else { return }
            do {
                _ = try await analyzer.analyzeSequence(inputStream)
                try await analyzer.finalizeAndFinishThroughEndOfInput()
                await self.waitForResults()
                await self.finish()
            } catch is CancellationError {
                await analyzer.cancelAndFinishNow()
            } catch {
                self.emitStatus("asr_failed: \(error.localizedDescription)")
                await self.finish()
            }
        }
    }

    func append(pcm16k: Data) {
        guard mode == .recognizeAndTranslate,
              isRunning,
              let continuation = inputContinuation,
              let analyzerFormat = analyzerFormat else { return }

        let frameCount = pcm16k.count / 2
        guard frameCount > 0,
              let sourceBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: AVAudioFrameCount(frameCount)) else {
            return
        }

        sourceBuffer.frameLength = AVAudioFrameCount(frameCount)
        pcm16k.withUnsafeBytes { raw in
            guard let base = raw.baseAddress, let dst = sourceBuffer.int16ChannelData?[0] else { return }
            memcpy(dst, base, pcm16k.count)
        }

        if analyzerFormat == sourceFormat {
            continuation.yield(AnalyzerInput(buffer: sourceBuffer))
            return
        }

        guard let outputBuffer = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: AVAudioFrameCount(Double(frameCount) * analyzerFormat.sampleRate / sourceFormat.sampleRate) + 1024) else {
            return
        }
        if converter == nil {
            converter = AVAudioConverter(from: sourceFormat, to: analyzerFormat)
        }
        guard let converter else { return }
        var didProvideInput = false
        var convertError: NSError?
        let status = converter.convert(to: outputBuffer, error: &convertError) { _, outStatus in
            if didProvideInput {
                outStatus.pointee = .noDataNow
                return nil
            }
            didProvideInput = true
            outStatus.pointee = .haveData
            return sourceBuffer
        }

        if convertError != nil || status == .error || outputBuffer.frameLength == 0 {
            return
        }

        let input = AnalyzerInput(buffer: outputBuffer)
        continuation.yield(input)
    }

    /// 翻译一句（云端档用）。id 由调用方给：云端那一路的句子 id 与本机引擎的不在一个号段里。
    ///
    /// 翻译成功、失败、压根没有会话，这一句都算收尾（回调 onSentenceSettled）。
    func translate(_ text: String, id: Int) {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !stopped else { return }
        guard let session = translationSession else {
            emitStatus("tx_unavailable")
            emitSettled(id)
            return
        }
        Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await session.translate(trimmed)
                guard !self.stopped else { return }
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    self.emitTranslation(id, translated)
                } else {
                    self.emitStatus("tx_empty")
                }
            } catch {
                guard !self.stopped else { return }
                self.logger.error("Translation failed: \(error.localizedDescription)")
                self.emitStatus("tx_error")
            }
            self.emitSettled(id)
        }
    }

    func stop() {
        // 必须在 isRunning 那道 guard 之前置真：启动中途被停时 isRunning 还是 false，
        // 这时候更要挡住启动期回调。
        let tasks = taskLock.withLock { () -> (Task<TranslationSession?, Never>?, Task<Void, Never>?) in
            stopped = true
            defer {
                translationReady = nil
                translationTail = nil
            }
            return (translationReady, translationTail)
        }
        tasks.0?.cancel()
        tasks.1?.cancel()
        guard isRunning else { return }
        isRunning = false
        inputContinuation?.finish()
        inputContinuation = nil
        analysisTask?.cancel()
        analysisTask = nil
        resultTask?.cancel()
        resultTask = nil
        analyzer = nil
        transcriber = nil
        translationSession = nil
        analyzerFormat = nil
        converter = nil
    }

    private func handleResult(_ result: SpeechTranscriber.Result) async {
        guard !stopped else { return }
        let raw = String(result.text.characters)
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, result.isFinal else { return }

        let id = nextID
        nextID += 1

        guard !stopped else { return }
        emitFinal(id, text, Self.rangeSeconds(result.range))

        // 翻译挂到串行尾链上，不在结果循环里等：否则翻译还在准备（或上一句还在翻）时，
        // 下一句的原文也得排队等着才能上屏。串行是为了译文和收尾仍按句子顺序到达。
        taskLock.withLock {
            guard !stopped else { return }
            let previous = translationTail
            translationTail = Task { [weak self] in
                await previous?.value
                await self?.translateAndSettle(id: id, text: text)
            }
        }
    }

    private func translateAndSettle(id: Int, text: String) async {
        guard !stopped else { return }
        let ready = taskLock.withLock { translationReady }
        if let session = await ready?.value {
            do {
                let response = try await session.translate(text)
                guard !stopped else { return }
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    emitTranslation(id, translated)
                } else {
                    emitStatus("tx_empty")
                }
            } catch {
                guard !stopped else { return }
                logger.error("Translation failed: \(error.localizedDescription)")
                emitStatus("tx_error")
            }
        }

        // 翻译成功、翻译失败、或者压根没有翻译会话，这一句都算收尾了。
        // 放在最后：调用方据此判断「本机这边可以放手了」，能安全切回节点。
        emitSettled(id)
    }

    /// CMTimeRange → 秒。整句区间为零/非法时返回 nil：宁可没有时间，
    /// 也不要拿一个假区间去覆盖别处的句子。
    private static func rangeSeconds(_ range: CMTimeRange) -> LocalSentenceRange? {
        let start = range.start.seconds
        let end = range.end.seconds
        guard start.isFinite, end.isFinite, end > start else { return nil }
        return LocalSentenceRange(start: start, end: end)
    }

    private func waitForResults() async {
        await resultTask?.value
        resultTask = nil
        // 最后一句的译文也要等它落地，finish 才能放掉会话。
        // 结果流已经结束，不会再有新句子接到尾链上：这里拿到的就是最后一句。
        let tail = taskLock.withLock { translationTail }
        await tail?.value
        taskLock.withLock { translationTail = nil }
    }

    private func finish() async {
        analyzer = nil
        transcriber = nil
        translationSession = nil
    }
}
