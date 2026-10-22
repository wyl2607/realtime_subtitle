import Foundation

/// 本机引擎状态解析：从一整行状态（空格分隔的 k=v 和 fatal:xxx 片段）里
/// 提取本机失败码，映射成一句中文提示；没有本机失败码时返回 nil。
///
/// 为什么要单独抽成纯函数：引擎的原始码（如 asr_failed:xyz、fatal:local_needs_ios26）
/// 对用户不可读，而映射规则要能在命令行自测里直接编译验证，不能依赖 SwiftUI。
enum LocalEngineStatus {
    static func userMessage(forStatusLine line: String) -> String? {
        for part in line.split(separator: " ") {
            let fragment = String(part)
            // 显式本机档会追加 fatal:<码>，状态行里同时带 last=<码>。
            let code: String
            if fragment.hasPrefix("fatal:") {
                code = String(fragment.dropFirst("fatal:".count))
            } else if fragment.hasPrefix("last=") {
                code = String(fragment.dropFirst("last=".count))
            } else {
                continue
            }
            switch code {
            case "asr_unsupported":
                return DesignTokens.Text.localAsrUnsupported
            case "asr_assets_missing":
                return DesignTokens.Text.localAssetsMissing
            case "analyzer_format_failed":
                return DesignTokens.Text.localFormatFailed
            // 翻译准备失败与资源缺失对用户是同一个结果：只有原文。
            case "tx_unavailable", "tx_prepare_failed":
                return DesignTokens.Text.localTxUnavailable
            case "local_needs_ios26":
                return DesignTokens.Text.localNeedsIOS26
            default:
                // asr_failed / asr_result_failed 后面会带错误描述，只能按前缀认；
                // 两者的用户操作相同（停止后重新开始）。
                if code.hasPrefix("asr_failed") || code.hasPrefix("asr_result_failed") {
                    return DesignTokens.Text.localAsrFailed
                }
            }
        }
        return nil
    }
}
