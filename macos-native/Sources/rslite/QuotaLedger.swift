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

    enum CodingKeys: String, CodingKey {
        case ts
        case billedS = "billed_s"
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
        if let data = try? Data(contentsOf: url),
           let decoded = try? JSONDecoder().decode(QuotaLedgerSnapshot.self, from: data) {
            snapshot = Self.pruned(decoded, now: now)
        } else {
            snapshot = QuotaLedgerSnapshot()
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
        snapshot.events.append(QuotaLedgerEvent(ts: now.timeIntervalSince1970, billedS: billed))
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
        let used = max(0, limits.requestsPerMinute - remaining)
        snapshot.requestCorrections.append(QuotaRequestCorrection(ts: now.timeIntervalSince1970, usedRequests: used))
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
        let correctedMinute = current.requestCorrections
            .filter { ts - $0.ts < 60 }
            .map(\.usedRequests)
            .max() ?? 0
        let minuteRequests = max(minuteEvents.count, correctedMinute)
        let hourAudio = hourEvents.reduce(0) { $0 + $1.billedS }
        let dayRequests = dayEvents.count
        let dayAudio = dayEvents.reduce(0) { $0 + $1.billedS }
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
            events: snapshot.events.filter { $0.ts >= cutoff },
            requestCorrections: snapshot.requestCorrections.filter { $0.ts >= cutoff }
        )
    }

    private static func remainingRequests(from headers: [AnyHashable: Any]) -> Int? {
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
            check(pruned.usage(at: t0).minuteRequests == 18, "remaining requests 响应头应校正分钟请求数")
        } catch {
            failures.append("QuotaLedger selftest 异常：\(error)")
        }

        return failures
    }
}
