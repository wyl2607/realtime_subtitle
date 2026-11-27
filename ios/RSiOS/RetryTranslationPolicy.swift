import Foundation

/// 识别某一行字幕的重试键：文件内序号区分同秒同文的重复句，时间和原文防止跨天/错行误改。
struct RetryLineKey: Hashable, Sendable {
    let id: Int
    let time: String
    let src: String
}

/// 回看页单句重试的界面状态。
enum RetryLineStatus: Equatable, Sendable {
    case idle
    case retrying
    case failed(String)

    var isFailed: Bool {
        if case .failed = self { return true }
        return false
    }
}

/// 回看页单句重试的明确失败原因；提示文案全部来自 DesignTokens。
enum RetryTranslationFailure: Error, Equatable, Sendable, LocalizedError {
    case emptySource
    case alreadyInFlight
    case assetsNotInstalled
    case timedOut
    case emptyResult
    case translationFailed

    var errorDescription: String? {
        RetryTranslationPolicy.failureMessage(for: self)
    }
}

/// 同一句并发去重器：正在重试时拒绝重复进入，不排队。
struct RetryInFlightTracker<Key: Hashable & Sendable>: Sendable {
    private(set) var activeKeys: Set<Key> = []

    mutating func begin(_ key: Key) -> Bool {
        activeKeys.insert(key).inserted
    }

    mutating func end(_ key: Key) {
        activeKeys.remove(key)
    }

    func contains(_ key: Key) -> Bool {
        activeKeys.contains(key)
    }
}

/// 回看页重新翻译的纯逻辑判定（无系统框架依赖，可直接在命令行自测）。
enum RetryTranslationPolicy {
    /// 与 LocalEngine.translationTimeout 保持一致的单句翻译超时（秒）。
    static let translationTimeout: TimeInterval = 10

    /// 只有原文非空、译文为空且该句未在重试中时才允许发起重试。
    static func canRetry(src: String, dst: String, isRetrying: Bool) -> Bool {
        guard !isRetrying else { return false }
        let hasSource = !src.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        let needsTranslation = dst.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        return hasSource && needsTranslation
    }

    static func failureMessage(for failure: RetryTranslationFailure) -> String {
        switch failure {
        case .assetsNotInstalled:
            return DesignTokens.Text.retryTxAssetsMissing
        case .timedOut:
            return DesignTokens.Text.retryTxTimedOut
        case .emptySource, .alreadyInFlight, .emptyResult, .translationFailed:
            return DesignTokens.Text.retryTxFailed
        }
    }
}
