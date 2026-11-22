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
        if let first = parts.first, first == "finished" {
            return StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle)
        }
        if parts.first == "no_config" {
            // 没配对节点不是「空闲」：广播已经起来了，只是没有可用的识别档，
            // 显示成「未启动」会让用户以为广播没开。
            // 显式 node 档与自动档都会写 no_config，解析不出是否已回退本机，
            // 所以只说「不可用」，不承诺「已用本机」。
            return StatusSummary(
                label: nodeLabel(in: parts) + DesignTokens.Text.nodeUnavailablePlainSuffix,
                level: .warning
            )
        }
        // key 无效和节点不可用都是完整短语，可能跨空格，直接按整行匹配。
        if trimmed.contains(DesignTokens.Text.statusCloudKeyInvalid) {
            return StatusSummary(label: DesignTokens.Text.statusCloudKeyInvalid, level: .error)
        }
        if trimmed.contains(DesignTokens.Text.nodeUnavailableSuffix) {
            return StatusSummary(label: DesignTokens.Text.statusLocal, level: .warning)
        }
        // 裸失败码（not_tailnet / resolve_failed / fatal:<码>）以前会一路掉到 nil，
        // 界面就一直停在最后一次正常状态，用户看不出广播其实已经没在工作。
        if let failure = nodeFailureSummary(in: parts) {
            return failure
        }

        var label: String?
        if let engineToken = parts.first(where: { $0.hasPrefix("engine=") }) {
            let engine = String(engineToken.dropFirst("engine=".count))
            switch engine {
            case "cloud":
                label = DesignTokens.Text.statusCloud
            case "node":
                label = nodeLabel(in: parts)
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
        // 本机失败的短行（如刚启动时的 local_needs_ios26）还没有 engine=，
        // 标签只能给「本机」，但等级必须抬成 error，不能让界面当成没这回事。
        if LocalEngineStatus.userMessage(forStatusLine: trimmed) != nil {
            return StatusSummary(label: DesignTokens.Text.statusLocal, level: .error)
        }
        return nil
    }

    /// 节点名。状态行缺 node= 时给一个非空占位，标签不能只剩「混合·」。
    private static func nodeLabel(in parts: [String]) -> String {
        let name = parts.first(where: { $0.hasPrefix("node=") })
            .map { String($0.dropFirst("node=".count)) } ?? ""
        return DesignTokens.Text.statusHybridPrefix + (name.isEmpty ? "电脑节点" : name)
    }

    /// 节点侧的裸失败码：不能当空闲、更不能吞掉。
    /// `not_tailnet` / `resolve_failed` 是还在按退避重连的失败（warning）；
    /// 带 `fatal:` 的表示节点已放弃（error）。
    /// 本机档的失败码走 LocalEngineStatus 那条路，这里必须放行，否则会把
    /// 「本机识别出错」显示成节点故障。
    private static func nodeFailureSummary(in parts: [String]) -> StatusSummary? {
        let engine = parts.first(where: { $0.hasPrefix("engine=") })
            .map { String($0.dropFirst("engine=".count)) }
        if engine == "local" || engine == "cloud" {
            return nil
        }
        for part in parts {
            if part.hasPrefix("fatal:") {
                // fatal:<码> 也用在没写 engine= 的本机短行上（如 local_needs_ios26）：
                // 能映射成本机文案的让它走下面那条本机路，别报成节点故障。
                let code = String(part.dropFirst("fatal:".count))
                if LocalEngineStatus.userMessage(forStatusLine: "fatal:\(code)") == nil {
                    return StatusSummary(
                        label: nodeLabel(in: parts) + DesignTokens.Text.nodeUnavailablePlainSuffix,
                        level: .error
                    )
                }
            }
            guard part.hasPrefix("last=") else { continue }
            let code = String(part.dropFirst("last=".count))
            if code == "not_tailnet" || code == "resolve_failed" {
                return StatusSummary(
                    label: nodeLabel(in: parts) + DesignTokens.Text.nodeUnavailableReconnectingSuffix,
                    level: .warning
                )
            }
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
