import Foundation

/// 广播扩展每 5 秒写一行状态，格式是空格分隔的 k=v。界面只要一个短状态词，
/// 但状态行里同时混着档位（engine=）、节点名、短决策（last=...）和失败码，
/// 所以解析必须单独成纯函数：不依赖 SwiftUI，命令行自测能直接验证。
enum StatusLevel: Equatable {
    case idle
    case normal
    case warning
    case error
}

struct StatusSummary: Equatable {
    let label: String
    let level: StatusLevel
}

enum StatusSummaryParser {
    /// nil = 这一行不含档位信息（如 started），界面应保留上一次的状态词。
    static func summary(forStatusLine line: String) -> StatusSummary? {
        let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty || trimmed == "无" {
            return StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle)
        }
        let parts = trimmed.split(separator: " ").map(String.init)
        if let first = parts.first, first == "finished" || first == "no_config" {
            return StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle)
        }
        // key 无效和节点不可用都是完整短语，可能跨空格，直接按整行匹配。
        if trimmed.contains(DesignTokens.Text.statusCloudKeyInvalid) {
            return StatusSummary(label: DesignTokens.Text.statusCloudKeyInvalid, level: .error)
        }
        if trimmed.contains(DesignTokens.Text.nodeUnavailableSuffix) {
            return StatusSummary(label: DesignTokens.Text.statusLocal, level: .warning)
        }

        var label: String?
        if let engineToken = parts.first(where: { $0.hasPrefix("engine=") }) {
            let engine = String(engineToken.dropFirst("engine=".count))
            switch engine {
            case "cloud":
                label = DesignTokens.Text.statusCloud
            case "node":
                let name = parts.first(where: { $0.hasPrefix("node=") })
                    .map { String($0.dropFirst("node=".count)) } ?? ""
                label = DesignTokens.Text.statusHybridPrefix + (name.isEmpty ? "电脑节点" : name)
            case "local":
                label = DesignTokens.Text.statusLocal
            default:
                label = nil
            }
        } else if let decision = decisionLabel(in: trimmed) {
            label = decision
        }

        if var current = label {
            var level = StatusLevel.normal
            if trimmed.contains(DesignTokens.Text.statusCloudCooldownSuffix) {
                // 短决策行里后缀本身就在 last= 的文本里，上面已剥掉，这里统一补一次。
                if !current.hasSuffix(DesignTokens.Text.statusCloudCooldownSuffix) {
                    current += DesignTokens.Text.statusCloudCooldownSuffix
                }
                level = .warning
            }
            // 本机失败码只抬等级，不改标签：用户先要知道「本机」，再看出错了。
            if LocalEngineStatus.userMessage(forStatusLine: trimmed) != nil {
                level = .error
            }
            return StatusSummary(label: current, level: level)
        }
        return nil
    }

    /// 路由决策时会写短行（没有 engine=）：`last=混合·mini2 · 云端限流冷却 src=broadcast`。
    /// last= 的值可能含空格（冷却后缀自带空格），所以按 ` src=` 的边界截取，不能按空格切词。
    private static func decisionLabel(in line: String) -> String? {
        guard let range = line.range(of: "last=") else { return nil }
        let after = line[range.upperBound...]
        let raw: Substring
        if let srcRange = after.range(of: " src=") {
            raw = after[..<srcRange.lowerBound]
        } else {
            raw = after
        }
        var value = raw.trimmingCharacters(in: .whitespaces)
        let prefixes = [
            DesignTokens.Text.statusHybridPrefix,
            DesignTokens.Text.statusCloud,
            DesignTokens.Text.statusLocal,
        ]
        guard prefixes.contains(where: { value.hasPrefix($0) }) else { return nil }
        if value.hasSuffix(DesignTokens.Text.statusCloudCooldownSuffix) {
            value.removeLast(DesignTokens.Text.statusCloudCooldownSuffix.count)
            value = value.trimmingCharacters(in: .whitespaces)
        }
        return value.isEmpty ? nil : value
    }
}
