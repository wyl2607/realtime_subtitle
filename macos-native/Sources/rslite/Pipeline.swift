@preconcurrency import AVFoundation
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
    var onFinal: @Sendable (Int, String) -> Void = { _, _ in }
    var onTranslation: @Sendable (Int, String) -> Void = { _, _ in }
    var onStatus: @Sendable (String) -> Void = { _ in }
    var onFinished: @Sendable () -> Void = {}
}

@available(macOS 27.0, *)
final class Pipeline: @unchecked Sendable {
    private let config: PipelineConfig
    private let callbacks: PipelineCallbacks
    private let translator: TranslationQueue

    private var source: AudioSource?
    private var analyzer: SpeechAnalyzer?
    private var resultTask: Task<Void, Never>?
    private var analysisTask: Task<Void, Never>?
    private var finishContinuation: CheckedContinuation<Void, Never>?
    private var nextID = 1
    private var didCheckAssets = false
    private var isPaused = false
    private var isStopped = false
    private var hasFinished = false

    init(config: PipelineConfig, callbacks: PipelineCallbacks) {
        self.config = config
        self.callbacks = callbacks
        translator = TranslationQueue(
            sourceID: config.sourceLocaleID,
            targetID: config.targetLanguageID,
            onStatus: callbacks.onStatus,
            onTranslation: callbacks.onTranslation
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
            reportingOptions: [.volatileResults],
            attributeOptions: []
        )

        if !didCheckAssets {
            try await ensureSpeechAssets(transcriber: transcriber)
            didCheckAssets = true
        }

        let source = try makeAudioSource(config.sourceSpec)
        let stream = try source.start()
        let modules: [any SpeechModule] = [transcriber]
        guard let analyzerFormat = await SpeechAnalyzer.bestAvailableAudioFormat(
            compatibleWith: modules,
            considering: source.format
        ) else {
            throw PipelineError.unavailable("找不到兼容的识别音频格式")
        }

        let analyzer = SpeechAnalyzer(modules: modules)
        self.source = source
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

        let sourceFormat = source.format
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
        source?.stop()
        analysisTask?.cancel()
        resultTask?.cancel()
        if let analyzer {
            await analyzer.cancelAndFinishNow()
        }
        source = nil
        analyzer = nil
        analysisTask = nil
        resultTask = nil
    }

    private func finishRun() async {
        source?.stop()
        source = nil
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
        let text = String(result.text.characters).trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            return
        }

        if result.isFinal {
            let id = nextID
            nextID += 1
            callbacks.onFinal(id, text)
            translator.enqueue(id: id, text: text)
        } else {
            callbacks.onVolatile(text)
        }
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

    private let sourceStream: SendableAudioStream
    private let sourceFormat: AVAudioFormat
    private let analyzerFormat: AVAudioFormat

    init(sourceStream: AsyncStream<AVAudioPCMBuffer>, sourceFormat: AVAudioFormat, analyzerFormat: AVAudioFormat) {
        self.sourceStream = SendableAudioStream(stream: sourceStream)
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
        var sourceIterator: AsyncStream<AVAudioPCMBuffer>.Iterator
        let converter: AVAudioConverter?
        let analyzerFormat: AVAudioFormat

        mutating func next() async throws -> AnalyzerInput? {
            guard let converter else {
                throw PipelineError.unavailable("无法创建音频格式转换器")
            }
            while let sourceBuffer = await sourceIterator.next() {
                let converted = try Pipeline.convert(sourceBuffer, using: converter, to: analyzerFormat)
                if converted.frameLength > 0 {
                    return AnalyzerInput(buffer: converted)
                }
            }
            return nil
        }
    }
}

struct SendableAudioStream: @unchecked Sendable {
    let stream: AsyncStream<AVAudioPCMBuffer>
}

@available(macOS 26.0, *)
final class TranslationQueue: @unchecked Sendable {
    private let source: Locale.Language
    private let target: Locale.Language
    private let key: String
    private let onStatus: @Sendable (String) -> Void
    private let onTranslation: @Sendable (Int, String) -> Void

    private var sessions: [String: TranslationSession] = [:]
    private let queue = DispatchQueue(label: "rslite.translation.queue")
    private var chain = Task<Void, Never> {}
    private var pending = 0
    private var idleWaiters: [CheckedContinuation<Void, Never>] = []
    private var cancelled = false

    init(
        sourceID: String,
        targetID: String,
        onStatus: @escaping @Sendable (String) -> Void,
        onTranslation: @escaping @Sendable (Int, String) -> Void
    ) {
        source = Locale.Language(identifier: sourceID)
        target = Locale.Language(identifier: targetID)
        key = "\(sourceID)->\(targetID)"
        self.onStatus = onStatus
        self.onTranslation = onTranslation
    }

    func enqueue(id: Int, text: String) {
        queue.sync {
            guard !cancelled else {
                return
            }
            pending += 1
            let previous = chain
            chain = Task { [weak self] in
                await previous.value
                guard let self else {
                    return
                }
                await self.translateOne(id: id, text: text)
                self.completeOne()
            }
        }
    }

    private func translateOne(id: Int, text: String) async {
        do {
            let session = try await session()
            let response = try await session.translate(text)
            var output = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
            if Self.isSimplifiedChinese(target) {
                output = Self.toSimplified(output)
            }
            onTranslation(id, output)
        } catch {
            onStatus("翻译失败：\(error.localizedDescription)")
        }
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
            pending -= 1
        }
        resumeIdleIfNeeded()
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
