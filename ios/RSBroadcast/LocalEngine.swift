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
    private var nextID = 1
    private var isRunning = false

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
        guard !isRunning else { return }
        switch mode {
        case .translateOnly:
            await startTranslationOnly()
        case .recognizeAndTranslate:
            await startRecognition()
        }
    }

    /// 只翻译：起一个 TranslationSession 就完事，不碰语音识别资源。
    ///
    /// 云端档必须先确认这条路能用：云端只给原文，出不了译文就等于没字幕。
    /// 翻译不可用时回调 `tx_unavailable`，上层据此把云端这一档关掉。
    private func startTranslationOnly() async {
        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        guard txStatus == .installed else {
            onStatus("tx_unavailable")
            return
        }
        do {
            let session = TranslationSession(installedSource: sourceLocale, target: targetLocale)
            try await session.prepareTranslation()
            self.translationSession = session
            isRunning = true
            onStatus("tx_ready")
        } catch {
            logger.error("TranslationSession prepare failed: \(error.localizedDescription)")
            onStatus("tx_prepare_failed")
        }
    }

    private func startRecognition() async {
        let locale = Locale(identifier: "de-DE")
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
            onStatus("asr_unsupported")
            return
        }

        let transcriber = SpeechTranscriber(
            locale: supported,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )

        let assetStatus = await AssetInventory.status(forModules: [transcriber])
        if assetStatus != .installed {
            onStatus("asr_assets_missing")
            return
        }

        let txStatus = await LanguageAvailability().status(from: sourceLocale, to: targetLocale)
        if txStatus != .installed {
            onStatus("tx_unavailable")
        }

        let inputStream = AsyncStream<AnalyzerInput> { continuation in
            self.inputContinuation = continuation
        }

        let modules: [any SpeechModule] = [transcriber]

        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules,
            considering: sourceFormat
        ) else {
            onStatus("analyzer_format_failed")
            return
        }
        self.analyzerFormat = analyzerFormat

        let analyzer = SpeechAnalyzer(modules: modules)
        self.analyzer = analyzer
        self.transcriber = transcriber
        isRunning = true

        if txStatus == .installed {
            do {
                let session = TranslationSession(installedSource: sourceLocale, target: targetLocale)
                try await session.prepareTranslation()
                self.translationSession = session
            } catch {
                logger.error("TranslationSession prepare failed: \(error.localizedDescription)")
                onStatus("tx_prepare_failed")
            }
        }

        resultTask = Task { [weak self] in
            guard let self else { return }
            do {
                for try await result in transcriber.results {
                    await self.handleResult(result)
                }
            } catch {
                self.onStatus("asr_result_failed: \(error.localizedDescription)")
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
                self.onStatus("asr_failed: \(error.localizedDescription)")
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
        guard !trimmed.isEmpty else { return }
        guard let session = translationSession else {
            onStatus("tx_unavailable")
            onSentenceSettled(id)
            return
        }
        Task { [weak self] in
            guard let self else { return }
            do {
                let response = try await session.translate(trimmed)
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    self.onTranslation(id, translated)
                } else {
                    self.onStatus("tx_empty")
                }
            } catch {
                self.logger.error("Translation failed: \(error.localizedDescription)")
                self.onStatus("tx_error")
            }
            self.onSentenceSettled(id)
        }
    }

    func stop() {
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
        let raw = String(result.text.characters)
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, result.isFinal else { return }

        let id = nextID
        nextID += 1

        onFinal(id, text, Self.rangeSeconds(result.range))

        if let session = translationSession {
            do {
                let response = try await session.translate(text)
                let translated = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if !translated.isEmpty {
                    onTranslation(id, translated)
                } else {
                    onStatus("tx_empty")
                }
            } catch {
                logger.error("Translation failed: \(error.localizedDescription)")
                onStatus("tx_error")
            }
        }

        // 翻译成功、翻译失败、或者压根没有翻译会话，这一句都算收尾了。
        // 放在最后：调用方据此判断「本机这边可以放手了」，能安全切回节点。
        onSentenceSettled(id)
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
    }

    private func finish() async {
        analyzer = nil
        transcriber = nil
        translationSession = nil
    }
}
