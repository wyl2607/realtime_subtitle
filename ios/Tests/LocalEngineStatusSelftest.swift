import Foundation

/// 本机引擎状态解析自测（spec：本机识别失败码要可读、可测）。
/// 每个失败码 → 对应文案；前缀匹配 `asr_failed:` / `asr_result_failed:`；
/// `fatal:asr_assets_missing` 也要被识别；正常节点状态行应返回 nil。
///
/// 为什么要自带 @main：不依赖任何 SwiftUI / ReplayKit / AVFoundation 模块，
/// `swiftc -parse` 和独立编译都能直接验证，CI 里跑一次就够。
@main
struct LocalEngineStatusSelftest {
    static func main() {
        var failed = false
        func check(_ condition: Bool, _ message: String) {
            if !condition {
                print("FAIL: \(message)")
                failed = true
            } else {
                print("PASS: \(message)")
            }
        }

        // 基础映射：每个本机失败码对应唯一提示文案。
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_unsupported") == DesignTokens.Text.localAsrUnsupported,
            "asr_unsupported → localAsrUnsupported"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_assets_missing") == DesignTokens.Text.localAssetsMissing,
            "asr_assets_missing → localAssetsMissing"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=analyzer_format_failed") == DesignTokens.Text.localFormatFailed,
            "analyzer_format_failed → localFormatFailed"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_failed:some error desc") == DesignTokens.Text.localAsrFailed,
            "asr_failed 前缀匹配（带描述）"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_result_failed:timeout") == DesignTokens.Text.localAsrFailed,
            "asr_result_failed 前缀匹配"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=tx_unavailable") == DesignTokens.Text.localTxUnavailable,
            "tx_unavailable → localTxUnavailable"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=local_needs_ios26") == DesignTokens.Text.localNeedsIOS26,
            "local_needs_ios26 → localNeedsIOS26"
        )
        // 显式本机档的 fatal:<码> 写法（SubtitleEngine 里 fatalErrorOccurred 写入状态行）。
        check(
            LocalEngineStatus.userMessage(forStatusLine: "fatal:asr_assets_missing last=-") == DesignTokens.Text.localAssetsMissing,
            "fatal:asr_assets_missing 识别（与 last 形式同码）"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "fatal:local_needs_ios26") == DesignTokens.Text.localNeedsIOS26,
            "fatal:local_needs_ios26 识别"
        )
        // 前缀匹配要覆盖带描述的变体（引擎写入的原始描述可能不同）。
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_failed:SpeechAnalyzer initialization failed") == DesignTokens.Text.localAsrFailed,
            "asr_failed 前缀覆盖长描述"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_result_failed:transcription canceled") == DesignTokens.Text.localAsrFailed,
            "asr_result_failed 前缀覆盖长描述"
        )

        // 正常节点状态行不应触发任何提示（避免误报）。
        check(
            LocalEngineStatus.userMessage(forStatusLine: "engine=node ready=1 sent=5 finals=2 tx=2 last=- drop=0 reconn=0") == nil,
            "正常节点状态行返回 nil"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=closed=1006") == nil,
            "节点 close 码不属于本机失败，返回 nil"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "engine=local last=tx_ready") == nil,
            "本机翻译就绪不触发提示"
        )
        // 自动档回退到节点/云端后，上一轮本机遗留的 last= 不能再冒出来。
        check(
            LocalEngineStatus.userMessage(forStatusLine: "engine=node ready=1 sent=5 last=asr_assets_missing") == nil,
            "engine=node 的旧本机失败码返回 nil"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "engine=local last=asr_assets_missing") == DesignTokens.Text.localAssetsMissing,
            "engine=local 的资源缺失照旧识别"
        )
        check(
            LocalEngineStatus.userMessage(forStatusLine: "engine=cloud last=tx_unavailable") == nil,
            "engine=cloud 的本机翻译失败码返回 nil"
        )
        // 引擎刚起、还没写 engine= 的短状态行不能被上面那道闸误伤。
        check(
            LocalEngineStatus.userMessage(forStatusLine: "last=asr_assets_missing src=mic") == DesignTokens.Text.localAssetsMissing,
            "不含 engine= 的短状态行照旧识别"
        )

        if failed {
            print("LOCAL ENGINE STATUS SELFTEST FAILED")
            exit(1)
        } else {
            print("LOCAL ENGINE STATUS SELFTEST OK")
        }
    }
}
