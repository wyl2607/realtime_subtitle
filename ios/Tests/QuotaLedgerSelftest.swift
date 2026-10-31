import Foundation

/// QuotaLedger 自测：计费下限、90/80 滞回、原子落盘往返、损坏账本隔离。
/// 临时文件只落在工作目录的 .tmp/ 下，由本函数自己清理（run-selftest.sh 也会兜底删 .tmp）。
func runQuotaLedgerSelftest(_ check: @escaping (Bool, String) -> Void) {
    let root = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent(".tmp/quota-selftest-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: root) }
    let file = root.appendingPathComponent("quota-ledger.json")
    let t0 = Date(timeIntervalSince1970: 10_000)

    func routeContext(_ state: CloudState) -> CloudRouteContext {
        CloudRouteContext(RouteInitial(
            hasKey: true, cloudEnabled: true, cloudState: state,
            nodeEnabled: false, nodeHealthy: false, nodeIsSelf: false,
            consecutiveFailures: 0, backoffIndex: 0
        ))
    }

    do {
        try FileManager.default.createDirectory(at: root, withIntermediateDirectories: true)
        let ledger = QuotaLedger(url: file, now: t0)
        let billed = try ledger.recordRequest(duration: 3, at: t0)
        check(billed == 10, "计费下限应为 10 秒，实际 \(billed)")
        check(ledger.usage(at: t0).dayAudioSeconds == 10, "短音频应按 10 秒入账")

        for i in 1..<18 {
            try ledger.recordRequest(duration: 10, at: t0.addingTimeInterval(Double(i)))
        }
        var context = routeContext(.available)
        ledger.applyQuotaEvent(to: &context, at: t0.addingTimeInterval(20))
        check(context.cloudState == .cooldownQuota, "达到 90% 应进入额度冷却")

        let later = t0.addingTimeInterval(80)
        ledger.applyQuotaEvent(to: &context, at: later)
        check(context.cloudState == .available, "1 分钟窗口落到 80% 以下应恢复")
        check(ledger.usage(at: later).minuteRequests == 0, "1 分钟窗口应滚动清零")

        try ledger.recordRequest(duration: 20, at: t0.addingTimeInterval(3_700))
        let usageAfterHour = ledger.usage(at: t0.addingTimeInterval(3_701))
        check(usageAfterHour.hourAudioSeconds == 20, "1 小时音频窗口应滚动，实际 \(usageAfterHour.hourAudioSeconds)")
        check(usageAfterHour.dayRequests == 19, "24 小时窗口内请求不应清零，实际 \(usageAfterHour.dayRequests)")

        let restarted = QuotaLedger(url: file, now: t0.addingTimeInterval(3_702))
        check(restarted.usage(at: t0.addingTimeInterval(3_702)).dayRequests == 19,
              "持久化重启不应清零")

        // 17 次 = 85%：已在冷却时不能因为掉到 90% 以下就立刻放行。
        let eightyFiveFile = root.appendingPathComponent("quota-85.json")
        let eightyFive = QuotaLedger(url: eightyFiveFile, now: t0)
        for _ in 0..<17 {
            try eightyFive.recordRequest(duration: 10, at: t0)
        }
        var sticky = routeContext(.cooldownQuota)
        eightyFive.applyQuotaEvent(to: &sticky, at: t0)
        check(sticky.cloudState == .cooldownQuota, "85% 应保持额度冷却（滞回下沿是 80%）")

        // 16 次 = 80%：仍然不放行。
        let eightyFile = root.appendingPathComponent("quota-80.json")
        let eighty = QuotaLedger(url: eightyFile, now: t0)
        for _ in 0..<16 {
            try eighty.recordRequest(duration: 10, at: t0)
        }
        var edge = routeContext(.cooldownQuota)
        eighty.applyQuotaEvent(to: &edge, at: t0)
        check(edge.cloudState == .cooldownQuota, "正好 80% 应保持额度冷却")

        // 15 次 = 75%：低于 80% 才恢复。
        let seventyFiveFile = root.appendingPathComponent("quota-75.json")
        let seventyFive = QuotaLedger(url: seventyFiveFile, now: t0)
        for _ in 0..<15 {
            try seventyFive.recordRequest(duration: 10, at: t0)
        }
        var recovered = routeContext(.cooldownQuota)
        seventyFive.applyQuotaEvent(to: &recovered, at: t0)
        check(recovered.cloudState == .available, "75% 应退出额度冷却")

        let oldSnapshot = QuotaLedgerSnapshot(events: [
            QuotaLedgerEvent(ts: t0.timeIntervalSince1970 - 90_000, billedS: 99),
            QuotaLedgerEvent(ts: t0.timeIntervalSince1970, billedS: 10),
        ])
        try JSONEncoder().encode(oldSnapshot).write(to: file)
        let pruned = QuotaLedger(url: file, now: t0)
        check(pruned.usage(at: t0).dayRequests == 1, "超过 24 小时事件应丢弃")

        try pruned.correctRemainingRequests(["x-ratelimit-remaining-requests": "2"], at: t0)
        check(pruned.usage(at: t0).minuteRequests == 1, "每日 remaining 不应影响 minuteRequests")
        check(pruned.usage(at: t0).dayRequests == 1_998, "remaining=2 应校正 24 小时请求数，实际 \(pruned.usage(at: t0).dayRequests)")
        let aged = pruned.usage(at: t0.addingTimeInterval(86_401))
        check(aged.dayRequests == 0, "校正记录应随 24 小时窗口丢弃")
        check(aged.minuteRequests == 0, "过期校正不应进入分钟窗口")

        let daily = QuotaLedger(url: root.appendingPathComponent("quota-daily.json"), now: t0)
        try daily.recordRequest(duration: 10, at: t0)
        try daily.correctRemainingRequests(["x-ratelimit-remaining-requests": "1980"], at: t0)
        let dailyUsage = daily.usage(at: t0)
        check(dailyUsage.dayRequests == 20, "remaining=1980 时 dayRequests 应为 20，实际 \(dailyUsage.dayRequests)")
        check(dailyUsage.minuteRequests == 1, "remaining=1980 不应影响 minuteRequests")

        let missingURL = root.appendingPathComponent("missing-quota.json")
        let missing = QuotaLedger(url: missingURL, now: t0)
        check(missing.usage(at: t0).usagePercent == 0, "账本文件不存在时应为空账本")
        check(missing.usage(at: t0).dayRequests == 0, "账本文件不存在时不应饱和")
        check(!FileManager.default.fileExists(atPath: missingURL.path), "空账本初始化不应创建文件")

        try Data("{".utf8).write(to: file)
        let broken = QuotaLedger(url: file, now: t0)
        check(broken.usage(at: t0).usagePercent == 100, "非法 JSON 账本应 fail-safe 为 usagePercent=100")
        let corruptURL = root.appendingPathComponent("quota-ledger.json.corrupt")
        check(FileManager.default.fileExists(atPath: corruptURL.path), "非法 JSON 应改名为 .corrupt")
        check((try? String(contentsOf: corruptURL, encoding: .utf8)) == "{", "损坏原文件应原样保留为 .corrupt")
        let rebuiltPerm = (try? FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber)?.intValue ?? 0
        check(rebuiltPerm & 0o777 == 0o600, "重建后的账本权限应为 0600，实际 \(String(rebuiltPerm, radix: 8))")
        let reloaded = QuotaLedger(url: file, now: t0.addingTimeInterval(10))
        check(reloaded.usage(at: t0.addingTimeInterval(10)).usagePercent == 100, "饱和标记应持久化")
        check(reloaded.usage(at: t0.addingTimeInterval(86_399)).usagePercent == 100, "饱和标记在 24 小时内保持 100%")
        check(reloaded.usage(at: t0.addingTimeInterval(86_401)).usagePercent == 0, "饱和标记超过 24 小时应失效")

        try Data("old-corrupt".utf8).write(to: corruptURL)
        try Data("[".utf8).write(to: file)
        let replaced = QuotaLedger(url: file, now: t0)
        check(replaced.usage(at: t0).usagePercent == 100, "再次损坏的账本仍应饱和")
        check((try? String(contentsOf: corruptURL, encoding: .utf8)) == "[", "已存在的 .corrupt 应被覆盖")

        let mixedURL = root.appendingPathComponent("quota-mixed.json")
        let mixedJSON = #"{"events":[{"ts":10000,"billed_s":-4},{"ts":10000,"billed_s":NaN},{"ts":10000,"billed_s":Infinity},{"ts":10000,"billed_s":-Infinity},{"ts":10000,"billed_s":10},{"ts":10000,"billed_s":12.5}],"request_corrections":[]}"#
        try Data(mixedJSON.utf8).write(to: mixedURL)
        let mixed = QuotaLedger(url: mixedURL, now: t0)
        let mixedUsage = mixed.usage(at: t0)
        check(mixedUsage.dayRequests == 2, "负数与 NaN 事件应被丢弃，实际 \(mixedUsage.dayRequests)")
        check(mixedUsage.dayAudioSeconds == 22.5, "非有限与负数 billed_s 不应累加，实际 \(mixedUsage.dayAudioSeconds)")
        check(!FileManager.default.fileExists(atPath: root.appendingPathComponent("quota-mixed.json.corrupt").path),
              "可过滤的坏事件不应整文件隔离")

        let durURL = root.appendingPathComponent("quota-dur.json")
        let durLedger = QuotaLedger(url: durURL, now: t0)
        try durLedger.recordRequest(duration: 2.3456, at: t0)
        if let data = try? Data(contentsOf: durURL),
           let decoded = try? JSONDecoder().decode(QuotaLedgerSnapshot.self, from: data) {
            check(decoded.events.first?.durS == 2.346, "dur_s 应按 3 位小数入账，实际 \(String(describing: decoded.events.first?.durS))")
            check(decoded.events.first?.billedS == 10, "billed_s 仍按 max(10, duration) 入账")
        } else {
            check(false, "dur_s 用例应能读回账本")
        }

        let oldURL = root.appendingPathComponent("quota-old.json")
        let oldJSON = #"{"events":[{"ts":10000,"billed_s":12.5}],"request_corrections":[]}"#
        try Data(oldJSON.utf8).write(to: oldURL)
        let oldLedger = QuotaLedger(url: oldURL, now: t0)
        check(oldLedger.usage(at: t0).dayAudioSeconds == 12.5, "旧账本无 dur_s 应照常累加 billed_s")
        if let data = try? Data(contentsOf: oldURL),
           let decoded = try? JSONDecoder().decode(QuotaLedgerSnapshot.self, from: data) {
            check(decoded.events.first?.durS == nil, "旧账本事件的 dur_s 应为 nil")
        } else {
            check(false, "旧账本兼容用例应能解码")
        }

        let resetCases: [(String?, Double?)] = [
            ("2m59.56s", 179.56), ("7.66s", 7.66), ("1h2m3s", 3723),
            ("0.5s", 0.5), ("120ms", 0.12), ("abc", nil), ("", nil),
            (nil, nil), ("1h 2m", nil), ("5x", nil),
        ]
        for (raw, expected) in resetCases {
            let actual = QuotaLedger.parseResetRequestsSeconds(raw)
            let matches: Bool
            if let expected {
                matches = actual.map { abs($0 - expected) < 1e-9 } ?? false
            } else {
                matches = actual == nil
            }
            check(matches, "恢复头解析应符合规格：\(raw ?? "nil")")
        }

        let at1000 = Date(timeIntervalSince1970: 1_000)
        let unlock = QuotaLedger(url: root.appendingPathComponent("quota-unlock.json"), now: at1000)
        try unlock.recordRequest(duration: 10, at: at1000)
        try unlock.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000)
        var resetContext = routeContext(.available)
        check(unlock.usage(at: at1000).usagePercent >= 90, "恢复前配额应达到冷却阈值")
        unlock.applyQuotaEvent(to: &resetContext, at: at1000)
        check(resetContext.cloudState == .cooldownQuota, "恢复前应进入额度冷却")
        check(unlock.usage(at: at1000.addingTimeInterval(50)).usagePercent >= 90, "恢复点前不应解锁")
        let afterReset = at1000.addingTimeInterval(101)
        let afterResetUsage = unlock.usage(at: afterReset)
        check(afterResetUsage.dayRequests == 0 && afterResetUsage.usagePercent < 80, "恢复点后旧周期日请求应清零")
        check(afterResetUsage.dayAudioSeconds == 10, "恢复点不应清掉日音频窗口")
        unlock.applyQuotaEvent(to: &resetContext, at: afterReset)
        check(resetContext.cloudState == .available, "恢复点后应通过 90/80 滞回解锁")

        let decrease = QuotaLedger(url: root.appendingPathComponent("quota-decrease.json"), now: at1000)
        try decrease.recordRequest(duration: 10, at: at1000)
        try decrease.correctRemainingRequests(["x-ratelimit-remaining-requests": "5"], at: at1000)
        try decrease.recordRequest(duration: 10, at: at1000.addingTimeInterval(30))
        try decrease.correctRemainingRequests(["x-ratelimit-remaining-requests": "1999"], at: at1000.addingTimeInterval(30))
        check(decrease.usage(at: at1000.addingTimeInterval(30)).dayRequests == 1_995, "60 秒内更小的服务端用量应被忽略")
        try decrease.recordRequest(duration: 10, at: at1000.addingTimeInterval(61))
        try decrease.correctRemainingRequests(["x-ratelimit-remaining-requests": "1999"], at: at1000.addingTimeInterval(61))
        check(decrease.usage(at: at1000.addingTimeInterval(61)).dayRequests == 3, "60 秒后更小的服务端用量应被接受")

        let baselineURL = root.appendingPathComponent("quota-baseline.json")
        let baseline = QuotaLedger(url: baselineURL, now: at1000)
        try baseline.recordRequest(duration: 10, at: at1000)
        try baseline.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000)
        try baseline.recordRequest(duration: 10, at: afterReset)
        try baseline.correctRemainingRequests(["x-ratelimit-remaining-requests": "1900"], at: afterReset)
        check(baseline.usage(at: afterReset).dayRequests == 100, "恢复后无恢复头的 remaining 应成为新周期基线")
        let baselineReloaded = QuotaLedger(url: baselineURL, now: afterReset.addingTimeInterval(1))
        check(baselineReloaded.usage(at: afterReset.addingTimeInterval(1)).dayRequests == 100, "已消费恢复点应持久化，重载后不得重算旧本地事件")

        let at2000 = Date(timeIntervalSince1970: 2_000)
        let persistedURL = root.appendingPathComponent("quota-reset-persisted.json")
        let persisted = QuotaLedger(url: persistedURL, now: at2000)
        try persisted.recordRequest(duration: 10, at: at2000)
        try persisted.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at2000)
        let persistedJSON = try JSONSerialization.jsonObject(with: Data(contentsOf: persistedURL)) as? [String: Any]
        check(persistedJSON?["server_reset_at"] as? Double == 2_100, "恢复时刻应写入 server_reset_at")
        let persistedReloaded = QuotaLedger(url: persistedURL, now: at2000.addingTimeInterval(50))
        check(persistedReloaded.usage(at: at2000.addingTimeInterval(50)).dayRequests == 1_995, "恢复前重载应保留校正")
        check(persistedReloaded.usage(at: at2000.addingTimeInterval(101)).dayRequests == 0, "恢复后重载应清零")

        let oldFormatURL = root.appendingPathComponent("quota-old-format.json")
        try Data(#"{"events":[{"ts":1000,"billed_s":10}],"request_corrections":[{"ts":1000,"used_requests":3}]}"#.utf8).write(to: oldFormatURL)
        let oldFormat = QuotaLedger(url: oldFormatURL, now: at1000)
        check(oldFormat.usage(at: at1000).dayRequests == 3, "没有新字段的旧账本应照常读取")
        check(!FileManager.default.fileExists(atPath: oldFormatURL.appendingPathExtension("corrupt").path), "旧账本不应生成 corrupt")
        let missingKeysURL = root.appendingPathComponent("quota-missing-keys.json")
        try Data("{}".utf8).write(to: missingKeysURL)
        check(QuotaLedger(url: missingKeysURL, now: at1000).usage(at: at1000).usagePercent == 100, "缺 events 键的账本应隔离并饱和")

        let override = QuotaLedger(url: root.appendingPathComponent("quota-reset-override.json"), now: at1000)
        try override.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000)
        try override.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "200s",
        ], at: at1000.addingTimeInterval(10))
        check(override.usage(at: at1000.addingTimeInterval(101)).usagePercent >= 90, "最新恢复头应覆盖旧恢复点")
        check(override.usage(at: at1000.addingTimeInterval(211)).dayRequests == 0, "最新恢复点后应清零")
    } catch {
        check(false, "QuotaLedger selftest 异常：\(error)")
    }
}
