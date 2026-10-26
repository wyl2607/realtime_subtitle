import Foundation

/// 可控单调钟：R4 的 (b) 条件只能靠时间流逝满足，测试必须能手动推进它。
private final class TestMonotonicClock {
    var now: Double

    init(_ now: Double) {
        self.now = now
    }
}

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
        let unlockMono = TestMonotonicClock(1_000)
        let unlock = QuotaLedger(
            url: root.appendingPathComponent("quota-unlock.json"),
            now: at1000,
            monotonicNow: { unlockMono.now }
        )
        try unlock.recordRequest(duration: 10, at: at1000)
        try unlock.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000, keyFingerprint: "key-a")
        var resetContext = routeContext(.available)
        check(unlock.usage(at: at1000).usagePercent >= 90, "恢复前配额应达到冷却阈值")
        unlock.applyQuotaEvent(to: &resetContext, at: at1000)
        check(resetContext.cloudState == .cooldownQuota, "恢复前应进入额度冷却")
        unlockMono.now = 1_050
        check(unlock.usage(at: at1000.addingTimeInterval(50)).usagePercent >= 90, "恢复点前不应解锁")
        let afterReset = at1000.addingTimeInterval(101)
        unlockMono.now = 1_101
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
        let baselineMono = TestMonotonicClock(1_000)
        let baseline = QuotaLedger(url: baselineURL, now: at1000, monotonicNow: { baselineMono.now })
        try baseline.recordRequest(duration: 10, at: at1000)
        try baseline.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000, keyFingerprint: "key-a")
        try baseline.recordRequest(duration: 10, at: afterReset)
        baselineMono.now = 1_101
        try baseline.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "1900",
        ], at: afterReset, keyFingerprint: "key-a", sentAt: afterReset)
        check(baseline.usage(at: afterReset).dayRequests == 100, "恢复后无恢复头的 remaining 应成为新周期基线")
        let baselineReloaded = QuotaLedger(url: baselineURL, now: afterReset.addingTimeInterval(1))
        check(baselineReloaded.usage(at: afterReset.addingTimeInterval(1)).dayRequests == 100, "已消费恢复点应持久化，重载后不得重算旧本地事件")

        // F4：已翻日后收到下一周期的未来 reset，旧周期本地事件不能重新计入 RPD。
        let futureResetURL = root.appendingPathComponent("quota-f4-future-reset.json")
        let oldCycleEvents = (0..<1_800).map { _ in
            QuotaLedgerEvent(ts: at1000.timeIntervalSince1970 - 3_601, billedS: 10)
        }
        try JSONEncoder().encode(QuotaLedgerSnapshot(events: oldCycleEvents)).write(to: futureResetURL)
        let futureResetMono = TestMonotonicClock(0)
        let futureReset = QuotaLedger(
            url: futureResetURL,
            now: at1000,
            monotonicNow: { futureResetMono.now }
        )
        try futureReset.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000, keyFingerprint: "key-a")
        var futureResetContext = routeContext(.available)
        futureReset.applyQuotaEvent(to: &futureResetContext, at: at1000)
        check(futureResetContext.cloudState == .cooldownQuota, "F4 前置：旧周期高用量应先进入冷却")
        let futureResponseAt = afterReset
        futureResetMono.now = 101
        try futureReset.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "1990",
            "x-ratelimit-reset-requests": "100s",
        ], at: futureResponseAt, keyFingerprint: "key-a", sentAt: futureResponseAt)
        let futureResetUsage = futureReset.usage(at: futureResponseAt)
        check(futureResetUsage.dayRequests == 10,
              "F4：翻日后只应保留新周期服务端校正，实际 \(futureResetUsage.dayRequests) 次")
        check(futureResetUsage.usagePercent < 80,
              "F4：翻日后的未来 reset 不得把旧周期事件算回冷却，实际 \(futureResetUsage.usagePercent)%")
        let futureResetJSON = try JSONSerialization.jsonObject(with: Data(contentsOf: futureResetURL)) as? [String: Any]
        check(futureResetJSON?["day_epoch"] as? Double == 1_100,
              "F4：已成立的恢复点应持久化为 day_epoch")
        let epochPruneAt = Date(timeIntervalSince1970: 1_100 + 86_401)
        try futureReset.recordRequest(duration: 10, at: epochPruneAt)
        let prunedEpochJSON = try JSONSerialization.jsonObject(with: Data(contentsOf: futureResetURL)) as? [String: Any]
        check(prunedEpochJSON?["day_epoch"] == nil, "F4：day_epoch 超过 24 小时应随 prune 失效")

        // F1：remaining 仅接受 [0, requestsPerDay] 的整数；越界/非整数不能校正或替换记录。
        let upperRemainingURL = root.appendingPathComponent("quota-f1-upper-remaining.json")
        let upperRemaining = QuotaLedger(url: upperRemainingURL, now: at1000)
        try upperRemaining.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "2000",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000, keyFingerprint: "key-a")
        check(upperRemaining.usage(at: at1000).dayRequests == 0,
              "F1：remaining=2000 应是合法上界")
        let upperRemainingJSON = try JSONSerialization.jsonObject(with: Data(contentsOf: upperRemainingURL)) as? [String: Any]
        check(upperRemainingJSON?["server_reset_at"] as? Double == 1_100,
              "F1：合法上界 remaining 应建立恢复记录")

        let invalidRemainingURL = root.appendingPathComponent("quota-f1-invalid-remaining.json")
        let invalidRemaining = QuotaLedger(url: invalidRemainingURL, now: at1000)
        try invalidRemaining.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "0",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000, keyFingerprint: "key-a")
        try invalidRemaining.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "2001",
            "x-ratelimit-reset-requests": "200s",
        ], at: at1000.addingTimeInterval(1), keyFingerprint: "key-a")
        try invalidRemaining.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "-1",
            "x-ratelimit-reset-requests": "300s",
        ], at: at1000.addingTimeInterval(2), keyFingerprint: "key-a")
        try invalidRemaining.correctRemainingRequests([
            "x-ratelimit-remaining-requests": NSNumber(value: 1.5),
            "x-ratelimit-reset-requests": "400s",
        ], at: at1000.addingTimeInterval(3), keyFingerprint: "key-a")
        let invalidRemainingJSON = try JSONSerialization.jsonObject(with: Data(contentsOf: invalidRemainingURL)) as? [String: Any]
        check(invalidRemaining.usage(at: at1000.addingTimeInterval(3)).dayRequests == 2_000,
              "F1：2001、-1 和非整数 remaining 不得校正日用量")
        check(invalidRemainingJSON?["server_reset_at"] as? Double == 1_100,
              "F1：2001、-1 和非整数 remaining 不得替换恢复记录")

        let at2000 = Date(timeIntervalSince1970: 2_000)
        let persistedURL = root.appendingPathComponent("quota-reset-persisted.json")
        let persisted = QuotaLedger(url: persistedURL, now: at2000)
        try persisted.recordRequest(duration: 10, at: at2000)
        try persisted.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at2000, keyFingerprint: "key-a")
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

        let overrideMono = TestMonotonicClock(1_000)
        let override = QuotaLedger(
            url: root.appendingPathComponent("quota-reset-override.json"),
            now: at1000,
            monotonicNow: { overrideMono.now }
        )
        try override.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "100s",
        ], at: at1000, keyFingerprint: "key-a")
        overrideMono.now = 1_010
        try override.correctRemainingRequests([
            "x-ratelimit-remaining-requests": "5",
            "x-ratelimit-reset-requests": "200s",
        ], at: at1000.addingTimeInterval(10), keyFingerprint: "key-a")
        overrideMono.now = 1_101
        check(override.usage(at: at1000.addingTimeInterval(101)).usagePercent >= 90, "最新恢复头应覆盖旧恢复点")
        overrideMono.now = 1_211
        check(override.usage(at: at1000.addingTimeInterval(211)).dayRequests == 0, "最新恢复点后应清零")

        // MARK: 可信 RPD 翻日信任门（R1–R7）

        let base = Date(timeIntervalSince1970: 10_000)
        let reset = base.addingTimeInterval(100)

        func storedRecord(_ url: URL) -> [String: Any]? {
            guard let data = try? Data(contentsOf: url),
                  let object = try? JSONSerialization.jsonObject(with: data),
                  let dictionary = object as? [String: Any] else { return nil }
            return dictionary
        }

        // 1. 两个头同一条响应才建记录；只带其一不建。
        do {
            let bothURL = root.appendingPathComponent("quota-trust-both.json")
            let mono = TestMonotonicClock(0)
            let both = QuotaLedger(url: bothURL, now: base, monotonicNow: { mono.now })
            try both.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            let json = storedRecord(bothURL)
            check(json?["server_reset_at"] as? Double == 10_100, "两个头同响应应写入 reset_at")
            check(json?["server_reset_observed_at"] as? Double == 10_000, "两个头同响应应写入 observed_at")
            check(json?["server_reset_key_fp"] as? String == "key-a", "恢复记录应绑定 key 指纹")
            check(json?["server_reset_consumed"] as? Bool == false, "新恢复记录 consumed 应为 false")

            let onlyResetURL = root.appendingPathComponent("quota-trust-only-reset.json")
            let onlyReset = QuotaLedger(url: onlyResetURL, now: base)
            try onlyReset.correctRemainingRequests([
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            check(storedRecord(onlyResetURL)?["server_reset_at"] == nil, "只有 reset 头不应建恢复记录")

            let onlyRemainingURL = root.appendingPathComponent("quota-trust-only-remaining.json")
            let onlyRemaining = QuotaLedger(url: onlyRemainingURL, now: base)
            try onlyRemaining.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
            ], at: base, keyFingerprint: "key-a")
            check(storedRecord(onlyRemainingURL)?["server_reset_at"] == nil, "只有 remaining 头不应建恢复记录")
            check(onlyRemaining.usage(at: base).dayRequests == 1_995, "只剩 remaining 时仍应校正日请求数")

            let tooLongURL = root.appendingPathComponent("quota-trust-too-long.json")
            let tooLong = QuotaLedger(url: tooLongURL, now: base)
            try tooLong.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "25h",
            ], at: base, keyFingerprint: "key-a")
            check(storedRecord(tooLongURL)?["server_reset_at"] == nil, "超过 24h+60s 的 reset 应视为非法")
        }

        // 2. 恢复点前更大的 remaining（用量变小）不触发翻日，也不发 reset 事件。
        do {
            let url = root.appendingPathComponent("quota-trust-before.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            _ = ledger.usage(at: base)
            mono.now = 10
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "1999",
            ], at: base.addingTimeInterval(10), keyFingerprint: "key-a", sentAt: base.addingTimeInterval(10))
            let event = ledger.routeEvent(at: base.addingTimeInterval(10))
            check(event.type == "quota_update", "恢复点前更大的 remaining 不应发 quota_reset_confirmed")
            check(ledger.usage(at: base.addingTimeInterval(10)).dayRequests == 1_995, "恢复点前仍应保留下限")
        }

        // 3. 过恢复点（墙钟+单调钟）且此前进过冷却：恰好一次 reset 事件，百分比取四窗口最大。
        do {
            let url = root.appendingPathComponent("quota-trust-confirm.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            for _ in 0..<18 {
                try ledger.recordRequest(duration: 10, at: base)
            }
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            let hotEvent = ledger.routeEvent(at: base)
            check(hotEvent.type == "quota_update" && (hotEvent.usagePercent ?? 0) >= 90, "恢复前应先把额度打进冷却")

            mono.now = 101
            let confirmed = ledger.routeEvent(at: reset.addingTimeInterval(1))
            check(confirmed.type == "quota_reset_confirmed", "过恢复点后应发 quota_reset_confirmed")
            check(confirmed.usagePercent == ledger.usage(at: reset.addingTimeInterval(1)).usagePercent,
                  "reset 事件的 usage_percent 应是四窗口最大值")
            let second = ledger.routeEvent(at: reset.addingTimeInterval(2))
            check(second.type == "quota_update", "同一恢复记录只能触发一次 reset 事件")

            var context = routeContext(.cooldownQuota)
            context.apply(confirmed)
            check(context.cloudState == .available, "reset 事件在冷却中且 <80% 时应放行云端")
        }

        // 4. 墙钟回拨 → 丢记录、不发事件，日请求仍含服务端下限。
        do {
            let url = root.appendingPathComponent("quota-trust-rollback.json")
            let mono = TestMonotonicClock(10_000)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            _ = ledger.usage(at: base)
            let rolledBack = base.addingTimeInterval(-10)
            check(ledger.takeConfirmedReset(at: rolledBack) == nil, "墙钟回拨不应触发翻日")
            check(ledger.usage(at: rolledBack).dayRequests == 1_995, "墙钟回拨后仍应保留下限")
            check(storedRecord(url)?["server_reset_at"] == nil, "墙钟回拨应丢弃恢复记录")
        }

        // 5. 墙钟前跳但单调钟没走够 → 不算翻日。
        do {
            let url = root.appendingPathComponent("quota-trust-mono.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            _ = ledger.usage(at: base)
            mono.now = 5
            check(ledger.takeConfirmedReset(at: reset.addingTimeInterval(1)) == nil,
                  "单调钟未流逝足够时不算翻日")
            check(ledger.usage(at: reset.addingTimeInterval(1)).dayRequests == 1_995, "未翻日时仍应含下限")
            check(storedRecord(url)?["server_reset_at"] as? Double == 10_100, "未翻日不应丢弃记录")
        }

        // 6. key_fp 变化 → 旧记录丢弃、下限保留；key_fp=None 不建记录。
        do {
            let url = root.appendingPathComponent("quota-trust-key.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            mono.now = 1
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "10",
                "x-ratelimit-reset-requests": "100s",
            ], at: base.addingTimeInterval(1), keyFingerprint: "key-b")
            check(storedRecord(url)?["server_reset_key_fp"] as? String == "key-b", "换 key 应重建恢复记录")
            check(ledger.usage(at: base.addingTimeInterval(1)).dayRequests == 1_995, "换 key 应保留服务端用量下限")

            let nilURL = root.appendingPathComponent("quota-trust-key-nil.json")
            let nilLedger = QuotaLedger(url: nilURL, now: base)
            try nilLedger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "7",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: nil)
            check(storedRecord(nilURL)?["server_reset_at"] == nil, "key_fp=None 不应建立恢复记录")
            check(nilLedger.usage(at: base).dayRequests == 1_993, "key_fp=None 仍应校正日请求数")
        }

        // 7. 翻日后 sent_at 早于恢复点 / 未知的迟到响应头不采信。
        do {
            let url = root.appendingPathComponent("quota-trust-late.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            mono.now = 101
            let after = reset.addingTimeInterval(1)
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "500s",
            ], at: after, keyFingerprint: "key-a", sentAt: base.addingTimeInterval(50))
            check(storedRecord(url)?["server_reset_at"] as? Double == 10_100, "sent_at<reset_at 的迟到头不得改锚点")
            check(ledger.usage(at: after).dayRequests == 0, "sent_at 早于恢复点的 remaining 不得采信")

            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "0",
                "x-ratelimit-reset-requests": "300s",
            ], at: after, keyFingerprint: "key-a", sentAt: nil)
            check(storedRecord(url)?["server_reset_at"] as? Double == 10_100, "sent_at=None 的迟到头不得改锚点")
            check(ledger.usage(at: after).dayRequests == 0, "sent_at=None 的 remaining 不得采信")
        }

        // 8. 翻日只清 RPD 服务端分量，1 分钟/1 小时/24h 音频窗口不动。
        do {
            let url = root.appendingPathComponent("quota-trust-windows.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.recordRequest(duration: 12, at: base)
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "4s",
            ], at: base, keyFingerprint: "key-a")
            mono.now = 3
            let before = ledger.usage(at: base.addingTimeInterval(3))
            mono.now = 5
            let after = ledger.usage(at: base.addingTimeInterval(4))
            check(before.minuteRequests == after.minuteRequests, "翻日不应改 1 分钟请求窗口")
            check(before.hourAudioSeconds == after.hourAudioSeconds, "翻日不应改 1 小时音频窗口")
            check(before.dayAudioSeconds == after.dayAudioSeconds, "翻日不应改 24 小时音频窗口")
            check(after.dayRequests == 0, "翻日应清掉 RPD 服务端分量")
        }

        // 9. 落盘往返 + 旧格式可读 + 记录字段非法只丢记录不隔离。
        do {
            let url = root.appendingPathComponent("quota-trust-persist.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            _ = ledger.usage(at: base)
            mono.now = 101
            check(ledger.routeEvent(at: reset.addingTimeInterval(1)).type == "quota_reset_confirmed",
                  "落盘用例应先发一次 reset 事件")
            let json = storedRecord(url)
            check(json?["server_reset_consumed"] as? Bool == true, "consumed 应落盘")
            check(json?["server_reset_key_fp"] as? String == "key-a", "key_fp 应落盘")
            check(json?["server_reset_observed_at"] as? Double == 10_000, "observed_at 应落盘")
            let reloaded = QuotaLedger(url: url, now: reset.addingTimeInterval(2))
            check(reloaded.takeConfirmedReset(at: reset.addingTimeInterval(2)) == nil,
                  "已消费的恢复记录重启后不得重复发 reset 事件")
            check(reloaded.usage(at: reset.addingTimeInterval(2)).dayRequests == 0, "重启后仍按恢复点算 RPD")

            let invalidURL = root.appendingPathComponent("quota-trust-invalid-record.json")
            let invalidJSON = #"{"events":[{"ts":10000,"billed_s":10}],"request_corrections":[],"server_reset_at":"bad","server_reset_observed_at":10000,"server_reset_key_fp":"key-a","server_reset_consumed":true}"#
            try Data(invalidJSON.utf8).write(to: invalidURL)
            let invalid = QuotaLedger(url: invalidURL, now: base)
            check(invalid.usage(at: base).dayRequests == 1, "非法恢复记录不应影响事件解码")
            check(storedRecord(invalidURL)?["server_reset_at"] == nil, "非法恢复记录应被丢弃")
            check(!FileManager.default.fileExists(atPath: root.appendingPathComponent("quota-trust-invalid-record.json.corrupt").path),
                  "非法恢复记录不应隔离整份账本")
        }

        // 10. 重启后没有单调钟 → 只看 R4a；consumed 防重复发。
        do {
            let url = root.appendingPathComponent("quota-trust-restart.json")
            let mono = TestMonotonicClock(0)
            let ledger = QuotaLedger(url: url, now: base, monotonicNow: { mono.now })
            try ledger.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            let restarted = QuotaLedger(url: url, now: reset.addingTimeInterval(1))
            check(restarted.rolloverConfirmed(at: reset.addingTimeInterval(1)), "重启后应按 R4a 判定翻日")
            check(restarted.usage(at: reset.addingTimeInterval(1)).dayRequests == 0, "重启后过恢复点应清 RPD")

            let consumedURL = root.appendingPathComponent("quota-trust-restart-consumed.json")
            let mono2 = TestMonotonicClock(0)
            let first = QuotaLedger(url: consumedURL, now: base, monotonicNow: { mono2.now })
            for _ in 0..<18 {
                try first.recordRequest(duration: 10, at: base)
            }
            try first.correctRemainingRequests([
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ], at: base, keyFingerprint: "key-a")
            _ = first.usage(at: base)
            mono2.now = 101
            check(first.routeEvent(at: reset.addingTimeInterval(1)).type == "quota_reset_confirmed",
                  "重启用例应先发一次 reset 事件")
            let afterRestart = QuotaLedger(url: consumedURL, now: reset.addingTimeInterval(2))
            check(afterRestart.routeEvent(at: reset.addingTimeInterval(2)).type == "quota_update",
                  "consumed 落盘后重启不得再发 quota_reset_confirmed")
        }
    } catch {
        check(false, "QuotaLedger selftest 异常：\(error)")
    }
}
