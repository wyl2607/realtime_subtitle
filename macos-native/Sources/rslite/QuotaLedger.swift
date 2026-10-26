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
    var events: [QuotaLedgerEvent]
    var requestCorrections: [QuotaRequestCorrection]
    /// 已消费的恢复时刻；避免旧周期的本地事件在锚点被清除后重新计入日用量。
    var dayEpoch: Double?
    /// 可信 RPD 恢复记录。四个字段必须同时有效，否则按没有记录处理。
    var resetAt: Double?
    var observedAt: Double?
    var resetKeyFingerprint: String?
    var resetConsumed: Bool?

    enum CodingKeys: String, CodingKey {
        case events
        case requestCorrections = "request_corrections"
        case dayEpoch = "day_epoch"
        case resetAt = "reset_at"
        case observedAt = "observed_at"
        case resetKeyFingerprint = "key_fp"
        case resetConsumed = "consumed"
    }

    init(
        events: [QuotaLedgerEvent] = [],
        requestCorrections: [QuotaRequestCorrection] = [],
        dayEpoch: Double? = nil,
        resetAt: Double? = nil,
        observedAt: Double? = nil,
        resetKeyFingerprint: String? = nil,
        resetConsumed: Bool? = nil
    ) {
        self.events = events
        self.requestCorrections = requestCorrections
        self.dayEpoch = dayEpoch
        self.resetAt = resetAt
        self.observedAt = observedAt
        self.resetKeyFingerprint = resetKeyFingerprint
        self.resetConsumed = resetConsumed
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // 两个列表仍是必填：缺键说明文件坏了，要走隔离 + 饱和，不能读成空账本。
        events = try container.decode([QuotaLedgerEvent].self, forKey: .events)
        requestCorrections = try container.decode([QuotaRequestCorrection].self, forKey: .requestCorrections)
        dayEpoch = try container.decodeIfPresent(Double.self, forKey: .dayEpoch)
        // 恢复记录是可选扩展：任一字段损坏只丢记录，绝不能隔离整份账本。
        let resetAt = try? container.decodeIfPresent(Double.self, forKey: .resetAt)
        let observedAt = try? container.decodeIfPresent(Double.self, forKey: .observedAt)
        let keyFP = try? container.decodeIfPresent(String.self, forKey: .resetKeyFingerprint)
        let consumed = try? container.decodeIfPresent(Bool.self, forKey: .resetConsumed)
        if let resetAt, let observedAt, let keyFP, !keyFP.isEmpty, let consumed,
           resetAt.isFinite, observedAt.isFinite {
            self.resetAt = resetAt
            self.observedAt = observedAt
            resetKeyFingerprint = keyFP
            resetConsumed = consumed
        } else {
            self.resetAt = nil
            self.observedAt = nil
            resetKeyFingerprint = nil
            resetConsumed = nil
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(events, forKey: .events)
        try container.encode(requestCorrections, forKey: .requestCorrections)
        try container.encodeIfPresent(dayEpoch, forKey: .dayEpoch)
        try container.encodeIfPresent(resetAt, forKey: .resetAt)
        try container.encodeIfPresent(observedAt, forKey: .observedAt)
        try container.encodeIfPresent(resetKeyFingerprint, forKey: .resetKeyFingerprint)
        try container.encodeIfPresent(resetConsumed, forKey: .resetConsumed)
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
    /// 单调钟只在当前进程有效，故绝不写入账本。
    private var observedResetMonotonic: Double?
    private var quotaHot = false
    private var resetConfirmationPending = false

    init(
        url: URL = QuotaLedger.defaultURL,
        limits: QuotaLedgerLimits = QuotaLedgerLimits(),
        now: Date = Date()
    ) {
        self.url = url
        self.limits = limits
        // 文件不存在是空账本。文件在，但读不到或解不开，才隔离并饱和 24 小时。
        if !FileManager.default.fileExists(atPath: url.path) {
            snapshot = QuotaLedgerSnapshot()
        } else {
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
        // 重启前已进额度冷却时，恢复记录仍须能在翻日后发出确认事件。
        if usageLocked(now: now).usagePercent >= 90 {
            quotaHot = true
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

    /// 记录一个带配额头的响应。`keyFingerprint` 永远是不可逆指纹，不能传原始 key。
    func note(
        _ headers: [AnyHashable: Any],
        at now: Date = Date(),
        keyFingerprint: String? = nil,
        sentAt: Date? = nil,
        monotonicNow: Double = ProcessInfo.processInfo.systemUptime
    ) throws {
        let remaining = Self.remainingRequests(from: headers)
        let resetSeconds = Self.resetRequestsSeconds(from: headers)
        guard remaining != nil || resetSeconds != nil else {
            return
        }
        lock.lock()
        defer { lock.unlock() }
        let ts = now.timeIntervalSince1970
        var changed = false
        // 不同 key 的响应不能延用旧 key 的恢复证明；服务端用量下限故意保留。
        if let oldFP = snapshot.resetKeyFingerprint, oldFP != keyFingerprint {
            clearResetRecordLocked()
            changed = true
        }

        // 必须在换 key 后判定：新 key 的第一条响应没有 sent_at 也能建立自己的恢复记录。
        let stalePostReset: Bool
        if let resetAt = snapshot.resetAt, ts >= resetAt {
            stalePostReset = sentAt == nil || sentAt!.timeIntervalSince1970 < resetAt
        } else {
            stalePostReset = false
        }

        // 翻日后旧请求（或来源未知的请求）返回的头一律不能影响新周期。
        if stalePostReset {
            changed = refreshRolloverLocked(now: now, monotonicNow: monotonicNow) || changed
            if changed { try persistLocked() }
            return
        }

        // x-ratelimit-remaining-requests 是每日剩余（上限 requestsPerDay），不是每分钟。
        if let remaining, Self.isValidRemaining(remaining, limit: limits.requestsPerDay) {
            let usedDay = max(0, limits.requestsPerDay - remaining)
            snapshot.requestCorrections.append(QuotaRequestCorrection(ts: ts, usedRequests: usedDay))
            changed = true
        }
        // 只有同一响应同时拥有合法的两个头和 key 指纹，才能生成或替换恢复记录。
        if let remaining,
           Self.isValidRemaining(remaining, limit: limits.requestsPerDay),
           let resetSeconds,
           resetSeconds.isFinite, resetSeconds >= 0, resetSeconds <= Self.maximumResetSeconds,
           let keyFingerprint, !keyFingerprint.isEmpty {
            // 旧记录已过恢复点但还没被路由事件消费时，先推进锚点再替换，
            // 否则上个周期的本地请求会重新计入 24h 请求数。
            if rolloverConfirmedLocked(now: now, monotonicNow: monotonicNow), let oldResetAt = snapshot.resetAt {
                snapshot.dayEpoch = max(snapshot.dayEpoch ?? -.infinity, oldResetAt)
            }
            snapshot.resetAt = ts + resetSeconds
            snapshot.observedAt = ts
            snapshot.resetKeyFingerprint = keyFingerprint
            snapshot.resetConsumed = false
            observedResetMonotonic = monotonicNow
            resetConfirmationPending = false
            changed = true
        }
        snapshot = Self.pruned(snapshot, now: now)
        changed = refreshRolloverLocked(now: now, monotonicNow: monotonicNow) || changed
        if changed { try persistLocked() }
    }

    /// 兼容 #87 的调用名；新调用方应使用 `note` 传入可信上下文。
    func correctRemainingRequests(
        _ headers: [AnyHashable: Any],
        at now: Date = Date(),
        keyFingerprint: String? = nil,
        sentAt: Date? = nil,
        monotonicNow: Double = ProcessInfo.processInfo.systemUptime
    ) throws {
        try note(headers, at: now, keyFingerprint: keyFingerprint, sentAt: sentAt, monotonicNow: monotonicNow)
    }

    func usage(at now: Date = Date(), monotonicNow: Double = ProcessInfo.processInfo.systemUptime) -> QuotaUsage {
        lock.withLockValue {
            _ = refreshRolloverLocked(now: now, monotonicNow: monotonicNow)
            let confirmedResetAt = rolloverConfirmedLocked(now: now, monotonicNow: monotonicNow)
                ? snapshot.resetAt
                : nil
            return usageLocked(now: now, confirmedResetAt: confirmedResetAt)
        }
    }

    func applyQuotaEvent(
        to context: inout CloudRouteContext,
        at now: Date = Date(),
        monotonicNow: Double = ProcessInfo.processInfo.systemUptime
    ) {
        lock.withLockVoid {
            _ = refreshRolloverLocked(now: now, monotonicNow: monotonicNow, consume: true)
            let percent = usageLocked(now: now).usagePercent
            if resetConfirmationPending {
                resetConfirmationPending = false
                context.apply(RouteEvent(type: "quota_reset_confirmed", status: nil, retryAfter: nil, usagePercent: percent))
            } else {
                context.apply(RouteEvent(type: "quota_update", status: nil, retryAfter: nil, usagePercent: percent))
                if context.cloudState == .cooldownQuota {
                    quotaHot = true
                }
            }
        }
    }

    private func usageLocked(now: Date, confirmedResetAt: Double? = nil) -> QuotaUsage {
        let current = Self.pruned(snapshot, now: now)
        let ts = now.timeIntervalSince1970
        let minuteEvents = current.events.filter { ts - $0.ts < 60 }
        let hourEvents = current.events.filter { ts - $0.ts < 3_600 }
        // 墙钟回拨到已消费锚点之前时，不能让未来锚点排除当前 24 小时窗口的数据。
        let persistedEpoch = current.dayEpoch.flatMap { $0 <= ts ? $0 : nil } ?? -.infinity
        let effectiveEpoch = max(persistedEpoch, confirmedResetAt ?? -.infinity)
        // 恢复点只影响日请求数；日音频窗口仍统计 24h 内全部事件。
        let dayRequestEvents = current.events.filter { $0.ts > effectiveEpoch }
        let dayEvents = current.events
        // 1 分钟窗口只信本地计数。响应头仅校正 24 小时请求数（校正记录已按 24h prune）。
        let minuteRequests = minuteEvents.count
        let correctedDay = Self.foldedDayRequests(
            current.requestCorrections.filter { $0.ts > effectiveEpoch }
        )
        let hourAudio = hourEvents.reduce(0.0) { $0 + $1.billedS }
        let dayRequests = max(dayRequestEvents.count, correctedDay)
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
        let ts = now.timeIntervalSince1970
        let cutoff = ts - 86_400
        // 保留未来锚点，供墙钟追上后继续生效；usageLocked 会在此前将它视为 -∞。
        let retainedDayEpoch: Double?
        if let dayEpoch = snapshot.dayEpoch, dayEpoch > ts {
            retainedDayEpoch = dayEpoch
        } else {
            retainedDayEpoch = snapshot.dayEpoch.flatMap { $0 < cutoff ? nil : $0 }
        }
        return QuotaLedgerSnapshot(
            events: snapshot.events.filter { $0.ts >= cutoff && isBillable($0) },
            requestCorrections: snapshot.requestCorrections.filter { $0.ts >= cutoff },
            dayEpoch: retainedDayEpoch,
            resetAt: snapshot.resetAt,
            observedAt: snapshot.observedAt,
            resetKeyFingerprint: snapshot.resetKeyFingerprint,
            resetConsumed: snapshot.resetConsumed
        )
    }

    /// 规格 002：按时序折叠服务端样本；减少仅在已采纳样本满 60 秒后生效。
    private static func foldedDayRequests(_ corrections: [QuotaRequestCorrection]) -> Int {
        var accepted: QuotaRequestCorrection?
        for correction in corrections.sorted(by: { $0.ts < $1.ts }) {
            guard let current = accepted else {
                accepted = correction
                continue
            }
            if correction.usedRequests > current.usedRequests ||
                (correction.usedRequests < current.usedRequests && correction.ts - current.ts >= serverDecreaseGraceSeconds) {
                accepted = correction
            }
        }
        return accepted?.usedRequests ?? 0
    }

    private static let serverDecreaseGraceSeconds = 60.0
    private static let maximumResetSeconds = 86_460.0

    private static func isValidRemaining(_ value: Int, limit: Int) -> Bool {
        value >= 0 && value <= limit
    }

    private static func isBillable(_ event: QuotaLedgerEvent) -> Bool {
        event.billedS.isFinite && event.billedS >= 0
    }

    private static func saturatedSnapshot(now: Date, requestLimit: Int) -> QuotaLedgerSnapshot {
        QuotaLedgerSnapshot(
            events: [],
            requestCorrections: [
                QuotaRequestCorrection(ts: now.timeIntervalSince1970, usedRequests: requestLimit),
            ],
            dayEpoch: nil
        )
    }

    /// 只有路由事件调用可消费恢复记录，避免状态查询消费记录却不发确认事件。
    @discardableResult
    private func refreshRolloverLocked(now: Date, monotonicNow: Double, consume: Bool = false) -> Bool {
        snapshot = Self.pruned(snapshot, now: now)
        guard let resetAt = snapshot.resetAt,
              let observedAt = snapshot.observedAt,
              let consumed = snapshot.resetConsumed else {
            return false
        }
        let wallNow = now.timeIntervalSince1970
        // 墙钟显著回拨时，不能继续信任从旧墙钟推导出的恢复点。
        if wallNow < observedAt - 5 {
            clearResetRecordLocked()
            try? persistLocked()
            return true
        }
        guard rolloverConfirmedLocked(now: now, monotonicNow: monotonicNow) else { return false }
        guard consume, !consumed else { return false }
        snapshot.resetConsumed = true
        snapshot.dayEpoch = max(snapshot.dayEpoch ?? -.infinity, resetAt)
        if quotaHot { resetConfirmationPending = true }
        try? persistLocked()
        return true
    }

    /// R4：墙钟和（若本进程有）单调钟均满足，才认定恢复点已到；重启后只看墙钟。
    private func rolloverConfirmedLocked(now: Date, monotonicNow: Double) -> Bool {
        guard let resetAt = snapshot.resetAt,
              let observedAt = snapshot.observedAt,
              snapshot.resetKeyFingerprint != nil,
              snapshot.resetConsumed != nil else {
            return false
        }
        let wallNow = now.timeIntervalSince1970
        guard wallNow >= resetAt, wallNow >= observedAt - 5 else { return false }
        if let observedResetMonotonic,
           monotonicNow - observedResetMonotonic < resetAt - observedAt - 1 {
            return false
        }
        return true
    }

    private func clearResetRecordLocked() {
        snapshot.resetAt = nil
        snapshot.observedAt = nil
        snapshot.resetKeyFingerprint = nil
        snapshot.resetConsumed = nil
        observedResetMonotonic = nil
        resetConfirmationPending = false
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

    static func resetRequestsSeconds(from headers: [AnyHashable: Any]) -> Double? {
        for (key, value) in headers where String(describing: key).lowercased() == "x-ratelimit-reset-requests" {
            return parseResetRequestsSeconds(String(describing: value))
        }
        return nil
    }

    /// 只接受完整、无空格的连续单位串，例如 `2m59.56s` 或 `120ms`。
    static func parseResetRequestsSeconds(_ raw: String?) -> Double? {
        let text = raw?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        guard !text.isEmpty else { return nil }
        let regex = try! NSRegularExpression(pattern: "(\\d+(?:\\.\\d+)?)(ms|h|m|s)")
        var index = text.startIndex
        var seconds = 0.0
        while index < text.endIndex {
            let range = NSRange(index..<text.endIndex, in: text)
            guard let match = regex.firstMatch(in: text, options: [.anchored], range: range),
                  let numberRange = Range(match.range(at: 1), in: text),
                  let unitRange = Range(match.range(at: 2), in: text),
                  let value = Double(text[numberRange]), value.isFinite else {
                return nil
            }
            let multiplier: Double
            switch text[unitRange] {
            case "h": multiplier = 3_600
            case "m": multiplier = 60
            case "s": multiplier = 1
            case "ms": multiplier = 0.001
            default: return nil
            }
            seconds += value * multiplier
            guard seconds.isFinite, let next = Range(match.range, in: text)?.upperBound else { return nil }
            index = next
        }
        return seconds
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

            // R1–R10：可信恢复记录必须同时绑定两个头、key 指纹、发送时刻和单调钟。
            let at1000 = Date(timeIntervalSince1970: 1_000)
            let keyA = "fp-a"
            let keyB = "fp-b"
            let both: [AnyHashable: Any] = [
                "x-ratelimit-remaining-requests": "5",
                "x-ratelimit-reset-requests": "100s",
            ]
            func resetRecord(in url: URL) -> [String: Any]? {
                (try? JSONSerialization.jsonObject(with: Data(contentsOf: url))) as? [String: Any]
            }

            // 1：两个头才建记录；超长 reset 也不能建记录。
            let r1URL = dir.appendingPathComponent("quota-r1.json")
            let r1 = QuotaLedger(url: r1URL, now: at1000)
            try r1.note(["x-ratelimit-reset-requests": "100s"], at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            try r1.note(["x-ratelimit-remaining-requests": "5"], at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            check(resetRecord(in: r1URL)?["reset_at"] == nil, "只有一个头不能建立恢复记录")
            try r1.note(["x-ratelimit-remaining-requests": "5", "x-ratelimit-reset-requests": "25h"], at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            check(resetRecord(in: r1URL)?["reset_at"] == nil, "超过 24h+60s 的 reset 应忽略")
            try r1.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            check(resetRecord(in: r1URL)?["reset_at"] as? Double == 1_100, "带两个头的响应应建立恢复记录")

            // 2、3：恢复前的较大 remaining 不是翻日证据；确认翻日后只发一次确认事件。
            let unlockURL = dir.appendingPathComponent("quota-unlock.json")
            let unlock = QuotaLedger(url: unlockURL, now: at1000)
            try unlock.recordRequest(duration: 10, at: at1000)
            try unlock.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            var resetContext = CloudRouteContext(RouteInitial(hasKey: true, cloudEnabled: true, cloudState: .available, nodeEnabled: false, nodeHealthy: false, nodeIsSelf: false, consecutiveFailures: 0, backoffIndex: 0))
            unlock.applyQuotaEvent(to: &resetContext, at: at1000, monotonicNow: 100)
            check(resetContext.cloudState == .cooldownQuota, "恢复前应进入额度冷却")
            try unlock.note(["x-ratelimit-remaining-requests": "1999"], at: at1000.addingTimeInterval(10), keyFingerprint: keyA, sentAt: at1000.addingTimeInterval(5), monotonicNow: 105)
            check(unlock.usage(at: at1000.addingTimeInterval(10), monotonicNow: 105).dayRequests >= 1_995, "恢复点前更大 remaining 不得触发翻日")
            let afterReset = at1000.addingTimeInterval(101)
            unlock.applyQuotaEvent(to: &resetContext, at: afterReset, monotonicNow: 200)
            let unlockedUsage = unlock.usage(at: afterReset, monotonicNow: 200)
            check(resetContext.cloudState == .available && resetContext.action == "resume_cloud", "可信翻日应以 quota_reset_confirmed 通过滞回解锁")
            check(unlockedUsage.dayRequests == 0 && unlockedUsage.dayAudioSeconds == 10, "翻日只能清 RPD，音频窗口必须保留")
            let expectedPercent = max(
                Double(unlockedUsage.minuteRequests) / 20,
                unlockedUsage.hourAudioSeconds / 7_200,
                Double(unlockedUsage.dayRequests) / 2_000,
                unlockedUsage.dayAudioSeconds / 28_800
            ) * 100
            check(abs(unlockedUsage.usagePercent - expectedPercent) < 1e-9, "确认事件的 usage_percent 应是四窗口最大值")
            unlock.applyQuotaEvent(to: &resetContext, at: afterReset, monotonicNow: 200)
            check(resetContext.action == "none", "同一恢复记录只能确认一次")

            // 翻日后、尚未消费前就来了新周期响应：替换记录不能让旧周期请求重新计入。
            let replaceURL = dir.appendingPathComponent("quota-replace.json")
            let replace = QuotaLedger(url: replaceURL, now: at1000)
            for i in 0..<1_900 {
                try replace.recordRequest(duration: 10, at: at1000.addingTimeInterval(Double(i) * 0.01))
            }
            try replace.note(both, at: at1000.addingTimeInterval(20), keyFingerprint: keyA, sentAt: at1000.addingTimeInterval(20), monotonicNow: 120)
            let nextCycle = at1000.addingTimeInterval(130)
            try replace.recordRequest(duration: 10, at: nextCycle)
            try replace.note(["x-ratelimit-remaining-requests": "1999", "x-ratelimit-reset-requests": "86000s"], at: nextCycle, keyFingerprint: keyA, sentAt: nextCycle, monotonicNow: 230)
            check(replace.usage(at: nextCycle, monotonicNow: 230).dayRequests < 10, "替换已翻日记录后旧周期请求不得重新计入")

            // 4、5：墙钟回拨和单调钟不足都 fail-closed。
            let rollback = QuotaLedger(url: dir.appendingPathComponent("quota-rollback.json"), now: at1000)
            try rollback.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            let rollbackUsage = rollback.usage(at: at1000.addingTimeInterval(-6), monotonicNow: 94)
            check(rollbackUsage.dayRequests == 1_995 && resetRecord(in: dir.appendingPathComponent("quota-rollback.json"))?["reset_at"] == nil, "墙钟回拨应丢记录但保留服务端下限")
            let jump = QuotaLedger(url: dir.appendingPathComponent("quota-jump.json"), now: at1000)
            try jump.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            check(jump.usage(at: afterReset, monotonicNow: 101).dayRequests == 1_995, "墙钟前跳但单调钟不足不能翻日")

            // 6、7：换 key 仅清恢复证明；翻日后的迟到/未知发送时刻响应不采信。
            let bindingURL = dir.appendingPathComponent("quota-binding.json")
            let binding = QuotaLedger(url: bindingURL, now: at1000)
            try binding.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            try binding.note(["x-ratelimit-remaining-requests": "1999"], at: at1000.addingTimeInterval(1), keyFingerprint: keyB, sentAt: at1000, monotonicNow: 101)
            check(binding.usage(at: at1000.addingTimeInterval(1), monotonicNow: 101).dayRequests == 1_995 && resetRecord(in: bindingURL)?["reset_at"] == nil, "换 key 应清恢复记录但保留服务端下限")
            try binding.note(both, at: at1000.addingTimeInterval(2), keyFingerprint: nil, sentAt: at1000.addingTimeInterval(2), monotonicNow: 102)
            check(resetRecord(in: bindingURL)?["reset_at"] == nil, "key_fp=nil 不能建恢复记录")

            // F2：旧 key 的恢复点已过时，新 key 的双头响应即使没有 sent_at，也必须建立新记录。
            let rotatedAfterResetURL = dir.appendingPathComponent("quota-rotated-after-reset.json")
            let rotatedAfterReset = QuotaLedger(url: rotatedAfterResetURL, now: at1000)
            try rotatedAfterReset.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            try rotatedAfterReset.note(both, at: afterReset, keyFingerprint: keyB, sentAt: nil, monotonicNow: 200)
            check(
                resetRecord(in: rotatedAfterResetURL)?["reset_at"] as? Double == 1_201 &&
                    resetRecord(in: rotatedAfterResetURL)?["key_fp"] as? String == keyB,
                "换 key 必须先于迟到旧周期判定，以新响应建立恢复记录"
            )

            let late = QuotaLedger(url: dir.appendingPathComponent("quota-late.json"), now: at1000)
            try late.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            _ = late.usage(at: afterReset, monotonicNow: 200)
            try late.note(["x-ratelimit-remaining-requests": "1999", "x-ratelimit-reset-requests": "100s"], at: afterReset, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 200)
            try late.note(["x-ratelimit-remaining-requests": "1999", "x-ratelimit-reset-requests": "100s"], at: afterReset, keyFingerprint: keyA, sentAt: nil, monotonicNow: 200)
            check(late.usage(at: afterReset, monotonicNow: 200).dayRequests == 0 && resetRecord(in: dir.appendingPathComponent("quota-late.json"))?["reset_at"] as? Double == 1_100, "迟到或未知发送时刻的头不得改写新周期")

            // 8：其它三个窗口不因 RPD 翻日而重置。
            let windows = QuotaLedger(url: dir.appendingPathComponent("quota-windows.json"), now: at1000)
            try windows.recordRequest(duration: 10, at: at1000)
            try windows.note(["x-ratelimit-remaining-requests": "5", "x-ratelimit-reset-requests": "10s"], at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            let beforeWindows = windows.usage(at: at1000.addingTimeInterval(9), monotonicNow: 108)
            let afterWindows = windows.usage(at: at1000.addingTimeInterval(11), monotonicNow: 111)
            check(beforeWindows.minuteRequests == afterWindows.minuteRequests && beforeWindows.hourAudioSeconds == afterWindows.hourAudioSeconds && beforeWindows.dayAudioSeconds == afterWindows.dayAudioSeconds, "翻日前后三个非 RPD 窗口必须不变")

            // 9、10：落盘往返、旧格式/坏恢复字段和重启后的墙钟确认。
            let persistedURL = dir.appendingPathComponent("quota-reset-persisted.json")
            let persisted = QuotaLedger(url: persistedURL, now: at1000)
            try persisted.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            check(resetRecord(in: persistedURL)?["key_fp"] as? String == keyA && resetRecord(in: persistedURL)?["consumed"] as? Bool == false, "恢复记录应原子落盘")
            let resetRestarted = QuotaLedger(url: persistedURL, now: afterReset)
            check(resetRestarted.usage(at: afterReset).dayRequests == 0, "重启后无单调钟时墙钟满足即可翻日")
            check(resetRecord(in: persistedURL)?["consumed"] as? Bool == false, "状态查询不能消费恢复记录")
            var persistedContext = CloudRouteContext(RouteInitial(hasKey: true, cloudEnabled: true, cloudState: .cooldownQuota, nodeEnabled: false, nodeHealthy: false, nodeIsSelf: false, consecutiveFailures: 0, backoffIndex: 0))
            resetRestarted.applyQuotaEvent(to: &persistedContext, at: afterReset)
            check(resetRecord(in: persistedURL)?["consumed"] as? Bool == true && persistedContext.action == "resume_cloud", "确认事件应和 consumed 一起落盘防重复")

            // F3：重启前已进额度冷却；重启后状态查询不能消费记录，首次路由调用必须原子地确认翻日。
            let restartConfirmURL = dir.appendingPathComponent("quota-restart-confirm.json")
            let beforeRestart = QuotaLedger(url: restartConfirmURL, now: at1000)
            try beforeRestart.note(both, at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100)
            let restartedHot = QuotaLedger(url: restartConfirmURL, now: at1000.addingTimeInterval(50))
            _ = restartedHot.usage(at: afterReset, monotonicNow: 200)
            check(resetRecord(in: restartConfirmURL)?["consumed"] as? Bool == false, "状态查询不得在未发确认事件时消费恢复记录")
            var restartContext = CloudRouteContext(RouteInitial(hasKey: true, cloudEnabled: true, cloudState: .cooldownQuota, nodeEnabled: false, nodeHealthy: false, nodeIsSelf: false, consecutiveFailures: 0, backoffIndex: 0))
            restartedHot.applyQuotaEvent(to: &restartContext, at: afterReset, monotonicNow: 200)
            check(restartContext.cloudState == .available && restartContext.action == "resume_cloud" && resetRecord(in: restartConfirmURL)?["consumed"] as? Bool == true, "重启后的首次路由调用必须消费记录并发 quota_reset_confirmed")

            // G2：重启时墙钟错误前跳会消费恢复点；随后回拨前锚点必须失效，追上后再恢复生效。
            let futureEpochURL = dir.appendingPathComponent("quota-future-day-epoch.json")
            let beforeJump = QuotaLedger(url: futureEpochURL, now: at1000)
            try beforeJump.recordRequest(duration: 10, at: at1000)
            try beforeJump.note(
                ["x-ratelimit-remaining-requests": "0", "x-ratelimit-reset-requests": "100s"],
                at: at1000, keyFingerprint: keyA, sentAt: at1000, monotonicNow: 100
            )
            let jumpedAfterRestart = QuotaLedger(url: futureEpochURL, now: afterReset)
            var jumpedContext = CloudRouteContext(RouteInitial(
                hasKey: true, cloudEnabled: true, cloudState: .cooldownQuota,
                nodeEnabled: false, nodeHealthy: false, nodeIsSelf: false,
                consecutiveFailures: 0, backoffIndex: 0
            ))
            jumpedAfterRestart.applyQuotaEvent(to: &jumpedContext, at: afterReset)
            check(resetRecord(in: futureEpochURL)?["day_epoch"] as? Double == 1_100, "错误前跳后应持久化已消费的 dayEpoch")
            let rolledBack = QuotaLedger(url: futureEpochURL, now: at1000.addingTimeInterval(50))
            check(rolledBack.usage(at: at1000.addingTimeInterval(50)).dayRequests == 2_000, "未来 dayEpoch 在墙钟回拨期间必须忽略，保留本地事件和 remaining=0 校正")
            check(rolledBack.usage(at: afterReset).dayRequests == 0, "墙钟追上 future dayEpoch 后锚点应重新生效")

            let oldFormatURL = dir.appendingPathComponent("quota-old-format.json")
            try Data(#"{"events":[{"ts":1000,"billed_s":10}],"request_corrections":[{"ts":1000,"used_requests":3}]}"#.utf8).write(to: oldFormatURL)
            check(QuotaLedger(url: oldFormatURL, now: at1000).usage(at: at1000).dayRequests == 3, "旧格式账本应照常读取")
            let malformedURL = dir.appendingPathComponent("quota-malformed-reset.json")
            try Data(#"{"events":[],"request_corrections":[],"reset_at":"bad","observed_at":1000,"key_fp":"fp","consumed":false}"#.utf8).write(to: malformedURL)
            _ = QuotaLedger(url: malformedURL, now: at1000)
            check(FileManager.default.fileExists(atPath: malformedURL.path) && !FileManager.default.fileExists(atPath: malformedURL.appendingPathExtension("corrupt").path), "恢复字段非法只丢记录，不隔离整份账本")

        } catch {
            failures.append("QuotaLedger selftest 异常：\(error)")
        }

        return failures
    }
}
