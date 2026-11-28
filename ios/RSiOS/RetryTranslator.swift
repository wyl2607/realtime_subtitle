import Foundation
import Translation
import os

/// 一次性交卷的竞速闸门：翻译结果和超时谁先到谁算数，后到的静默丢弃。
/// 与 LocalEngine 同款原因：Translation 不保证响应取消，不用 TaskGroup 在尾部死等。
private final class RetryTimeoutRace<Value: Sendable>: @unchecked Sendable {
    private let lock = NSLock()
    private var settled: Result<Value, Error>?
    private var waiter: CheckedContinuation<Value, Error>?

    func settle(_ result: Result<Value, Error>) {
        lock.lock()
        guard settled == nil else {
            lock.unlock()
            return
        }
        if let waiter {
            settled = result
            self.waiter = nil
            lock.unlock()
            waiter.resume(with: result)
        } else {
            settled = result
            lock.unlock()
        }
    }

    func value() async throws -> Value {
        try await withCheckedThrowingContinuation { continuation in
            lock.lock()
            if let settled {
                lock.unlock()
                continuation.resume(with: settled)
            } else {
                waiter = continuation
                lock.unlock()
            }
        }
    }
}

/// 回看页单句重试翻译器：仅使用已安装的本机 de→zh-Hans 翻译资源，不弹下载、不联网。
actor RetryTranslator {
    static let sourceLocale = Locale.Language(identifier: "de")
    static let targetLocale = Locale.Language(identifier: "zh-Hans")
    static let translationTimeout: TimeInterval = RetryTranslationPolicy.translationTimeout

    private var inFlight = RetryInFlightTracker<RetryLineKey>()
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "retry-translator")

    func translate(key: RetryLineKey, text: String) async throws -> String {
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            throw RetryTranslationFailure.emptySource
        }
        guard inFlight.begin(key) else {
            throw RetryTranslationFailure.alreadyInFlight
        }
        defer { inFlight.end(key) }

        let status = await LanguageAvailability().status(from: Self.sourceLocale, to: Self.targetLocale)
        guard status == .installed else {
            throw RetryTranslationFailure.assetsNotInstalled
        }

        do {
            let targetText = try await Self.translateWithTimeout(text: trimmed)
            let translated = targetText.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !translated.isEmpty else {
                throw RetryTranslationFailure.emptyResult
            }
            return translated
        } catch let failure as RetryTranslationFailure {
            throw failure
        } catch is CancellationError {
            throw CancellationError()
        } catch {
            let latestStatus = await LanguageAvailability().status(from: Self.sourceLocale, to: Self.targetLocale)
            if latestStatus != .installed {
                throw RetryTranslationFailure.assetsNotInstalled
            }
            logger.error("Retry translation failed: \(error.localizedDescription, privacy: .public)")
            throw RetryTranslationFailure.translationFailed
        }
    }

    private static func translateWithTimeout(text: String) async throws -> String {
        let race = RetryTimeoutRace<String>()
        let sourceLocale = Self.sourceLocale
        let targetLocale = Self.targetLocale
        let timeoutSeconds = Self.translationTimeout
        let work = Task {
            do {
                let session = TranslationSession(installedSource: sourceLocale, target: targetLocale)
                let response = try await session.translate(text)
                race.settle(.success(response.targetText))
            } catch {
                race.settle(.failure(error))
            }
        }
        let timer = Task {
            try? await Task.sleep(nanoseconds: UInt64(timeoutSeconds * 1_000_000_000))
            race.settle(.failure(RetryTranslationFailure.timedOut))
        }
        defer {
            timer.cancel()
            work.cancel()
        }
        return try await withTaskCancellationHandler {
            try await race.value()
        } onCancel: {
            race.settle(.failure(CancellationError()))
        }
    }
}
