import Foundation

enum ProviderTier: String, Codable {
    case cloud
    case node
    case local
}

protocol Provider {
    var tier: ProviderTier { get }
    func available() -> Bool
}

enum CloudState: String, Codable {
    case available
    case cooldownBackoff = "cooldown_backoff"
    case disabledInvalidKey = "disabled_invalid_key"
    case cooldownRateLimited = "cooldown_rate_limited"
    case cooldownQuota = "cooldown_quota"
    case probe
    case off
}

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
            if cloudState == .cooldownBackoff || cloudState == .cooldownRateLimited {
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

enum P9FixtureSelfTest {
    static func selfTest() -> [String] {
        var failures: [String] = []
        failures.append(contentsOf: routeSelfTest().map { "route_cases: \($0)" })
        failures.append(contentsOf: p5SelfTest().map { "p5_cases: \($0)" })
        return failures
    }

    private static func routeSelfTest() -> [String] {
        do {
            let fixture: RouteFixture = try loadFixture("route_cases.json")
            return fixture.routeCases.flatMap { routeFailures($0) }
        } catch {
            return ["读取失败 \(error)"]
        }
    }

    private static func routeFailures(_ testCase: RouteCase) -> [String] {
        var state = CloudRouteContext(testCase.initial)
        var failures: [String] = []
        for (index, step) in testCase.steps.enumerated() {
            state.apply(step.event)
            let actual = state.snapshot(keeping: step.expect)
            if actual != step.expect {
                failures.append("\(testCase.name)#\(index + 1) expected \(step.expect) got \(actual)")
            }
        }
        return failures
    }

    private static func p5SelfTest() -> [String] {
        do {
            let fixture: P5Fixture = try loadFixture("p5_cases.json")
            return fixture.p5Cases.flatMap { p5Failures($0) }
        } catch {
            return ["读取失败 \(error)"]
        }
    }

    private static func p5Failures(_ testCase: P5Case) -> [String] {
        var store = LineStore()
        var failures: [String] = []
        for operation in testCase.operations {
            switch operation.type {
            case "add_local":
                store.addLocal(key: operation.key ?? 0, t0: operation.t0, t1: operation.t1, text: operation.src ?? "")
            case "apply_remote":
                let replaced = store.applyNode(
                    nodeID: operation.nodeID ?? "cloud:groq",
                    t0: operation.t0,
                    t1: operation.t1,
                    srcText: operation.src ?? "",
                    dstText: operation.dst
                )
                if let expected = operation.expectReplaced, replaced != expected {
                    failures.append("\(testCase.name) replaced expected \(expected) got \(replaced)")
                }
            default:
                failures.append("\(testCase.name) unknown operation \(operation.type)")
            }
        }
        if store.lines.count != testCase.expect.lines.count {
            failures.append("\(testCase.name) lines count expected \(testCase.expect.lines.count) got \(store.lines.count)")
        }
        let actualLines = filtered(store.lines, keeping: testCase.expect.lines)
        if actualLines != testCase.expect.lines {
            failures.append("\(testCase.name) lines expected \(testCase.expect.lines) got \(actualLines)")
        }
        let visibleLimit = max(testCase.visibleLimit ?? store.lines.count, 0)
        let visible = Array(store.lines.suffix(visibleLimit))
        if visible.count != testCase.expect.visible.count {
            failures.append("\(testCase.name) visible count expected \(testCase.expect.visible.count) got \(visible.count)")
        }
        let actualVisible = filtered(visible, keeping: testCase.expect.visible)
        if actualVisible != testCase.expect.visible {
            failures.append("\(testCase.name) visible expected \(testCase.expect.visible) got \(actualVisible)")
        }
        return failures
    }

    private static func filtered(
        _ lines: [SubtitleLine],
        keeping expected: [[String: FixtureValue]]
    ) -> [[String: FixtureValue]] {
        zip(lines, expected).map { line, expectedLine in
            let full = serialise(line)
            return expectedLine.reduce(into: [:]) { result, item in
                result[item.key] = full[item.key] ?? .null
            }
        }
    }

    private static func serialise(_ line: SubtitleLine) -> [String: FixtureValue] {
        let source: String
        switch line.source {
        case .local:
            source = "local"
        case .node:
            source = "remote"
        }
        return [
            "source": .string(source),
            "key": .number(Double(line.key)),
            "src": .string(line.srcText),
            "dst": line.dstText.map { .string($0) } ?? .null,
            "t0": line.t0.map { .number($0) } ?? .null,
            "t1": line.t1.map { .number($0) } ?? .null,
        ]
    }

    private static func loadFixture<T: Decodable>(_ name: String) throws -> T {
        let url = try fixtureURL(name)
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(T.self, from: data)
    }

    private static func fixtureURL(_ name: String) throws -> URL {
        let fileURL = URL(fileURLWithPath: #filePath)
        let repoFromSource = fileURL
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .deletingLastPathComponent()
        let current = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let candidates = [
            current.appendingPathComponent("../tests/fixtures/\(name)"),
            current.appendingPathComponent("tests/fixtures/\(name)"),
            repoFromSource.appendingPathComponent("tests/fixtures/\(name)"),
        ]
        if let found = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) {
            return found
        }
        throw FixtureError.missing(name)
    }
}

enum FixtureError: Error, CustomStringConvertible {
    case missing(String)

    var description: String {
        switch self {
        case .missing(let name):
            return "missing fixture \(name)"
        }
    }
}

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

struct P5Fixture: Decodable {
    var version: Int
    var p5Cases: [P5Case]

    enum CodingKeys: String, CodingKey {
        case version
        case p5Cases = "p5_cases"
    }
}

struct P5Case: Decodable {
    var name: String
    var visibleLimit: Int?
    var operations: [P5Operation]
    var expect: P5Expect

    enum CodingKeys: String, CodingKey {
        case name
        case visibleLimit = "visible_limit"
        case operations
        case expect
    }
}

struct P5Operation: Decodable {
    var type: String
    var key: Int?
    var nodeID: String?
    var t0: Double?
    var t1: Double?
    var src: String?
    var dst: String?
    var expectReplaced: [Int]?

    enum CodingKeys: String, CodingKey {
        case type
        case key
        case nodeID = "node_id"
        case t0
        case t1
        case src
        case dst
        case expectReplaced = "expect_replaced"
    }
}

struct P5Expect: Decodable {
    var lines: [[String: FixtureValue]]
    var visible: [[String: FixtureValue]]
}
