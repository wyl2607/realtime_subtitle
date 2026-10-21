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
        let dayEvents = current.events
        // 1 分钟窗口只信本地计数，响应头校正只抬高 24 小时请求数（校正记录已按 24h prune）。
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
        try Self.atomicWrite(data, to: url, mode: 0o600)
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
