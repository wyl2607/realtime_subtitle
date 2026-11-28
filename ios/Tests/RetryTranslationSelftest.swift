import Foundation

/// 回看页重新翻译纯逻辑自测：可重试判定、同句并发去重、失败文案映射与超时常量。
@main
struct RetryTranslationSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        // 是否可重试判定。
        check(
            RetryTranslationPolicy.canRetry(src: "Guten Tag.", dst: "", isRetrying: false),
            "有原文且译文为空时允许重试"
        )
        check(
            RetryTranslationPolicy.canRetry(src: "Guten Tag.", dst: "  \n ", isRetrying: false),
            "译文仅空白时允许重试"
        )
        check(
            !RetryTranslationPolicy.canRetry(src: "Guten Tag.", dst: "你好", isRetrying: false),
            "已有译文时不允许重试"
        )
        check(
            !RetryTranslationPolicy.canRetry(src: "   ", dst: "", isRetrying: false),
            "原文为空白时不允许重试"
        )
        check(
            !RetryTranslationPolicy.canRetry(src: "Guten Tag.", dst: "", isRetrying: true),
            "正在重试中时不允许重复触发"
        )

        // 同句并发去重（同时间同原文不同行号视为不同句）。
        var tracker = RetryInFlightTracker<RetryLineKey>()
        let line0 = RetryLineKey(id: 0, time: "12:00:00", src: "Hallo.")
        let line1 = RetryLineKey(id: 1, time: "12:00:00", src: "Hallo.")
        check(tracker.begin(line0), "首次开始重试应成功")
        check(!tracker.begin(line0), "同一句并发重试应被拒绝（不排队）")
        check(tracker.begin(line1), "同秒同原文但不同行号应允许各自重试")
        check(tracker.contains(line0) && tracker.contains(line1), "两句都在进行中")
        tracker.end(line0)
        check(!tracker.contains(line0) && tracker.contains(line1), "结束一句不影响另一句")
        check(tracker.begin(line0), "上一轮结束后同一句应可再次重试")

        // 失败文案映射来自 DesignTokens。
        check(
            RetryTranslationPolicy.failureMessage(for: .assetsNotInstalled) == DesignTokens.Text.retryTxAssetsMissing,
            "资源未安装文案映射"
        )
        check(
            RetryTranslationPolicy.failureMessage(for: .timedOut) == DesignTokens.Text.retryTxTimedOut,
            "超时文案映射"
        )
        check(
            RetryTranslationPolicy.failureMessage(for: .translationFailed) == DesignTokens.Text.retryTxFailed,
            "一般失败文案映射"
        )
        check(
            RetryTranslationPolicy.failureMessage(for: .emptyResult) == DesignTokens.Text.retryTxFailed,
            "空译文失败文案映射"
        )
        check(
            RetryTranslationFailure.assetsNotInstalled.errorDescription == DesignTokens.Text.retryTxAssetsMissing,
            "LocalizedError errorDescription 应与 failureMessage 一致"
        )
        check(
            RetryTranslationPolicy.translationTimeout >= 5 && RetryTranslationPolicy.translationTimeout <= 20,
            "单句翻译超时应在 5-20 秒合理范围"
        )

        if failures.isEmpty {
            print("RETRY TRANSLATION SELFTEST OK")
        } else {
            for failure in failures { print("FAIL: \(failure)") }
            exit(1)
        }
    }
}
