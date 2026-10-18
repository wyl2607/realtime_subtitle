import Darwin
import Foundation

struct QuotaLedgerLimits: Codable, Equatable {
    var requestsPerMinute: Int = 20
    var audioSecondsPerHour: Double = 7_200
    var requestsPerDay: Int = 2_000
    var audioSecondsPerDay: Double = 28_800
}

struct QuotaUsage: Equatable {
    var minuteRequests: Int
    var hourAudioSeconds: Double
    var dayRequests: Int
    var dayAudioSeconds: Double
    var usagePercent: Double
}

struct QuotaLedgerSnapshot: Codable, Equatable {
    var events: [QuotaLedgerEvent] = []
    var requestCorrections: [QuotaRequestCorrection] = []

    enum CodingKeys: String, CodingKey {
        case events
        case requestCorrections = "request_corrections"
    }
}

struct QuotaLedgerEvent: Codable, Equatable {
    var ts: Double
    var billedS: Double
    /// 实际音频秒（round 3 位）。旧账本没有这个字段，读出时是 nil。
    var durS: Double?

    enum CodingKeys: String, CodingKey {
        case ts
        case billedS = "billed_s"
        case durS = "dur_s"
    }

    init(ts: Double, billedS: Double, durS: Double? = nil) {
        self.ts = ts
        self.billedS = billedS
        self.durS = durS
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        ts = try container.decode(Double.self, forKey: .ts)
        // 非法数字（null / 非数字 / NaN 文本被换成 null）先记成 NaN，解码后丢弃。
        if let value = try? container.decode(Double.self, forKey: .billedS) {
            billedS = value
        } else {
            billedS = .nan
        }
        // dur_s 是可选：旧账本没有它，NaN/Infinity 被换成 null 后也按缺失处理。
        durS = try? container.decodeIfPresent(Double.self, forKey: .durS)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(ts, forKey: .ts)
        try container.encode(billedS, forKey: .billedS)
        try container.encodeIfPresent(durS, forKey: .durS)
    }
}

struct QuotaRequestCorrection: Codable, Equatable {
    var ts: Double
    var usedRequests: Int

    enum CodingKeys: String, CodingKey {
        case ts
        case usedRequests = "used_requests"
    }
}

final class QuotaLedger: @unchecked Sendable {
    private let url: URL
    private let limits: QuotaLedgerLimits
    private let lock = NSLock()
    private var snapshot: QuotaLedgerSnapshot

    init(
        url: URL = QuotaLedger.defaultURL,
        limits: QuotaLedgerLimits = QuotaLedgerLimits(),
        now: Date = Date()
    ) {
        self.url = url
        self.limits = limits
        // 文件不存在是空账本。文件在，但读不到或解不开，才隔离并饱和 24 小时。
        guard FileManager.default.fileExists(atPath: url.path) else {
            snapshot = QuotaLedgerSnapshot()
            return
        }
        do {
            let data = try Data(contentsOf: url)
            snapshot = Self.pruned(try Self.decodeSnapshot(data), now: now)
        } catch {
            let quarantined = Self.quarantine(url)
            snapshot = Self.saturatedSnapshot(now: now, requestLimit: limits.requestsPerDay)
            if quarantined {
                try? persistLocked()
            }
        }
    }

    static var defaultURL: URL {
        CloudConfigStore.defaultDirectory.appendingPathComponent("quota-ledger.json")
    }

    @discardableResult
    func recordRequest(duration: Double, at now: Date = Date()) throws -> Double {
        lock.lock()
        defer { lock.unlock() }
        let billed = max(10, duration)
        snapshot.events.append(QuotaLedgerEvent(ts: now.timeIntervalSince1970, billedS: billed, durS: Self.round3(duration)))
        snapshot = Self.pruned(snapshot, now: now)
        try persistLocked()
        return billed
    }

    func correctRemainingRequests(_ headers: [AnyHashable: Any], at now: Date = Date()) throws {
        guard let remaining = Self.remainingRequests(from: headers) else {
            return
        }
        lock.lock()
        defer { lock.unlock() }
        // x-ratelimit-remaining-requests 是每日剩余（上限 requestsPerDay），不是每分钟。
        let usedDay = max(0, limits.requestsPerDay - remaining)
        snapshot.requestCorrections.append(
            QuotaRequestCorrection(ts: now.timeIntervalSince1970, usedRequests: usedDay)
        )
        snapshot = Self.pruned(snapshot, now: now)
        try persistLocked()
    }

    func usage(at now: Date = Date()) -> QuotaUsage {
        lock.withLockValue {
            usageLocked(now: now)
        }
    }

    func applyQuotaEvent(to context: inout CloudRouteContext, at now: Date = Date()) {
        let percent = usage(at: now).usagePercent
        context.apply(RouteEvent(type: "quota_update", status: nil, retryAfter: nil, usagePercent: percent))
    }

    private func usageLocked(now: Date) -> QuotaUsage {
        let current = Self.pruned(snapshot, now: now)
        let ts = now.timeIntervalSince1970
        let minuteEvents = current.events.filter { ts - $0.ts < 60 }
        let hourEvents = current.events.filter { ts - $0.ts < 3_600 }
        let dayEvents = current.events
        // 1 分钟窗口只信本地计数。响应头校正只抬高 24 小时请求数（校正记录已按 24h prune）。
        let minuteRequests = minuteEvents.count
        let correctedDay = current.requestCorrections.map(\.usedRequests).max() ?? 0
        let hourAudio = hourEvents.reduce(0.0) { $0 + $1.billedS }
        let dayRequests = max(dayEvents.count, correctedDay)
        let dayAudio = dayEvents.reduce(0.0) { $0 + $1.billedS }
        let percentages = [
            Double(minuteRequests) / Double(limits.requestsPerMinute),
            hourAudio / limits.audioSecondsPerHour,
            Double(dayRequests) / Double(limits.requestsPerDay),
            dayAudio / limits.audioSecondsPerDay,
        ]
        return QuotaUsage(
            minuteRequests: minuteRequests,
            hourAudioSeconds: hourAudio,
            dayRequests: dayRequests,
            dayAudioSeconds: dayAudio,
            usagePercent: (percentages.max() ?? 0) * 100
        )
    }

    private func persistLocked() throws {
        let data = try JSONEncoder().encode(snapshot)
        try CloudConfigStore.atomicWrite(data, to: url, mode: 0o600)
    }

    private static func pruned(_ snapshot: QuotaLedgerSnapshot, now: Date) -> QuotaLedgerSnapshot {
        let cutoff = now.timeIntervalSince1970 - 86_400
        return QuotaLedgerSnapshot(
            events: snapshot.events.filter { $0.ts >= cutoff && isBillable($0) },
            requestCorrections: snapshot.requestCorrections.filter { $0.ts >= cutoff }
        )
    }

    private static func isBillable(_ event: QuotaLedgerEvent) -> Bool {
        event.billedS.isFinite && event.billedS >= 0
    }

    private static func saturatedSnapshot(now: Date, requestLimit: Int) -> QuotaLedgerSnapshot {
        QuotaLedgerSnapshot(
            events: [],
            requestCorrections: [
                QuotaRequestCorrection(ts: now.timeIntervalSince1970, usedRequests: requestLimit),
            ]
        )
    }

    private static func decodeSnapshot(_ data: Data) throws -> QuotaLedgerSnapshot {
        try JSONDecoder().decode(QuotaLedgerSnapshot.self, from: normalizingNonFiniteTokens(data))
    }

    /// JSON 不允许 NaN / Infinity。账本里出现这些记号时，换成 null，让同文件里的其它事件还能解码。
    private static func normalizingNonFiniteTokens(_ data: Data) -> Data {
        let bytes = [UInt8](data)
        var out: [UInt8] = []
        out.reserveCapacity(bytes.count)
        let tokens: [[UInt8]] = [
            Array("-Infinity".utf8),
            Array("+Infinity".utf8),
            Array("Infinity".utf8),
            Array("NaN".utf8),
        ]
        let nullToken = Array("null".utf8)
        var inString = false
        var escaped = false
        var index = 0
        while index < bytes.count {
            let byte = bytes[index]
            if inString {
                out.append(byte)
                if escaped {
                    escaped = false
                } else if byte == UInt8(ascii: "\\") {
                    escaped = true
                } else if byte == UInt8(ascii: "\"") {
                    inString = false
                }
                index += 1
                continue
            }
            if byte == UInt8(ascii: "\"") {
                inString = true
                out.append(byte)
                index += 1
                continue
            }
            let previous = index == 0 ? nil : bytes[index - 1]
            var replaced = false
            if isJSONBoundary(previous) {
                for token in tokens where bytes[index...].starts(with: token) {
                    let end = index + token.count
                    if end == bytes.count || isJSONBoundary(bytes[end]) {
                        out.append(contentsOf: nullToken)
                        index = end
                        replaced = true
                        break
                    }
                }
            }
            if replaced { continue }
            out.append(byte)
            index += 1
        }
        return Data(out)
    }

    private static func isJSONBoundary(_ byte: UInt8?) -> Bool {
        guard let byte else { return true }
        switch byte {
        case UInt8(ascii: " "), UInt8(ascii: "\n"), UInt8(ascii: "\r"), UInt8(ascii: "\t"),
             UInt8(ascii: ","), UInt8(ascii: ":"), UInt8(ascii: "["), UInt8(ascii: "]"),
             UInt8(ascii: "{"), UInt8(ascii: "}"):
            return true
        default:
            return false
        }
    }

    /// 覆盖已有的 `.corrupt`。rename 在同一目录内会原子替换目标。
    @discardableResult
    private static func quarantine(_ url: URL) -> Bool {
        let corrupt = url.deletingLastPathComponent()
            .appendingPathComponent(url.lastPathComponent + ".corrupt")
        return rename(url.path, corrupt.path) == 0
    }

    static func remainingRequests(from headers: [AnyHashable: Any]) -> Int? {
        for (key, value) in headers {
            guard String(describing: key).lowercased() == "x-ratelimit-remaining-requests" else {
                continue
            }
            if let number = value as? NSNumber {
                return number.intValue
            }
            return Int(String(describing: value).trimmingCharacters(in: .whitespacesAndNewlines))
        }
        return nil
    }

    private static func round3(_ value: Double) -> Double {
        (value * 1000).rounded() / 1000
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("rslite-quota-\(UUID().uuidString)")
        let file = dir.appendingPathComponent("quota-ledger.json")
        defer { try? FileManager.default.removeItem(at: dir) }

        do {
            let t0 = Date(timeIntervalSince1970: 10_000)
            let ledger = QuotaLedger(url: file, now: t0)
            let billed = try ledger.recordRequest(duration: 3, at: t0)
            check(billed == 10, "计费下限应为 10 秒")
            check(ledger.usage(at: t0).dayAudioSeconds == 10, "短音频应按 10 秒入账")

            for i in 1..<18 {
                try ledger.recordRequest(duration: 10, at: t0.addingTimeInterval(Double(i)))
            }
            var context = CloudRouteContext(RouteInitial(
                hasKey: true, cloudEnabled: true, cloudState: .available,
                nodeEnabled: false, nodeHealthy: false, nodeIsSelf: false,
                consecutiveFailures: 0, backoffIndex: 0
            ))
            ledger.applyQuotaEvent(to: &context, at: t0.addingTimeInterval(20))
            check(context.cloudState == .cooldownQuota, "达到 90% 应进入额度冷却")

            let later = t0.addingTimeInterval(80)
            ledger.applyQuotaEvent(to: &context, at: later)
            check(context.cloudState == .available, "1 分钟窗口落到 80% 以下应恢复")
            check(ledger.usage(at: later).minuteRequests == 0, "1 分钟窗口应滚动清零")

            try ledger.recordRequest(duration: 20, at: t0.addingTimeInterval(3_700))
            let usageAfterHour = ledger.usage(at: t0.addingTimeInterval(3_701))
            check(usageAfterHour.hourAudioSeconds == 20, "1 小时音频窗口应滚动")
            check(usageAfterHour.dayRequests == 19, "24 小时窗口内请求不应清零")

            let restarted = QuotaLedger(url: file, now: t0.addingTimeInterval(3_702))
            check(restarted.usage(at: t0.addingTimeInterval(3_702)).dayRequests == 19, "持久化重启不应清零")

            let oldSnapshot = QuotaLedgerSnapshot(events: [
                QuotaLedgerEvent(ts: t0.timeIntervalSince1970 - 90_000, billedS: 99),
                QuotaLedgerEvent(ts: t0.timeIntervalSince1970, billedS: 10),
            ])
            try CloudConfigStore.atomicWrite(try JSONEncoder().encode(oldSnapshot), to: file, mode: 0o600)
            let pruned = QuotaLedger(url: file, now: t0)
            check(pruned.usage(at: t0).dayRequests == 1, "超过 24 小时事件应丢弃")

            try pruned.correctRemainingRequests(["x-ratelimit-remaining-requests": "2"], at: t0)
            check(pruned.usage(at: t0).minuteRequests == 1, "每日 remaining 不应影响 minuteRequests")
            check(pruned.usage(at: t0).dayRequests == 1_998, "remaining=2 应校正 24 小时请求数")
            let aged = pruned.usage(at: t0.addingTimeInterval(86_401))
            check(aged.dayRequests == 0, "校正记录应随 24 小时窗口丢弃")
            check(aged.minuteRequests == 0, "过期校正不应进入分钟窗口")

            let daily = QuotaLedger(url: dir.appendingPathComponent("quota-daily.json"), now: t0)
            try daily.recordRequest(duration: 10, at: t0)
            try daily.correctRemainingRequests(["x-ratelimit-remaining-requests": "1980"], at: t0)
            let dailyUsage = daily.usage(at: t0)
            check(dailyUsage.dayRequests >= 20, "remaining=1980 时 dayRequests 应至少为 20")
            check(dailyUsage.minuteRequests == 1, "remaining=1980 不应影响 minuteRequests")

            let missingURL = dir.appendingPathComponent("missing-quota.json")
            let missing = QuotaLedger(url: missingURL, now: t0)
            check(missing.usage(at: t0).usagePercent == 0, "账本文件不存在时应为空账本")
            check(missing.usage(at: t0).dayRequests == 0, "账本文件不存在时不应饱和")
            check(!FileManager.default.fileExists(atPath: missingURL.path), "空账本初始化不应创建文件")

            try Data("{".utf8).write(to: file)
            let broken = QuotaLedger(url: file, now: t0)
            check(broken.usage(at: t0).usagePercent == 100, "非法 JSON 账本应 fail-safe 为 usagePercent=100")
            let corruptURL = dir.appendingPathComponent("quota-ledger.json.corrupt")
            check(FileManager.default.fileExists(atPath: corruptURL.path), "非法 JSON 应改名为 quota-ledger.json.corrupt")
            check(try String(contentsOf: corruptURL, encoding: .utf8) == "{", "损坏原文件应原样保留为 .corrupt")
            let rebuiltPerm = (try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber)?.intValue ?? 0
            check(rebuiltPerm & 0o777 == 0o600, "重建后的账本权限应为 0600")
            let reloaded = QuotaLedger(url: file, now: t0.addingTimeInterval(10))
            check(reloaded.usage(at: t0.addingTimeInterval(10)).usagePercent == 100, "饱和标记应持久化")
            check(reloaded.usage(at: t0.addingTimeInterval(86_399)).usagePercent == 100, "饱和标记在 24 小时内保持 100%")
            check(reloaded.usage(at: t0.addingTimeInterval(86_401)).usagePercent == 0, "饱和标记超过 24 小时应失效")

            try Data("old-corrupt".utf8).write(to: corruptURL)
            try Data("[".utf8).write(to: file)
            let replaced = QuotaLedger(url: file, now: t0)
            check(replaced.usage(at: t0).usagePercent == 100, "再次损坏的账本仍应饱和")
            check(try String(contentsOf: corruptURL, encoding: .utf8) == "[", "已存在的 .corrupt 应被覆盖")

            let mixedURL = dir.appendingPathComponent("quota-mixed.json")
            let mixedJSON = #"{"events":[{"ts":10000,"billed_s":-4},{"ts":10000,"billed_s":NaN},{"ts":10000,"billed_s":Infinity},{"ts":10000,"billed_s":-Infinity},{"ts":10000,"billed_s":10},{"ts":10000,"billed_s":12.5}],"request_corrections":[]}"#
            try Data(mixedJSON.utf8).write(to: mixedURL)
            let mixed = QuotaLedger(url: mixedURL, now: t0)
            let mixedUsage = mixed.usage(at: t0)
            check(mixedUsage.dayRequests == 2, "负数与 NaN 事件应被丢弃")
            check(mixedUsage.dayAudioSeconds == 22.5, "非有限与负数 billed_s 不应累加")
            check(FileManager.default.fileExists(atPath: mixedURL.path), "可过滤的坏事件不应整文件隔离")
            check(!FileManager.default.fileExists(atPath: dir.appendingPathComponent("quota-mixed.json.corrupt").path), "负数/NaN 事件不应生成 .corrupt")

            // 新字段：recordRequest 应把实际音频秒按 3 位小数写进 dur_s，billed_s 仍按 max(10, duration)。
            let durURL = dir.appendingPathComponent("quota-dur.json")
            let durLedger = QuotaLedger(url: durURL, now: t0)
            try durLedger.recordRequest(duration: 2.3456, at: t0)
            if let data = try? Data(contentsOf: durURL),
               let decoded = try? JSONDecoder().decode(QuotaLedgerSnapshot.self, from: data) {
                check(decoded.events.first?.durS == 2.346, "dur_s 应按 3 位小数入账，实际 \(String(describing: decoded.events.first?.durS))")
                check(decoded.events.first?.billedS == 10, "billed_s 仍按 max(10, duration) 入账")
            } else {
                check(false, "dur_s 新字段用例应能读回账本")
            }

            // 旧账本兼容：没有 dur_s 的事件照常读出，dur_s 为 nil。
            let oldURL = dir.appendingPathComponent("quota-old.json")
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
        } catch {
            failures.append("QuotaLedger selftest 异常：\(error)")
        }

        return failures
    }
}
