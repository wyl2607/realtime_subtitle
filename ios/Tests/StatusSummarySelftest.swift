import Foundation

/// 状态词解析自测：状态行格式是扩展和路由决策共同写下的契约，
/// 这里把每种写法都钉住，避免界面把「正在连接」之类的中间态读成别的东西。
enum StatusSummarySelftest {
    static func run() -> Bool {
        var failed = false
        func check(_ condition: Bool, _ message: String) {
            if condition {
                print("PASS: \(message)")
            } else {
                print("FAIL: \(message)")
                failed = true
            }
        }

        // 空闲/结束：界面显示「未启动」。
        check(
            StatusSummaryParser.summary(forStatusLine: "") == StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle),
            "空行 → 未启动"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "   \n ") == StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle),
            "纯空白 → 未启动"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "无") == StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle),
            "无 → 未启动"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "finished src=broadcast") == StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle),
            "finished → 未启动"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "no_config src=broadcast") == StatusSummary(label: DesignTokens.Text.micStatusIdle, level: .idle),
            "no_config → 未启动"
        )

        // 三种 engine 档位。
        check(
            StatusSummaryParser.summary(forStatusLine: "blocks=120 src=broadcast engine=cloud finals=2 tx=2 last=- cloud=up:3")
                == StatusSummary(label: DesignTokens.Text.statusCloud, level: .normal),
            "engine=cloud → 云端"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "blocks=120 src=mic engine=local finals=2 tx=2 last=-")
                == StatusSummary(label: DesignTokens.Text.statusLocal, level: .normal),
            "engine=local → 本机"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "blocks=120 src=broadcast engine=node node=mini2 ready=1 last=-")
                == StatusSummary(label: DesignTokens.Text.statusHybridPrefix + "mini2", level: .normal),
            "engine=node 带节点名 → 混合"
        )

        // 节点名缺失也要有一个非空标签，否则界面只剩「混合·」。
        let missingNodeName = StatusSummaryParser.summary(forStatusLine: "engine=node ready=1 last=-")
        check(
            missingNodeName?.level == .normal
                && missingNodeName?.label.hasPrefix(DesignTokens.Text.statusHybridPrefix) == true
                && missingNodeName?.label != DesignTokens.Text.statusHybridPrefix,
            "engine=node 缺节点名仍有非空标签"
        )

        // 短决策行：没有 engine=，只有 last=。
        check(
            StatusSummaryParser.summary(forStatusLine: "last=混合·mini2 src=broadcast")
                == StatusSummary(label: DesignTokens.Text.statusHybridPrefix + "mini2", level: .normal),
            "短决策行 last=混合"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "last=云端·Groq src=broadcast")
                == StatusSummary(label: DesignTokens.Text.statusCloud, level: .normal),
            "短决策行 last=云端"
        )

        // key 无效优先级高于冷却/节点判定。
        check(
            StatusSummaryParser.summary(forStatusLine: "last=云端 key 无效 src=broadcast")
                == StatusSummary(label: DesignTokens.Text.statusCloudKeyInvalid, level: .error),
            "key 无效 → error"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "engine=cloud last=云端 key 无效 cloud=down")
                == StatusSummary(label: DesignTokens.Text.statusCloudKeyInvalid, level: .error),
            "完整状态行里的 key 无效同样生效"
        )

        // 节点不可用时退回本机，只是警告级别。
        check(
            StatusSummaryParser.summary(forStatusLine: "last=mini2 不可用，已用本机 src=broadcast")
                == StatusSummary(label: DesignTokens.Text.statusLocal, level: .warning),
            "节点不可用 → 本机 warning"
        )

        // 冷却后缀：短决策行里自带一次，解析后必须只留一次。
        let cooledExpected = DesignTokens.Text.statusHybridPrefix + "mini2" + DesignTokens.Text.statusCloudCooldownSuffix
        let cooled = StatusSummaryParser.summary(forStatusLine: "last=混合·mini2 · 云端限流冷却 src=broadcast")
        check(
            cooled == StatusSummary(label: cooledExpected, level: .warning),
            "短决策行冷却后缀 → warning"
        )
        if let cooledLabel = cooled?.label {
            check(
                cooledLabel.components(separatedBy: DesignTokens.Text.statusCloudCooldownSuffix).count == 2,
                "冷却后缀只出现一次"
            )
        } else {
            check(false, "冷却后缀结果不应为 nil")
        }
        check(
            StatusSummaryParser.summary(forStatusLine: "engine=node node=mini2 last=混合·mini2 · 云端限流冷却 src=broadcast")
                == StatusSummary(label: cooledExpected, level: .warning),
            "完整状态行也只在末尾补一次冷却后缀"
        )

        // 不含档位信息的行必须返回 nil，界面才会保留上一次状态词。
        check(
            StatusSummaryParser.summary(forStatusLine: "started mode=auto src=mic") == nil,
            "started → nil（保留上一次）"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "blocks=120 src=broadcast last=- drop=0") == nil,
            "没有 engine= 也没有档位 last= → nil"
        )

        // 本机失败码只把等级抬成 error，标签仍显示本机。
        check(
            StatusSummaryParser.summary(forStatusLine: "engine=local last=asr_assets_missing")
                == StatusSummary(label: DesignTokens.Text.statusLocal, level: .error),
            "本机资源缺失 → 本机 error"
        )
        check(
            StatusSummaryParser.summary(forStatusLine: "engine=local fatal:asr_unsupported")
                == StatusSummary(label: DesignTokens.Text.statusLocal, level: .error),
            "本机不支持 → 本机 error"
        )
        // 节点档里遗留的本机失败码不算本机错误（LocalEngineStatus 自己会返回 nil）。
        check(
            StatusSummaryParser.summary(forStatusLine: "engine=node node=mini2 last=asr_assets_missing ready=1")?.level == .normal,
            "节点档的旧本机失败码不抬等级"
        )

        return !failed
    }
}
