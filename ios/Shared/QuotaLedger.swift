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
    /// 最近一条带有效服务端恢复头的响应所给出的恢复时刻。
    var serverResetAt: Double?
    /// 已消费的恢复时刻；避免旧周期的本地事件在锚点被清除后重新计入日用量。
    var dayEpoch: Double?

    enum CodingKeys: String, CodingKey {
        case events
        case requestCorrections = "request_corrections"
        case serverResetAt = "server_reset_at"
        case dayEpoch = "day_epoch"
    }

    init(
        events: [QuotaLedgerEvent] = [],
        requestCorrections: [QuotaRequestCorrection] = [],
        serverResetAt: Double? = nil,
        dayEpoch: Double? = nil
    ) {
        self.events = events
        self.requestCorrections = requestCorrections
        self.serverResetAt = serverResetAt
        self.dayEpoch = dayEpoch
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        // 两个列表仍是必填：缺键说明文件坏了，要走隔离 + 饱和，不能读成空账本。
        events = try container.decode([QuotaLedgerEvent].self, forKey: .events)
        requestCorrections = try container.decode([QuotaRequestCorrection].self, forKey: .requestCorrections)
        serverResetAt = try container.decodeIfPresent(Double.self, forKey: .serverResetAt)
        dayEpoch = try container.decodeIfPresent(Double.self, forKey: .dayEpoch)
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(events, forKey: .events)
        try container.encode(requestCorrections, forKey: .requestCorrections)
        try container.encodeIfPresent(serverResetAt, forKey: .serverResetAt)
        try container.encodeIfPresent(dayEpoch, forKey: .dayEpoch)
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
    private let lock = NSLock()
    private var snapshot: QuotaLedgerSnapshot

    init(url: URL, limits: QuotaLedgerLimits = QuotaLedgerLimits(), now: Date = Date()) {
        self.url = url
        self.limits = limits
        // 文件不存在是空账本；文件在但读不到或解不开，才隔离并饱和 24 小时。
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
            // 隔离失败（比如只读目录）也不能让账本停在内存里：能写就写，写不了就只保留饱和状态。
            if quarantined {
                try? persistLocked()
            }
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

    func correctRemainingRequests(_ headers: [AnyHashable: Any], at now: Date = Date()) throws {
        let remaining = Self.remainingRequests(from: headers)
        let resetSeconds = Self.resetRequestsSeconds(from: headers)
        guard remaining != nil || resetSeconds != nil else {
            return
        }
        lock.lock()
        defer { lock.unlock() }
        let ts = now.timeIntervalSince1970
        if let resetSeconds {
            if let oldReset = snapshot.serverResetAt, oldReset <= ts {
                snapshot.dayEpoch = max(snapshot.dayEpoch ?? -.infinity, oldReset)
            }
            // 每个带有效恢复头的响应都以自身时刻重新锚定，最新值覆盖旧值。
            snapshot.serverResetAt = ts + resetSeconds
        } else if remaining != nil, let oldReset = snapshot.serverResetAt, ts >= oldReset {
            // 恢复后的首个 remaining 样本是新周期基线；先消费锚点以隔离旧本地事件。
            snapshot.dayEpoch = max(snapshot.dayEpoch ?? -.infinity, oldReset)
            snapshot.serverResetAt = nil
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
        return usageLocked(now: now)
    }

    /// 把 usagePercent 交给路由状态机。90% 进冷却、80% 以下才恢复的滞回在 Routing.swift 里，
    /// 账本只负责给出准确百分比。
    func applyQuotaEvent(to context: inout CloudRouteContext, at now: Date = Date()) {
        let percent = usage(at: now).usagePercent
        context.apply(RouteEvent(type: "quota_update", status: nil, retryAfter: nil, usagePercent: percent))
    }

    private func usageLocked(now: Date) -> QuotaUsage {
        let current = Self.pruned(snapshot, now: now)
        let ts = now.timeIntervalSince1970
        let minuteEvents = current.events.filter { ts - $0.ts < 60 }
        let hourEvents = current.events.filter { ts - $0.ts < 3_600 }
        let effectiveEpoch = Self.effectiveEpoch(in: current, at: ts)
        // 恢复点只影响日请求数；日音频窗口仍统计 24h 内全部事件。
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
            dayEpoch: snapshot.dayEpoch.flatMap { $0 < cutoff ? nil : $0 }
        )
    }

    private static func effectiveEpoch(in snapshot: QuotaLedgerSnapshot, at ts: Double) -> Double {
        var epoch = snapshot.dayEpoch ?? -.infinity
        if let reset = snapshot.serverResetAt, ts >= reset {
            epoch = max(epoch, reset)
        }
        return epoch
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
            ],
            serverResetAt: nil,
            dayEpoch: nil
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
