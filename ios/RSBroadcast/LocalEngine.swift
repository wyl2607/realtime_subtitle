import Foundation
import AVFoundation
import Speech
import Translation
import os

@available(iOS 26.0, *)
final class LocalEngine {
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "local-engine")

    private var analyzer: SpeechAnalyzer?
    private var transcriber: SpeechTranscriber?
    private var translationSession: TranslationSession?
    private var inputContinuation: AsyncStream<AnalyzerInput>.Continuation?
    private var analysisTask: Task<Void, Never>?
    private var resultTask: Task<Void, Never>?
    private var nextID = 1
    private var isRunning = false

    private let onFinal: (Int, String) -> Void
    private let onTranslation: (Int, String) -> Void
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

    init(
        onFinal: @escaping (Int, String) -> Void,
        onTranslation: @escaping (Int, String) -> Void,
        onStatus: @escaping (String) -> Void
    ) {
        self.onFinal = onFinal
        self.onTranslation = onTranslation
        self.onStatus = onStatus
    }

    func start() async {
        guard !isRunning else { return }

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
        guard isRunning,
              let continuation = inputContinuation,
              let analyzerFormat = analyzerFormat else { return }

        let frameCount = pcm16k.count / 2
        guard frameCount > 0,
              let sourceBuffer = AVAudioPCMBuffer(pcmFormat: sourceFormat, frameCapacity: AVAudioFrameCount(frameCount)),
              let outputBuffer = AVAudioPCMBuffer(pcmFormat: analyzerFormat, frameCapacity: AVAudioFrameCount(Double(frameCount) * analyzerFormat.sampleRate / sourceFormat.sampleRate) + 1024) else {
            return
        }

        sourceBuffer.frameLength = AVAudioFrameCount(frameCount)
        pcm16k.withUnsafeBytes { raw in
            guard let base = raw.baseAddress, let dst = sourceBuffer.int16ChannelData?[0] else { return }
            memcpy(dst, base, pcm16k.count)
        }

        var didProvideInput = false
        var convertError: NSError?
        guard let converter = AVAudioConverter(from: sourceFormat, to: analyzerFormat) else { return }
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
    }

    private func handleResult(_ result: SpeechTranscriber.Result) async {
        let raw = String(result.text.characters)
        let text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty, result.isFinal else { return }

        let id = nextID
        nextID += 1

        onFinal(id, text)

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