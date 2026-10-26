import Darwin
import Foundation

/// 限额常量对齐 rslite 的 QuotaLedgerLimits：Groq 免费档按分钟/小时/每日三维记账。
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
    /// 可信恢复记录：只有同一条响应同时带合法的 remaining 与 reset 头才写入。
    /// 四个字段一起构成记录；缺任何一个都按「没有恢复记录」处理（旧文件也是这样）。
    var serverResetAt: Double?
    var serverResetObservedAt: Double?
    var serverResetKeyFp: String?
    var serverResetConsumed: Bool
    /// 解码时发现恢复记录字段非法并丢弃；只用于初始化后把干净版本写回去，不落盘。
    var droppedInvalidRecovery = false

    enum CodingKeys: String, CodingKey {
        case events
        case requestCorrections = "request_corrections"
        case serverResetAt = "server_reset_at"
        case serverResetObservedAt = "server_reset_observed_at"
        case serverResetKeyFp = "server_reset_key_fp"
        case serverResetConsumed = "server_reset_consumed"
    }

    init(
        events: [QuotaLedgerEvent] = [],
        requestCorrections: [QuotaRequestCorrection] = [],
        serverResetAt: Double? = nil,
        serverResetObservedAt: Double? = nil,
        serverResetKeyFp: String? = nil,
        serverResetConsumed: Bool = false
    ) {
        self.events = events
        self.requestCorrections = requestCorrections
        self.serverResetAt = serverResetAt
        self.serverResetObservedAt = serverResetObservedAt
        self.serverResetKeyFp = serverResetKeyFp
        self.serverResetConsumed = serverResetConsumed
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // 两个列表仍是必填：缺键说明文件坏了，要走隔离 + 饱和，不能读成空账本。
        events = try container.decode([QuotaLedgerEvent].self, forKey: .events)
        requestCorrections = try container.decode([QuotaRequestCorrection].self, forKey: .requestCorrections)
        // 恢复记录字段类型不对/非有限数只丢弃记录，不隔离整份账本（R7）。
        let (resetAt, badReset) = Self.decodeRecoveryField(Double.self, from: container, forKey: .serverResetAt)
        let (observedAt, badObserved) = Self.decodeRecoveryField(Double.self, from: container, forKey: .serverResetObservedAt)
        let (keyFp, badKey) = Self.decodeRecoveryField(String.self, from: container, forKey: .serverResetKeyFp)
        let (consumed, badConsumed) = Self.decodeRecoveryField(Bool.self, from: container, forKey: .serverResetConsumed)
        serverResetAt = resetAt
        serverResetObservedAt = observedAt
        serverResetKeyFp = keyFp
        serverResetConsumed = consumed ?? false
        let hasRecoveryFields = resetAt != nil
            || observedAt != nil
            || keyFp != nil
            || container.contains(.serverResetConsumed)
            || badReset || badObserved || badKey || badConsumed
        if badReset || badObserved || badKey || badConsumed || !Self.isCompleteRecovery(
            resetAt: serverResetAt,
            observedAt: serverResetObservedAt,
            keyFp: serverResetKeyFp
        ) {
            droppedInvalidRecovery = hasRecoveryFields
            serverResetAt = nil
            serverResetObservedAt = nil
            serverResetKeyFp = nil
            serverResetConsumed = false
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(events, forKey: .events)
        try container.encode(requestCorrections, forKey: .requestCorrections)
        try container.encodeIfPresent(serverResetAt, forKey: .serverResetAt)
        try container.encodeIfPresent(serverResetObservedAt, forKey: .serverResetObservedAt)
        try container.encodeIfPresent(serverResetKeyFp, forKey: .serverResetKeyFp)
        if serverResetAt != nil {
            try container.encode(serverResetConsumed, forKey: .serverResetConsumed)
        }
    }

    /// 区分「字段缺失 / 明确为 null」与「类型不对」：后者要把整条恢复记录丢掉（R7）。
    private static func decodeRecoveryField<T: Decodable>(
        _ type: T.Type,
        from container: KeyedDecodingContainer<CodingKeys>,
        forKey key: CodingKeys
    ) -> (T?, Bool) {
        guard container.contains(key) else { return (nil, false) }
        do {
            if try container.decodeNil(forKey: key) { return (nil, false) }
            return (try container.decode(T.self, forKey: key), false)
        } catch {
            return (nil, true)
        }
    }

    static func isCompleteRecovery(resetAt: Double?, observedAt: Double?, keyFp: String?) -> Bool {
        guard let resetAt, let observedAt, let keyFp,
              resetAt.isFinite, observedAt.isFinite, !keyFp.isEmpty else {
            return false
        }
        let duration = resetAt - observedAt
        return duration >= 0 && duration <= 86_400 + 60
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
        // 非法数字（null / 非数字 / NaN 文本被换成 null）先记成 NaN，解码后由 pruned 丢弃。
        if let value = try? container.decode(Double.self, forKey: .billedS) {
            billedS = value
        } else {
            billedS = .nan
        }
        // dur_s 是可选：旧账本没有它；NaN/Infinity 被换成 null 后也按缺失处理。
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

/// 本地额度账本。路径由调用方传入（广播扩展和 app 各自决定放哪），本类不读 Keychain、不碰网络。
/// 记账规则只认 rslite 那一份：每请求按 max(10, duration) 计费，分钟窗口不吸收响应头校正。
final class QuotaLedger: @unchecked Sendable {
    private let url: URL
    private let limits: QuotaLedgerLimits
    private let monotonicNow: () -> Double
    private let lock = NSLock()
    private var snapshot: QuotaLedgerSnapshot
    /// 恢复记录的单调钟观测点。只活在进程内，绝不落盘（R7）。
    private var recoveryObservedMono: Double?
    /// 本账本此前把额度打进过冷却。跟 rslite 的 `_quota_hot` 同义，但不落盘。
    private var quotaHot = false

    private static let enterPercent = 90.0
    private static let recoverPercent = 80.0
    private static let maxResetSeconds = 86_400.0 + 60

    init(
        url: URL,
        limits: QuotaLedgerLimits = QuotaLedgerLimits(),
        now: Date = Date(),
        monotonicNow: @escaping () -> Double = { ProcessInfo.processInfo.systemUptime }
    ) {
        self.url = url
        self.limits = limits
        self.monotonicNow = monotonicNow
        // 文件不存在是空账本；文件在但读不到或解不开，才隔离并饱和 24 小时。
        guard FileManager.default.fileExists(atPath: url.path) else {
            snapshot = QuotaLedgerSnapshot()
            return
        }
        do {
            let data = try Data(contentsOf: url)
            let decoded = try Self.decodeSnapshot(data)
            snapshot = Self.pruned(decoded, now: now)
            if decoded.droppedInvalidRecovery {
                try? persistLocked()
            }
        } catch {
            let quarantined = Self.quarantine(url)
            snapshot = Self.saturatedSnapshot(now: now, requestLimit: limits.requestsPerDay)
            // 隔离失败（比如只读目录）也不能让账本停在内存里：能写就写，写不了就只保留饱和状态。
            if quarantined {
                try? persistLocked()
            }
        }
        // 墙钟回拨超过 5 秒：恢复记录不可信，丢弃但保留下限（R4a）。
        if discardRecoveryOnClockRollbackLocked(at: now.timeIntervalSince1970) {
            try? persistLocked()
        }
        // 与 rslite 的 `_publish(load 时)` 对齐：载入时已经 ≥90% 就算「此前进过冷却」，
        // 重启后仍能靠翻日记录解锁。
        if usageLocked(now: now).usagePercent >= Self.enterPercent {
            quotaHot = true
        }
    }

    @discardableResult
    func recordRequest(duration: Double, at now: Date = Date()) throws -> Double {
        lock.lock()
        defer { lock.unlock() }
        let billed = max(10, duration)
        snapshot.events.append(
            QuotaLedgerEvent(ts: now.timeIntervalSince1970, billedS: billed, durS: Self.round3(duration))
        )
        snapshot = Self.pruned(snapshot, now: now)
        try persistLocked()
        return billed
    }

    /// 记账接口：把响应头里的服务端事实并进账本。
    ///
    /// - Parameters:
    ///   - keyFingerprint: 这次请求实际使用的 key 的指纹（哈希，绝不是 key 本身）；未知传 nil。
    ///   - sentAt: 请求发出时的墙钟时刻；未知传 nil。用于识别上个周期在飞的迟到响应（R3）。
    ///
    /// 只有同一条响应同时带合法的 remaining 与 reset 头才建立/替换恢复记录（R1）。
    /// 只带其一时 remaining 仍按 #87 规则校正 24h 请求窗口，但恢复记录原样不动。
    func correctRemainingRequests(
        _ headers: [AnyHashable: Any],
        at now: Date = Date(),
        keyFingerprint: String? = nil,
        sentAt: Date? = nil
    ) throws {
        let remaining = Self.remainingRequests(from: headers)
        let rawReset = Self.resetRequestsSeconds(from: headers)
        // reset 时长超过 24h+60s 视为非法（R1）。
        let resetSeconds = rawReset.flatMap { value -> Double? in
            guard value.isFinite, value >= 0, value <= Self.maxResetSeconds else { return nil }
            return value
        }
        lock.lock()
        defer { lock.unlock() }
        let ts = now.timeIntervalSince1970
        var changed = discardRecoveryOnClockRollbackLocked(at: ts)
        // 换 key：旧记录作废，但服务端用量的下限留在 requestCorrections 里（R2）。
        if let existingFp = snapshot.serverResetKeyFp, existingFp != keyFingerprint {
            discardRecoveryLocked()
            changed = true
        }
        guard remaining != nil || resetSeconds != nil else {
            if changed {
                snapshot = Self.pruned(snapshot, now: now)
                try persistLocked()
            }
            return
        }
        // R3：恢复点已经过去，这时回来的旧周期响应（sent_at 未知或早于恢复点）
        // 一律不采信它的 remaining/reset 头，免得把新周期的账改回去。
        if let resetAt = snapshot.serverResetAt, ts >= resetAt,
           sentAt.map({ $0.timeIntervalSince1970 < resetAt }) ?? true {
            if changed {
                snapshot = Self.pruned(snapshot, now: now)
                try persistLocked()
            }
            return
        }
        // R1 + R2：两个头都合法且知道 key 指纹时，才建立/替换恢复记录。
        if remaining != nil, let resetSeconds, let keyFingerprint, !keyFingerprint.isEmpty {
            snapshot.serverResetAt = ts + resetSeconds
            snapshot.serverResetObservedAt = ts
            snapshot.serverResetKeyFp = keyFingerprint
            snapshot.serverResetConsumed = false
            recoveryObservedMono = monotonicNow()
        }
        // x-ratelimit-remaining-requests 是每日剩余（上限 requestsPerDay），不是每分钟。
        if let remaining {
            let usedDay = max(0, limits.requestsPerDay - remaining)
            snapshot.requestCorrections.append(QuotaRequestCorrection(ts: ts, usedRequests: usedDay))
        }
        snapshot = Self.pruned(snapshot, now: now)
        try persistLocked()
    }

    func usage(at now: Date = Date()) -> QuotaUsage {
        lock.lock()
        defer { lock.unlock() }
        if discardRecoveryOnClockRollbackLocked(at: now.timeIntervalSince1970) {
            try? persistLocked()
        }
        let usage = usageLocked(now: now)
        // 只升不降：真正的降级要等一次 quota_update 发出去（routeEvent），
        // 否则一次只读的 usage() 会把「此前已打进冷却」抹掉，错过翻日事件。
        if usage.usagePercent >= Self.enterPercent {
            quotaHot = true
        }
        return usage
    }

    /// 把所有窗口里最高的百分比交给路由状态机。90% 进冷却、80% 以下才恢复的滞回在
    /// Routing.swift 里，账本只负责给出准确百分比。
    ///
    /// 若这条恢复记录刚刚被判定为可信翻日（R4），且本账本此前已经把额度打进冷却，
    /// 这一次发 `quota_reset_confirmed` 代替 `quota_update`（R6）；每条记录只发一次，
    /// consumed 已落盘。是否放行仍由路由状态机决定。
    func applyQuotaEvent(to context: inout CloudRouteContext, at now: Date = Date()) {
        let event = routeEvent(at: now)
        context.apply(event)
    }

    /// 拉取一次路由事件：正常是 `quota_update`，可信翻日成立的那一次是
    /// `quota_reset_confirmed`（usage_percent 为四个窗口的最大值）。
    func routeEvent(at now: Date = Date()) -> RouteEvent {
        lock.lock()
        defer { lock.unlock() }
        let ts = now.timeIntervalSince1970
        var changed = discardRecoveryOnClockRollbackLocked(at: ts)
        let usage = usageLocked(now: now)
        if quotaHot, !snapshot.serverResetConsumed, rolloverConfirmedLocked(at: ts) {
            snapshot.serverResetConsumed = true
            changed = true
            updateQuotaHotLocked(usage)
            try? persistLocked()
            return RouteEvent(
                type: "quota_reset_confirmed",
                status: nil,
                retryAfter: nil,
                usagePercent: usage.usagePercent
            )
        }
        updateQuotaHotLocked(usage)
        if changed {
            try? persistLocked()
        }
        return RouteEvent(
            type: "quota_update",
            status: nil,
            retryAfter: nil,
            usagePercent: usage.usagePercent
        )
    }

    /// 冷却期间轮询用：只有「此前进过额度冷却 + 可信翻日刚成立 + 尚未消费」时才返回
    /// 四窗口最大百分比并消费这条恢复记录；其余情况一律返回 nil，不产生任何事件。
    ///
    /// 不在这里更新 quotaHot：额度是自然老化到 80% 以下时仍要靠这条记录解锁一次。
    func takeConfirmedReset(at now: Date = Date()) -> Double? {
        lock.lock()
        defer { lock.unlock() }
        let ts = now.timeIntervalSince1970
        if discardRecoveryOnClockRollbackLocked(at: ts) {
            try? persistLocked()
        }
        let usage = usageLocked(now: now)
        guard quotaHot,
              !snapshot.serverResetConsumed,
              rolloverConfirmedLocked(at: ts) else {
            return nil
        }
        snapshot.serverResetConsumed = true
        updateQuotaHotLocked(usage)
        try? persistLocked()
        return usage.usagePercent
    }

    /// 只读查询：当前恢复记录是否已按 R4 构成可信翻日。
    func rolloverConfirmed(at now: Date = Date()) -> Bool {
        lock.lock()
        defer { lock.unlock() }
        return rolloverConfirmedLocked(at: now.timeIntervalSince1970)
    }

    private func usageLocked(now: Date) -> QuotaUsage {
        let current = Self.pruned(snapshot, now: now)
        let ts = now.timeIntervalSince1970
        let minuteEvents = current.events.filter { ts - $0.ts < 60 }
        let hourEvents = current.events.filter { ts - $0.ts < 3_600 }
        let effectiveEpoch = effectiveEpochLocked(at: ts)
        // 恢复点只影响日请求数；日音频窗口仍统计 24h 内全部事件（R5）。
        let dayRequestEvents = current.events.filter { $0.ts > effectiveEpoch }
        let dayEvents = current.events
        // 1 分钟窗口只信本地计数，响应头仅校正 24 小时请求数（校正记录已按 24h prune）。
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

    private func updateQuotaHotLocked(_ usage: QuotaUsage) {
        if usage.usagePercent >= Self.enterPercent {
            quotaHot = true
        } else if usage.usagePercent < Self.recoverPercent {
            quotaHot = false
        }
    }

    /// R4：墙钟 (a) 与单调钟 (b) 都满足才算翻日成立。重启后没有 observed_mono，只看 (a)。
    private func rolloverConfirmedLocked(at ts: Double) -> Bool {
        guard let resetAt = snapshot.serverResetAt,
              let observedAt = snapshot.serverResetObservedAt,
              let keyFp = snapshot.serverResetKeyFp,
              QuotaLedgerSnapshot.isCompleteRecovery(
                resetAt: resetAt,
                observedAt: observedAt,
                keyFp: keyFp
              ) else {
            return false
        }
        guard ts >= resetAt, ts >= observedAt - 5 else { return false }
        if let observedMono = recoveryObservedMono {
            let elapsed = monotonicNow() - observedMono
            if elapsed < resetAt - observedAt - 1 {
                return false
            }
        }
        return true
    }

    private func effectiveEpochLocked(at ts: Double) -> Double {
        rolloverConfirmedLocked(at: ts) ? (snapshot.serverResetAt ?? -.infinity) : -.infinity
    }

    @discardableResult
    private func discardRecoveryOnClockRollbackLocked(at ts: Double) -> Bool {
        guard let observedAt = snapshot.serverResetObservedAt, ts < observedAt - 5 else {
            return false
        }
        discardRecoveryLocked()
        return true
    }

    private func discardRecoveryLocked() {
        snapshot.serverResetAt = nil
        snapshot.serverResetObservedAt = nil
        snapshot.serverResetKeyFp = nil
        snapshot.serverResetConsumed = false
        recoveryObservedMono = nil
    }

    private func persistLocked() throws {
        let data = try JSONEncoder().encode(snapshot)
        try Self.atomicWrite(data, to: url, mode: 0o600)
    }

    private static func pruned(_ snapshot: QuotaLedgerSnapshot, now: Date) -> QuotaLedgerSnapshot {
        let cutoff = now.timeIntervalSince1970 - 86_400
        return QuotaLedgerSnapshot(
            events: snapshot.events.filter { $0.ts >= cutoff && isBillable($0) },
            requestCorrections: snapshot.requestCorrections.filter { $0.ts >= cutoff },
            serverResetAt: snapshot.serverResetAt,
            serverResetObservedAt: snapshot.serverResetObservedAt,
            serverResetKeyFp: snapshot.serverResetKeyFp,
            serverResetConsumed: snapshot.serverResetConsumed
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

    /// JSON 不允许 NaN / Infinity。账本里出现这些记号时换成 null，让同文件里的其它事件还能解码；
    /// 整个文件因此不会被判成损坏而饱和 24 小时。
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

    /// 同目录临时文件写满、fsync、0600、rename：账本写一半时进程被杀也不会留下可解析的残账。
    /// 与 rslite 的 CloudConfigStore.atomicWrite 保持同一套落盘语义。
    private static func atomicWrite(_ data: Data, to url: URL, mode: mode_t) throws {
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let tmp = url.deletingLastPathComponent()
            .appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_EXCL, mode)
        guard fd >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var renamed = false
        defer {
            if !renamed {
                unlink(tmp.path)
            }
        }
        do {
            try data.withUnsafeBytes { raw in
                guard let base = raw.baseAddress else { return }
                var offset = 0
                while offset < raw.count {
                    let written = write(fd, base.advanced(by: offset), raw.count - offset)
                    if written < 0 {
                        if errno == EINTR { continue }
                        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                    }
                    if written == 0 {
                        throw POSIXError(.EIO)
                    }
                    offset += written
                }
            }
            if fchmod(fd, mode) != 0 {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if fsync(fd) != 0 {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
        } catch {
            close(fd)
            throw error
        }
        close(fd)
        guard rename(tmp.path, url.path) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        renamed = true
    }
}
