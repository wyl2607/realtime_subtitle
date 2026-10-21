import Foundation

/// 识别引擎模式。缺省（没写过 / 读到不认识的旧值）一律按 `auto`，
/// 这样升级后的老用户不会再被钉死在“只连节点”上。
enum EngineMode: String, Equatable {
    case auto
    case node
    case local

    static func normalized(_ raw: String?) -> EngineMode {
        guard let raw, let mode = EngineMode(rawValue: raw) else { return .auto }
        return mode
    }
}

/// 与 mac rslite `ProviderTier` 同名同值，四端共享路由语义。
enum ProviderTier: String, Codable {
    case cloud
    case node
    case local
}

/// 与 mac rslite `CloudState` 同名同值。本轮 iOS 不做云端档，
/// 但夹具里的状态机要原样跑通，下一轮接云端时不用再改规则。
enum CloudState: String, Codable {
    case available
    case cooldownBackoff = "cooldown_backoff"
    case disabledInvalidKey = "disabled_invalid_key"
    case cooldownRateLimited = "cooldown_rate_limited"
    case cooldownQuota = "cooldown_quota"
    case probe
    case off
}

/// 路由决策的纯逻辑，从 mac `macos-native/Sources/rslite/Provider.swift` 移植。
/// 不 import AppKit、不碰网络、不读写 Keychain——只有这样才能用同一份夹具
/// 在 macOS 上用 swiftc 跑自测（见 ios/Tests/RoutingSelftest.swift）。
struct CloudRouteContext {
    var hasKey: Bool
    var cloudEnabled: Bool
    var cloudState: CloudState
    var nodeEnabled: Bool
    var nodeHealthy: Bool
    var nodeIsSelf: Bool
    var consecutiveFailures: Int
    var backoffIndex: Int
    var keyInvalid: Bool

    private(set) var cooldownSeconds: Int?
    private(set) var cancelInflight = false
    private(set) var discardResponse = false
    private(set) var action = "none"

    private static let backoffSeconds = [5, 30, 120, 300]

    init(_ initial: RouteInitial) {
        hasKey = initial.hasKey ?? false
        cloudEnabled = initial.cloudEnabled ?? true
        cloudState = initial.cloudState ?? .available
        nodeEnabled = initial.nodeEnabled ?? false
        nodeHealthy = initial.nodeHealthy ?? false
        nodeIsSelf = initial.nodeIsSelf ?? false
        consecutiveFailures = initial.consecutiveFailures ?? 0
        backoffIndex = initial.backoffIndex ?? 0
        keyInvalid = initial.keyInvalid ?? (cloudState == .disabledInvalidKey)
    }

    mutating func apply(_ event: RouteEvent) {
        // Confirming RPD rollover cannot erase an unrelated HTTP 429 retry-after.
        if !(event.type == "quota_reset_confirmed" && cloudState == .cooldownRateLimited) {
            cooldownSeconds = nil
        }
        cancelInflight = false
        discardResponse = false
        action = "none"

        switch event.type {
        case "noop":
            return
        case "segment_success":
            consecutiveFailures = 0
            if cloudState == .probe {
                cloudState = .available
                backoffIndex = 0
                action = "resume_cloud"
            }
        case "segment_failure":
            failSegment()
        case "http_status":
            httpStatus(event.status ?? 0, retryAfter: event.retryAfter)
        case "quota_update":
            quotaUpdate(event.usagePercent ?? 0)
        case "quota_reset_confirmed":
            // The ledger verifies rollover first; here only quota cooldown may lift.
            // Missing usage remains fail-closed (100%), and 401/429/off win.
            if cloudEnabled && hasKey && !keyInvalid && cloudState == .cooldownQuota {
                quotaUpdate(event.usagePercent ?? 100)
            }
        case "cooldown_expired":
            if cloudState == .cooldownBackoff || cloudState == .cooldownRateLimited || cloudState == .cooldownQuota {
                cloudState = .probe
                action = "probe_once"
            }
        case "switch_off":
            cloudEnabled = false
            turnCloudOff()
        case "switch_on":
            cloudEnabled = true
            if hasKey {
                if keyInvalid {
                    cloudState = .disabledInvalidKey
                } else {
                    resumeCloud()
                }
            }
        case "key_cleared":
            hasKey = false
            turnCloudOff()
        case "key_modified":
            keyInvalid = false
            hasKey = true
            cloudEnabled = true
            resumeCloud()
        default:
            action = "unknown_event"
        }
    }

    func tier() -> ProviderTier {
        if hasKey && cloudEnabled && (cloudState == .available || cloudState == .probe) {
            return .cloud
        }
        if nodeEnabled && nodeHealthy && !nodeIsSelf {
            return .node
        }
        return .local
    }

    func snapshot(keeping expected: [String: FixtureValue]) -> [String: FixtureValue] {
        let full: [String: FixtureValue] = [
            "tier": .string(tier().rawValue),
            "cloud_state": .string(cloudState.rawValue),
            "consecutive_failures": .number(Double(consecutiveFailures)),
            "cooldown_seconds": cooldownSeconds.map { .number(Double($0)) } ?? .null,
            "backoff_index": .number(Double(backoffIndex)),
            "cancel_inflight": .bool(cancelInflight),
            "discard_response": .bool(discardResponse),
            "action": .string(action),
            "key_invalid": .bool(keyInvalid),
        ]
        return expected.reduce(into: [:]) { result, item in
            result[item.key] = full[item.key] ?? .null
        }
    }

    private mutating func failSegment() {
        if cloudState == .probe {
            // 试探失败说明当前退避不足，下一轮按更长档位再试。
            backoffIndex = min(backoffIndex + 1, Self.backoffSeconds.count - 1)
            cloudState = .cooldownBackoff
            cooldownSeconds = Self.backoffSeconds[backoffIndex]
            consecutiveFailures = max(consecutiveFailures, 1)
            action = "fallback"
            return
        }
        guard cloudState == .available else {
            action = "fallback"
            return
        }
        consecutiveFailures += 1
        guard consecutiveFailures >= 3 else {
            action = "keep_local"
            return
        }
        cloudState = .cooldownBackoff
        cooldownSeconds = Self.backoffSeconds[backoffIndex]
        action = "fallback"
    }

    private mutating func httpStatus(_ status: Int, retryAfter: Int?) {
        if status == 401 || status == 403 {
            keyInvalid = true
            cloudState = .disabledInvalidKey
            action = "mark_key_invalid"
            return
        }
        if status == 429 {
            if keyInvalid || cloudState == .disabledInvalidKey {
                // 失效 key 只有 key_modified 能解除。401 之后迟到的 429 是并发在飞请求的
                // 回执，若照常进限流冷却，冷却到期会 probe→tier() 回到 cloud，
                // 把已经失效的 key 又带回云端发请求，所以这里整条忽略。
                return
            }
            cloudState = .cooldownRateLimited
            cooldownSeconds = retryAfter ?? 60
            action = "rate_limited"
            return
        }
        if status >= 500 && status <= 599 {
            failSegment()
        }
    }

    private mutating func quotaUpdate(_ usagePercent: Double) {
        guard cloudEnabled && hasKey && cloudState != .disabledInvalidKey && cloudState != .off else {
            return
        }
        if usagePercent >= 90 {
            cloudState = .cooldownQuota
            action = "quota_cooldown"
        } else if cloudState == .cooldownQuota && usagePercent < 80 {
            resumeCloud()
        } else if cloudState == .cooldownQuota {
            action = "quota_cooldown"
        }
    }

    private mutating func turnCloudOff() {
        // 开关关闭或 key 清除后，已发请求无法撤回，但返回值必须被本地丢弃。
        cloudState = .off
        cancelInflight = true
        discardResponse = true
        action = "cloud_off"
    }

    private mutating func resumeCloud() {
        cloudState = .available
        consecutiveFailures = 0
        backoffIndex = 0
        action = "resume_cloud"
    }
}

// MARK: - 夹具类型（与 mac Provider.swift 逐字段一致）

enum FixtureValue: Codable, Equatable, CustomStringConvertible {
    case string(String)
    case number(Double)
    case bool(Bool)
    case null

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
        } else if let value = try? container.decode(Bool.self) {
            self = .bool(value)
        } else if let value = try? container.decode(Double.self) {
            self = .number(value)
        } else {
            self = .string(try container.decode(String.self))
        }
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .string(let value):
            try container.encode(value)
        case .number(let value):
            try container.encode(value)
        case .bool(let value):
            try container.encode(value)
        case .null:
            try container.encodeNil()
        }
    }

    var description: String {
        switch self {
        case .string(let value):
            return value
        case .number(let value):
            return String(value)
        case .bool(let value):
            return String(value)
        case .null:
            return "null"
        }
    }
}

struct RouteFixture: Decodable {
    var version: Int
    var routeCases: [RouteCase]

    enum CodingKeys: String, CodingKey {
        case version
        case routeCases = "route_cases"
    }
}

struct RouteCase: Decodable {
    var name: String
    var initial: RouteInitial
    var steps: [RouteStep]
}

struct RouteInitial: Decodable {
    var hasKey: Bool?
    var cloudEnabled: Bool?
    var cloudState: CloudState?
    var nodeEnabled: Bool?
    var nodeHealthy: Bool?
    var nodeIsSelf: Bool?
    var consecutiveFailures: Int?
    var backoffIndex: Int?
    var keyInvalid: Bool?

    init(
        hasKey: Bool? = nil,
        cloudEnabled: Bool? = nil,
        cloudState: CloudState? = nil,
        nodeEnabled: Bool? = nil,
        nodeHealthy: Bool? = nil,
        nodeIsSelf: Bool? = nil,
        consecutiveFailures: Int? = nil,
        backoffIndex: Int? = nil,
        keyInvalid: Bool? = nil
    ) {
        self.hasKey = hasKey
        self.cloudEnabled = cloudEnabled
        self.cloudState = cloudState
        self.nodeEnabled = nodeEnabled
        self.nodeHealthy = nodeHealthy
        self.nodeIsSelf = nodeIsSelf
        self.consecutiveFailures = consecutiveFailures
        self.backoffIndex = backoffIndex
        self.keyInvalid = keyInvalid
    }

    enum CodingKeys: String, CodingKey {
        case hasKey = "has_key"
        case cloudEnabled = "cloud_enabled"
        case cloudState = "cloud_state"
        case nodeEnabled = "node_enabled"
        case nodeHealthy = "node_healthy"
        case nodeIsSelf = "node_is_self"
        case consecutiveFailures = "consecutive_failures"
        case backoffIndex = "backoff_index"
        case keyInvalid = "key_invalid"
    }
}

struct RouteStep: Decodable {
    var event: RouteEvent
    var expect: [String: FixtureValue]
}

struct RouteEvent: Decodable {
    var type: String
    var status: Int?
    var retryAfter: Int?
    var usagePercent: Double?

    enum CodingKeys: String, CodingKey {
        case type
        case status
        case retryAfter = "retry_after"
        case usagePercent = "usage_percent"
    }
}
