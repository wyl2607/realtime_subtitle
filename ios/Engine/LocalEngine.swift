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

    /// stop() 锁内摘出来的待取消/待结束对象；cancel()/finish() 一律在锁外做。
    private struct StopSnapshot {
        var translationReady: Task<TranslationSession?, Never>?
        var translationTail: Task<Void, Never>?
        var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?
        var analysisTask: Task<Void, Never>?
        var resultTask: Task<Void, Never>?
    }

    /// 守本类全部可变状态：analyzer / transcriber / translationSession /
    /// inputContinuation / analysisTask / resultTask / translationReady /
    /// translationTail / isRunning / stopped / analyzerFormat / converter。
    ///
    /// 本类没有任何隔离：stop() 跑在调用方线程，启动、结果循环、翻译任务跑在全局
    /// 执行器上，append() 跑在音频实时线程上，不加锁时这些线程会同时读写同一批字段。
    ///
    /// 锁只包字段读写：绝不在持锁时 await、调回调、cancel/finish 任务或 continuation，
    /// 也不碰 analyzer / Translation / Speech API。append() 在锁内只做 O(1) 的检查和
    /// 取快照（isRunning / inputContinuation / analyzerFormat / converter），转换和
    /// yield 都在锁外，所以音频线程最多等几十条指令。
    private let stateLock = NSLock()
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
        let canStart = stateLock.withLock { !isRunning && !stopped }
        guard canStart else { return }
        switch mode {
        case .translateOnly:
            await startTranslationOnly()
        case .recognizeAndTranslate:
            await startRecognition()
        }
    }

    private func isStopped() -> Bool {
        stateLock.withLock { stopped }
    }

    private func emitStatus(_ status: String) {
        // 锁内只读闸门，回调一律锁外调：stop() 恰好落在检查之后是可接受的，
        // 调用方 SubtitleEngine 自己还有一道闸门。
        let allowed = stateLock.withLock { !stopped }
        guard allowed else { return }
        onStatus(status)
    }

    private func emitFinal(_ id: Int, _ text: String, _ range: LocalSentenceRange?) {
        let allowed = stateLock.withLock { !stopped }
        guard allowed else { return }
        onFinal(id, text, range)
    }

    private func emitTranslation(_ id: Int, _ text: String) {
        let allowed = stateLock.withLock { !stopped }
        guard allowed else { return }
        onTranslation(id, text)
    }

    private func emitSettled(_ id: Int) {
        let allowed = stateLock.withLock { !stopped }
        guard allowed else { return }
        onSentenceSettled(id)
    }

    /// 只翻译：起一个 TranslationSession 就完事，不碰语音识别资源。
    ///
    /// 云端档必须先确认这条路能用：云端只给原文，出不了译文就等于没字幕。
    /// 翻译不可用时回调 `tx_unavailable`，上层据此把云端这一档关掉。
    private func startTranslationOnly() async {
        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        guard !isStopped() else { return }
        guard txStatus == .installed else {
            emitStatus("tx_unavailable")
            return
        }
        do {
            let session = TranslationSession(installedSource: sourceLocale, target: targetLocale)
            try await session.prepareTranslation()
            // await 期间 stop() 可能已经跑完：锁内核对再落，避免把 isRunning 重新置真。
            let kept = stateLock.withLock { () -> Bool in
                guard !stopped else { return false }
                translationSession = session
                isRunning = true
                return true
            }
            guard kept else { return }
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
        guard !isStopped() else { return }

        let transcriber = SpeechTranscriber(
            locale: supported,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )

        let assetStatus = await AssetInventory.status(forModules: [transcriber])
        guard !isStopped() else { return }
        if assetStatus != .installed {
            emitStatus("asr_assets_missing")
            return
        }

        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        guard !isStopped() else { return }
        if txStatus != .installed {
            emitStatus("tx_unavailable")
        }

        let inputStream = AsyncStream<AnalyzerInput> { continuation in
            // build 闭包同步执行，但 stop() 可能正并发跑：赋值同样要进锁，
            // 已停就当场 finish，免得留下一个没人消费也没人结束的 continuation。
            let kept = stateLock.withLock { () -> Bool in
                guard !stopped else { return false }
                inputContinuation = continuation
                return true
            }
            if !kept { continuation.finish() }
        }

        let modules: [any SpeechModule] = [transcriber]

        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules,
            considering: sourceFormat
        ) else {
            emitStatus("analyzer_format_failed")
            discardPendingInputContinuation()
            return
        }
        guard !isStopped() else {
            discardPendingInputContinuation()
            return
        }

        let analyzer = SpeechAnalyzer(modules: modules)
        // 锁内一次性核对 stopped 并落 analyzer / transcriber / format / isRunning：
        // 否则 stop() 刚清完，这里又把这些字段和 isRunning 写回来。
        let kept = stateLock.withLock { () -> Bool in
            guard !stopped else { return false }
            self.analyzerFormat = analyzerFormat
            self.analyzer = analyzer
            self.transcriber = transcriber
            isRunning = true
            return true
        }
        guard kept else {
            discardPendingInputContinuation()
            return
        }

        if txStatus == .installed {
            // 翻译模型首次准备可能很久，独立执行才能让识别任务立即消费已到的音频。
            // 放在识别器预热和结果任务之前创建：两边的准备并行，最早的结果也一定看得到这个任务。
            let ready = Task { [weak self] () -> TranslationSession? in
                guard let self, !self.isStopped() else { return nil }
                let session = TranslationSession(installedSource: self.sourceLocale, target: self.targetLocale)
                do {
                    try await session.prepareTranslation()
                    // 准备期间可能已经 stop()：锁内核对再写 translationSession。
                    let kept = self.stateLock.withLock { () -> Bool in
                        guard !self.stopped else { return false }
                        self.translationSession = session
                        return true
                    }
                    return kept ? session : nil
                } catch {
                    guard !self.isStopped() else { return nil }
                    self.logger.error("TranslationSession prepare failed: \(error.localizedDescription)")
                    self.emitStatus("tx_prepare_failed")
                    return nil
                }
            }
            // 和 stop() 在同一把锁里核对 stopped：否则 stop() 刚清完，这里又挂上一个没人取消的任务。
            let kept = stateLock.withLock { () -> Bool in
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
        guard !isStopped() else { return }

        // 任务先锁外建好，再锁内核对 stopped 后挂上；已停就取消刚建的任务，
        // 与 translationReady 同一个模式。
        let newResultTask = Task { [weak self] in
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
        let resultKept = stateLock.withLock { () -> Bool in
            guard !stopped else { return false }
            resultTask = newResultTask
            return true
        }
        if !resultKept { newResultTask.cancel() }

        let newAnalysisTask = Task { [weak self, inputStream, analyzer] in
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
        let analysisKept = stateLock.withLock { () -> Bool in
            guard !stopped else { return false }
            analysisTask = newAnalysisTask
            return true
        }
        if !analysisKept { newAnalysisTask.cancel() }
    }

    /// 启动中途被 stop() 抢先时，把已经建好、但还没交给分析任务的输入流收掉。
    ///
    /// stop() 只在 isRunning 为真时清 continuation；而这里恰好是 isRunning 还没置真的窗口，
    /// 不主动 finish 会留下一个永远没人消费的 AsyncStream。
    private func discardPendingInputContinuation() {
        let orphan = stateLock.withLock { () -> AsyncStream<AnalyzerInput>.Continuation? in
            guard !isRunning else { return nil }
            let continuation = inputContinuation
            inputContinuation = nil
            return continuation
        }
        orphan?.finish()
    }

    func append(pcm16k: Data) {
        guard mode == .recognizeAndTranslate else { return }

        // 音频线程约束：锁内只做 O(1) 的检查和取快照，不分配、不做格式转换、
        // 不 yield、不调回调。后面的 buffer / converter / yield 全部在锁外用快照做。
        let snapshot = stateLock.withLock { () -> (AsyncStream<AnalyzerInput>.Continuation, AVAudioFormat, AVAudioConverter?)? in
            guard isRunning,
                  let continuation = inputContinuation,
                  let format = analyzerFormat else { return nil }
            return (continuation, format, converter)
        }
        guard let snapshot = snapshot else { return }
        let continuation = snapshot.0
        let analyzerFormat = snapshot.1
        let converterSnapshot = snapshot.2

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
        // 懒建 converter：构造可能不便宜，锁外建；锁内只在「仍为 nil 且未停」时存，
        // 避免和 stop() 清字段互相覆盖。
        var converter = converterSnapshot
        if converter == nil {
            let made = AVAudioConverter(from: sourceFormat, to: analyzerFormat)
            if let made = made {
                converter = stateLock.withLock { () -> AVAudioConverter? in
                    guard !stopped else { return nil }
                    if let existing = self.converter { return existing }
                    self.converter = made
                    return made
                }
            }
        }
        guard let converter = converter else { return }
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
        guard !trimmed.isEmpty else { return }
        let snapshot = stateLock.withLock { () -> (Bool, TranslationSession?) in
            (stopped, translationSession)
        }
        guard !snapshot.0 else { return }
        guard let session = snapshot.1 else {
            emitStatus("tx_unavailable")
            emitSettled(id)
            return
        }
        Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await session.translate(trimmed)
                guard !self.isStopped() else { return }
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    self.emitTranslation(id, translated)
                } else {
                    self.emitStatus("tx_empty")
                }
            } catch {
                guard !self.isStopped() else { return }
                self.logger.error("Translation failed: \(error.localizedDescription)")
                self.emitStatus("tx_error")
            }
            self.emitSettled(id)
        }
    }

    func stop() {
        // 锁内一次性置 stopped、isRunning = false，并把要取消/结束的对象摘出来；
        // cancel()/finish() 一律锁外做，避免在持锁时调任务/continuation 的方法。
        //
        // stopped 必须在 isRunning 那道判断之前置真：启动中途被停时 isRunning 还是 false，
        // 这时候更要挡住启动期回调。
        let snapshot = stateLock.withLock { () -> StopSnapshot in
            stopped = true
            var snapshot = StopSnapshot(
                translationReady: translationReady,
                translationTail: translationTail
            )
            translationReady = nil
            translationTail = nil
            guard isRunning else { return snapshot }
            isRunning = false
            snapshot.inputContinuation = inputContinuation
            snapshot.analysisTask = analysisTask
            snapshot.resultTask = resultTask
            inputContinuation = nil
            analysisTask = nil
            resultTask = nil
            analyzer = nil
            transcriber = nil
            translationSession = nil
            analyzerFormat = nil
            converter = nil
            return snapshot
        }
        snapshot.translationReady?.cancel()
        snapshot.translationTail?.cancel()
        snapshot.inputContinuation?.finish()
        snapshot.analysisTask?.cancel()
        snapshot.resultTask?.cancel()
    }

    private func handleResult(_ result: SpeechTranscriber.Result) async {
        guard !isStopped() else { return }
        let raw = String(result.text.characters)
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, result.isFinal else { return }

        // nextID 只在本方法里读写，而本方法只由 resultTask 里那个单个串行的
        // for-await 结果循环调用，同一时刻只有一个执行者，所以不需要锁。
        let id = nextID
        nextID += 1

        guard !isStopped() else { return }
        emitFinal(id, text, Self.rangeSeconds(result.range))

        // 翻译挂到串行尾链上，不在结果循环里等：否则翻译还在准备（或上一句还在翻）时，
        // 下一句的原文也得排队等着才能上屏。串行是为了译文和收尾仍按句子顺序到达。
        let previous = stateLock.withLock { () -> (Task<Void, Never>?, Bool) in
            (translationTail, stopped)
        }
        guard !previous.1 else { return }
        let newTail = Task { [weak self] in
            await previous.0?.value
            await self?.translateAndSettle(id: id, text: text)
        }
        // 和 stop() 在同一把锁里核对 stopped：stop() 刚清完就不要再挂尾链。
        let kept = stateLock.withLock { () -> Bool in
            guard !stopped else { return false }
            translationTail = newTail
            return true
        }
        if !kept { newTail.cancel() }
    }

    private func translateAndSettle(id: Int, text: String) async {
        guard !isStopped() else { return }
        let ready = stateLock.withLock { translationReady }
        if let session = await ready?.value {
            do {
                let response = try await session.translate(text)
                guard !isStopped() else { return }
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    emitTranslation(id, translated)
                } else {
                    emitStatus("tx_empty")
                }
            } catch {
                guard !isStopped() else { return }
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
        // 等完再清：等待期间 stop() 仍要能从字段里拿到它去取消。
        let pendingResultTask = stateLock.withLock { resultTask }
        await pendingResultTask?.value
        stateLock.withLock { resultTask = nil }
        // 最后一句的译文也要等它落地，finish 才能放掉会话。
        // 结果流已经结束，不会再有新句子接到尾链上：这里拿到的就是最后一句。
        let tail = stateLock.withLock { translationTail }
        await tail?.value
        stateLock.withLock { translationTail = nil }
    }

    private func finish() async {
        stateLock.withLock {
            analyzer = nil
            transcriber = nil
            translationSession = nil
        }
    }
}
