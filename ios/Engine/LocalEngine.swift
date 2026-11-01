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
    /// 守所有可变状态：analyzer / transcriber / translationSession / inputContinuation /
    /// analysisTask / resultTask / translationReady / translationTail / isRunning / stopped /
    /// analyzerFormat / converter。调用方无隔离：start() 在全局执行器上跑、await 后可能被
    /// stop() 打断；append() 在音频实时线程上同步调用（每帧几十毫秒，锁内只能读快照、
    /// 不许分配内存、不许做格式转换、不许调回调、不许 await、不许 yield）；
    /// translate() 在另一队列上同步调用；stop() 在调用方线程上同步调用。
    /// 锁内绝不 await、不调用回调、不调用 cancel()/finish()、不访问 Speech/Translation API。
    private let taskLock = NSLock()
    /// 只在结果循环（单个串行 for-await 循环里的 handleResult）读写，没有并发访问者，可不加锁。
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
        let shouldProceed: Bool = taskLock.withLock { () -> Bool in
            guard !isRunning, !stopped else { return false }
            return true
        }
        guard shouldProceed else { return }
        switch mode {
        case .translateOnly:
            await startTranslationOnly()
        case .recognizeAndTranslate:
            await startRecognition()
        }
    }

    private func emitStatus(_ status: String) {
        let shouldEmit: Bool = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            return true
        }
        guard shouldEmit else { return }
        onStatus(status)
    }

    private func emitFinal(_ id: Int, _ text: String, _ range: LocalSentenceRange?) {
        let shouldEmit: Bool = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            return true
        }
        guard shouldEmit else { return }
        onFinal(id, text, range)
    }

    private func emitTranslation(_ id: Int, _ text: String) {
        let shouldEmit: Bool = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            return true
        }
        guard shouldEmit else { return }
        onTranslation(id, text)
    }

    private func emitSettled(_ id: Int) {
        let shouldEmit: Bool = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            return true
        }
        guard shouldEmit else { return }
        onSentenceSettled(id)
    }

    /// 只翻译：起一个 TranslationSession 就完事，不碰语音识别资源。
    ///
    /// 云端档必须先确认这条路能用：云端只给原文，出不了译文就等于没字幕。
    /// 翻译不可用时回调 `tx_unavailable`，上层据此把云端这一档关掉。
    private func startTranslationOnly() async {
        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        let stoppedAfterAwait: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterAwait else { return }
        guard txStatus == .installed else {
            emitStatus("tx_unavailable")
            return
        }
        do {
            let session = TranslationSession(installedSource: sourceLocale, target: targetLocale)
            try await session.prepareTranslation()
            let stoppedAfterPrepare: Bool = taskLock.withLock { () -> Bool in
                return stopped
            }
            guard !stoppedAfterPrepare else { return }
            let kept: Bool = taskLock.withLock { () -> Bool in
                guard !stopped else { return false }
                self.translationSession = session
                self.isRunning = true
                return true
            }
            if kept {
                emitStatus("tx_ready")
            }
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
        let stoppedAfterLocale: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterLocale else { return }

        let transcriber = SpeechTranscriber(
            locale: supported,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )

        let assetStatus = await AssetInventory.status(forModules: [transcriber])
        let stoppedAfterAssets: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterAssets else { return }
        if assetStatus != .installed {
            emitStatus("asr_assets_missing")
            return
        }

        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        let stoppedAfterTxStatus: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterTxStatus else { return }
        if txStatus != .installed {
            emitStatus("tx_unavailable")
        }

        let inputStream = AsyncStream<AnalyzerInput> { continuation in
            taskLock.withLock {
                self.inputContinuation = continuation
            }
        }

        let modules: [any SpeechModule] = [transcriber]

        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules,
            considering: sourceFormat
        ) else {
            emitStatus("analyzer_format_failed")
            return
        }
        let stoppedAfterFormat: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterFormat else { return }
        let keptFormat = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            self.analyzerFormat = analyzerFormat
            return true
        }
        guard keptFormat else { return }

        let analyzer = SpeechAnalyzer(modules: modules)
        let keptAnalyzer = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            self.analyzer = analyzer
            self.transcriber = transcriber
            self.isRunning = true
            return true
        }
        guard keptAnalyzer else { return }

        if txStatus == .installed {
            // 翻译模型首次准备可能很久，独立执行才能让识别任务立即消费已到的音频。
            // 放在识别器预热和结果任务之前创建：两边的准备并行，最早的结果也一定看得到这个任务。
            let ready = Task { [weak self] () -> TranslationSession? in
                guard let self else { return nil }
                let stoppedNow: Bool = taskLock.withLock { () -> Bool in
                    return self.stopped
                }
                guard !stoppedNow else { return nil }
                let session = TranslationSession(installedSource: self.sourceLocale, target: self.targetLocale)
                do {
                    try await session.prepareTranslation()
                    let stoppedAfterPrepare: Bool = taskLock.withLock { () -> Bool in
                        return self.stopped
                    }
                    guard !stoppedAfterPrepare else { return nil }
                    let keptSession: Bool = taskLock.withLock { () -> Bool in
                        guard !self.stopped else { return false }
                        self.translationSession = session
                        return true
                    }
                    if keptSession {
                        return session
                    }
                    return nil
                } catch {
                    let stoppedAfterError: Bool = taskLock.withLock { () -> Bool in
                        return self.stopped
                    }
                    guard !stoppedAfterError else { return nil }
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
        let stoppedAfterPrepareAnalyze: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterPrepareAnalyze else { return }

        let resultTaskLocal = Task { [weak self] in
            guard let self else { return }
            do {
                let transcriberLocal: SpeechTranscriber? = taskLock.withLock { () -> SpeechTranscriber? in
                    return self.transcriber
                }
                guard let transcriberLocal else { return }
                for try await result in transcriberLocal.results {
                    await self.handleResult(result)
                }
            } catch is CancellationError {
                // stop() 取消结果流不是识别失败：显式本机档会把它当成 fatal 直接收场。
            } catch {
                self.emitStatus("asr_result_failed: \(error.localizedDescription)")
            }
        }
        let keptResult = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            resultTask = resultTaskLocal
            return true
        }
        if !keptResult {
            resultTaskLocal.cancel()
        }

        let analysisTaskLocal = Task { [weak self, inputStream, analyzer] in
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
        let keptAnalysis = taskLock.withLock { () -> Bool in
            guard !stopped else { return false }
            analysisTask = analysisTaskLocal
            return true
        }
        if !keptAnalysis {
            analysisTaskLocal.cancel()
        }
    }

    func append(pcm16k: Data) {
        // 音频线程约束：锁内只读快照（O(1)、无分配、无回调、无 await、无 yield、无格式转换）；
        // 转换和 continuation.yield 在锁外用快照执行。
        let snapshot = taskLock.withLock { () -> (running: Bool, continuation: AsyncStream<AnalyzerInput>.Continuation?, fmt: AVAudioFormat?, converter: AVAudioConverter?, mode: Mode) in
            return (isRunning, inputContinuation, analyzerFormat, converter, self.mode)
        }
        guard snapshot.mode == .recognizeAndTranslate,
              snapshot.running,
              let continuation = snapshot.continuation,
              let analyzerFormat = snapshot.fmt else { return }

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

        // 懒建 converter：先锁外建，再锁内「若仍为 nil 且未停才存」。
        var converterToUse: AVAudioConverter? = snapshot.converter
        if converterToUse == nil {
            let newConverter = AVAudioConverter(from: sourceFormat, to: analyzerFormat)
            let keptConverter = taskLock.withLock { () -> Bool in
                guard !stopped, converter == nil else { return false }
                converter = newConverter
                return true
            }
            converterToUse = keptConverter ? newConverter : (converterToUse ?? newConverter)
        }
        guard let converter = converterToUse else { return }
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
        let (stoppedNow, sessionSnapshot): (Bool, TranslationSession?) = taskLock.withLock { () -> (Bool, TranslationSession?) in
            return (stopped, translationSession)
        }
        guard !trimmed.isEmpty, !stoppedNow else { return }
        guard let session = sessionSnapshot else {
            emitStatus("tx_unavailable")
            emitSettled(id)
            return
        }
        Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await session.translate(trimmed)
                let stoppedAfterTranslate: Bool = taskLock.withLock { () -> Bool in
                    return self.stopped
                }
                guard !stoppedAfterTranslate else { return }
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    self.emitTranslation(id, translated)
                } else {
                    self.emitStatus("tx_empty")
                }
            } catch {
                let stoppedAfterError: Bool = taskLock.withLock { () -> Bool in
                    return self.stopped
                }
                guard !stoppedAfterError else { return }
                self.logger.error("Translation failed: \(error.localizedDescription)")
                self.emitStatus("tx_error")
            }
            self.emitSettled(id)
        }
    }

    func stop() {
        // 必须在 isRunning 那道 guard 之前置真：启动中途被停时 isRunning 还是 false，
        // 这时候更要挡住启动期回调。
        let (readyTask, tailTask, wasRunning, cont, fmt, an, tr, session, anTask, resTask, convVal) = taskLock.withLock { () -> (Task<TranslationSession?, Never>?, Task<Void, Never>?, Bool, AsyncStream<AnalyzerInput>.Continuation?, AVAudioFormat?, SpeechAnalyzer?, SpeechTranscriber?, TranslationSession?, Task<Void, Never>?, Task<Void, Never>?, AVAudioConverter?) in
            let running = isRunning
            stopped = true
            isRunning = false
            if running {
                let r = translationReady; translationReady = nil
                let t = translationTail; translationTail = nil
                let c = inputContinuation; inputContinuation = nil
                let f = analyzerFormat; analyzerFormat = nil
                let a = analyzer; analyzer = nil
                let trVal = transcriber; transcriber = nil
                let s = translationSession; translationSession = nil
                let at = analysisTask; analysisTask = nil
                let rt = resultTask; resultTask = nil
                let conv = converter; converter = nil
                return (r, t, true, c, f, a, trVal, s, at, rt, conv)
            } else {
                // 即使未运行，translationReady / translationTail 仍要清（和原行为一致）
                translationReady = nil
                translationTail = nil
                return (nil, nil, false, nil, nil, nil, nil, nil, nil, nil, nil)
            }
        }
        readyTask?.cancel()
        tailTask?.cancel()
        if wasRunning {
            cont?.finish()
            anTask?.cancel()
            resTask?.cancel()
        }
    }

    private func handleResult(_ result: SpeechTranscriber.Result) async {
        let stoppedNow: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedNow else { return }
        let raw = String(result.text.characters)
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, result.isFinal else { return }

        let id = nextID
        nextID += 1

        let stoppedAfterID: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedAfterID else { return }
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
        let stoppedNow: Bool = taskLock.withLock { () -> Bool in
            return stopped
        }
        guard !stoppedNow else { return }
        let ready = taskLock.withLock { translationReady }
        if let session = await ready?.value {
            do {
                let response = try await session.translate(text)
                let stoppedAfterTranslate: Bool = taskLock.withLock { () -> Bool in
                    return stopped
                }
                guard !stoppedAfterTranslate else { return }
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    emitTranslation(id, translated)
                } else {
                    emitStatus("tx_empty")
                }
            } catch {
                let stoppedAfterError: Bool = taskLock.withLock { () -> Bool in
                    return stopped
                }
                guard !stoppedAfterError else { return }
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
        let resultTaskLocal: Task<Void, Never>? = taskLock.withLock { () -> Task<Void, Never>? in
            let r = resultTask
            resultTask = nil
            return r
        }
        await resultTaskLocal?.value
        // 最后一句的译文也要等它落地，finish 才能放掉会话。
        // 结果流已经结束，不会再有新句子接到尾链上：这里拿到的就是最后一句。
        let tail = taskLock.withLock { translationTail }
        await tail?.value
        taskLock.withLock { translationTail = nil }
    }

    private func finish() async {
        taskLock.withLock {
            analyzer = nil
            transcriber = nil
            translationSession = nil
        }
    }
}
