@preconcurrency import AVFoundation
import Darwin
import Foundation

struct NodeStateRecord: Codable, Equatable, Sendable {
    var info: NodeInfo?
    var rtf: Double?
    var offlineUntil: Double?

    enum CodingKeys: String, CodingKey {
        case info
        case rtf
        case offlineUntil = "offline_until"
    }
}

struct RouteWeights: Sendable {
    var asrRank: [String: Double] = [
        "whisper-large-v3-turbo": 40,
        "whisper-large-v3": 38,
        "fake": 30,
    ]
    var translatorRank: [String: Double] = [
        "apple": 20,
        "fake": 18,
    ]
    var speedK = 20.0
    var inUsePenalty = 12.0
    var batteryPenalty = 10.0
    var rttPenalty = 6.0
    var localUnderP8Penalty = 1_000.0
    var noNetworkBonus = 4.0
    var migrationMargin = 8.0

    static let `default` = RouteWeights()
}

struct RouteCandidate: Sendable {
    var id: String
    var expectedNodeID: String
    var info: NodeInfo?
    var rttMS: Double
    var isLocal: Bool
    var offlineUntil: Double?
}

struct RouteScore: Sendable {
    var id: String
    var score: Double
    var reason: String
}

enum RoutingDecider {
    static func score(
        _ candidate: RouteCandidate,
        now: Double,
        localAccurate: Bool,
        weights: RouteWeights = .default
    ) -> RouteScore? {
        if let offlineUntil = candidate.offlineUntil, offlineUntil > now {
            return nil
        }
        guard let info = candidate.info, info.nodeID == candidate.expectedNodeID, !info.busy else {
            return nil
        }
        let model = info.asr.model ?? ""
        let translator = info.translator
        let asr = weights.asrRank[model] ?? 25
        let tr = weights.translatorRank[translator] ?? (translator.hasPrefix("ollama:") ? 16 : 12)
        let rtf = info.asr.rtf ?? candidate.rttMS / 1000.0
        var penalties = 0.0
        if info.inUse { penalties += weights.inUsePenalty }
        if !info.onAC { penalties += weights.batteryPenalty }
        if candidate.rttMS > 100 { penalties += weights.rttPenalty }
        if candidate.isLocal && !localAccurate { penalties += weights.localUnderP8Penalty }
        let bonus = candidate.isLocal ? weights.noNetworkBonus : 0
        let score = asr + tr - weights.speedK * rtf - penalties + bonus
        let reason = String(
            format: "quality=%.1f speed=%.1f penalties=%.1f bonus=%.1f rtt_ms=%.0f",
            asr + tr, -weights.speedK * rtf, penalties, bonus, candidate.rttMS
        )
        return RouteScore(id: candidate.id, score: score, reason: reason)
    }

    static func choose(
        candidates: [RouteCandidate],
        now: Double,
        localAccurate: Bool,
        weights: RouteWeights = .default
    ) -> RouteScore? {
        candidates.compactMap { score($0, now: now, localAccurate: localAccurate, weights: weights) }
            .max { $0.score < $1.score }
    }
}

struct MigrationGate {
    var currentID: String?
    var pendingID: String?
    var consecutiveWins = 0
    var weights: RouteWeights = .default

    mutating func observe(best: RouteScore?, current: RouteScore?) -> Bool {
        guard let best, let current, best.id != current.id,
              best.score >= current.score + weights.migrationMargin else {
            pendingID = nil
            consecutiveWins = 0
            return false
        }
        if pendingID == best.id {
            consecutiveWins += 1
        } else {
            pendingID = best.id
            consecutiveWins = 1
        }
        return consecutiveWins >= 2
    }
}

@available(macOS 27.0, *)
final class NodeRouter: @unchecked Sendable {
    private static let probeInterval: Duration = .seconds(30)
    private static let offlineDuration = 60.0
    private static let silenceThreshold = 0.003
    private static let silenceSecondsForMigration = 0.6

    private let sourceLocaleID: String
    private let targetLanguageID: String
    private let callbacks: NodeCallbacks
    private let onMode: @Sendable (SubtitleMode) -> Void
    private let nodesURL: URL
    private let stateURL: URL
    private let weights = RouteWeights.default
    private let localAccurate: Bool
    private let lock = NSLock()

    private var nodes: [NodeConfig] = []
    private var states: [String: NodeStateRecord] = [:]
    private var currentClient: NodeClient?
    private var currentNode: NodeConfig?
    private var currentToken: String?
    private var currentInfo: NodeInfo?
    private var audioTask: Task<Void, Never>?
    private var probeTask: Task<Void, Never>?
    private var gate = MigrationGate()
    private var pendingMigrationID: String?
    private var silenceRun = 0.0
    private var latestClock = 0.0
    private var stopped = false
    private var switching = false

    init(
        sourceLocaleID: String,
        targetLanguageID: String,
        callbacks: NodeCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void,
        nodesURL: URL = NodeRouter.defaultNodesURL,
        stateURL: URL = NodeRouter.defaultStateURL,
        localAccurate: Bool = Capability.supportsAccurateLocal()
    ) {
        self.sourceLocaleID = sourceLocaleID
        self.targetLanguageID = targetLanguageID
        self.callbacks = callbacks
        self.onMode = onMode
        self.nodesURL = nodesURL
        self.stateURL = stateURL
        self.localAccurate = localAccurate
    }

    // macOS 的 expandingTildeInPath 不认 $HOME，冒烟/测试没法靠改 HOME 隔离；
    // 不给这个入口，测试只能去挪用户真实的 nodes.json（10-09 就被覆盖过一次）。
    static var configDir: String {
        ProcessInfo.processInfo.environment["RSLITE_CONFIG_DIR"] ?? "~/.config/rslite".expandingTilde
    }

    static var defaultNodesURL: URL {
        URL(fileURLWithPath: configDir).appendingPathComponent("nodes.json")
    }

    static var defaultStateURL: URL {
        URL(fileURLWithPath: configDir).appendingPathComponent("node-state.json")
    }

    func start(fanout: AudioFanout) {
        lock.withLockVoid {
            stopped = false
            nodes = Self.loadNodes(from: nodesURL)
            states = Self.loadState(from: stateURL)
        }
        guard !nodes.isEmpty else {
            routeLog("select local reason=no_nodes")
            onMode(.local)
            return
        }
        let stream = fanout.subscribe(capacity: AudioFanout.defaultCapacity)
        probeTask = Task { await self.probeLoop() }
        audioTask = Task { await self.audioLoop(stream: stream, fanout: fanout) }
    }

    func stop() async {
        lock.withLockVoid { stopped = true }
        probeTask?.cancel()
        audioTask?.cancel()
        let client = lock.withLockValue { currentClient }
        await client?.close()
        lock.withLockVoid {
            currentClient = nil
            currentNode = nil
            currentToken = nil
            currentInfo = nil
            pendingMigrationID = nil
        }
    }

    /// 音频源结束后收尾：等音频循环退出，对当前节点 drain（最多 5s）把尾部句子收回来再关闭。
    func finish() async {
        await audioTask?.value
        let client = lock.withLockValue { stopped ? nil : currentClient }
        probeTask?.cancel()
        await client?.drain(timeout: .seconds(5))
        await stop()
    }

    private func probeLoop() async {
        await probeOnce()
        while !Task.isCancelled {
            do {
                try await Task.sleep(for: Self.probeInterval)
            } catch {
                return
            }
            await probeOnce()
        }
    }

    private func probeOnce() async {
        let configs = lock.withLockValue { nodes }
        var candidates: [RouteCandidate] = []
        for node in configs {
            let t0 = ContinuousClock().now
            do {
                let (token, probed) = try await NodeClient.prepare(config: node)
                let rtt = max(1, t0.duration(to: ContinuousClock().now).secondsValue * 1000)
                // 节点同一时刻只服务一个会话：我们自己正占着的节点，/v1/info 必然报 busy=true。
                // 这不是「被别人占用」，按未占用评分，否则 30s 探测会把正在用的节点判成不可用。
                var info = probed
                if lock.withLockValue({ currentNode?.id == node.id }) {
                    info.busy = false
                }
                lock.withLockVoid {
                    states[node.id] = NodeStateRecord(info: info, rtf: info.asr.rtf, offlineUntil: nil)
                    if currentNode?.id == node.id {
                        currentToken = token
                        currentInfo = info
                    }
                }
                candidates.append(RouteCandidate(
                    id: node.id, expectedNodeID: node.nodeID, info: info, rttMS: rtt, isLocal: false, offlineUntil: nil
                ))
            } catch NodeClientError.nodeIDMismatch {
                routeLog("probe node=\(node.id) rejected=node_id_mismatch")
                lock.withLockVoid {
                    states[node.id] = NodeStateRecord(info: nil, rtf: nil, offlineUntil: nowSeconds() + Self.offlineDuration)
                }
            } catch {
                routeLog("probe node=\(node.id) offline err=\(routeErrorCode(error))")
                let old = lock.withLockValue { states[node.id] }
                candidates.append(RouteCandidate(
                    id: node.id,
                    expectedNodeID: node.nodeID,
                    info: old?.info,
                    rttMS: 999,
                    isLocal: false,
                    offlineUntil: old?.offlineUntil
                ))
            }
        }
        persistState()
        await select(candidates: candidates)
    }

    private func select(candidates: [RouteCandidate]) async {
        let now = nowSeconds()
        let best = RoutingDecider.choose(
            candidates: candidates.map { candidate in
                var c = candidate
                c.offlineUntil = lock.withLockValue { states[candidate.id]?.offlineUntil }
                return c
            },
            now: now,
            localAccurate: localAccurate,
            weights: weights
        )
        let currentScore: RouteScore? = lock.withLockValue {
            guard let node = currentNode, let info = currentInfo else { return nil }
            return RoutingDecider.score(
                RouteCandidate(
                    id: node.id,
                    expectedNodeID: node.nodeID,
                    info: info,
                    rttMS: 1,
                    isLocal: false,
                    offlineUntil: states[node.id]?.offlineUntil
                ),
                now: now,
                localAccurate: localAccurate,
                weights: weights
            )
        }
        if let best {
            routeLog("select node=\(best.id) score=\(String(format: "%.1f", best.score)) \(best.reason)")
        } else {
            routeLog("select local reason=no_available_node")
        }
        // 切换一律由 audioLoop 在下一块音频到达时执行（首次连接立刻，迁移等静音点）：
        // 这里若也直接切，会和 audioLoop 同时对同一节点开两个会话，后者吃 1013。
        lock.withLockVoid {
            if currentNode == nil {
                pendingMigrationID = best?.id
                return
            }
            var gateCopy = gate
            let ok = gateCopy.observe(best: best, current: currentScore)
            gate = gateCopy
            if ok, let best, best.id != currentNode?.id {
                pendingMigrationID = best.id
            }
        }
    }

    private func audioLoop(stream: AsyncStream<TimedAudio>, fanout: AudioFanout) async {
        for await audio in stream {
            if Task.isCancelled { return }
            lock.withLockVoid { latestClock = audio.startSeconds }
            updateSilence(audio)
            if lock.withLockValue({ pendingMigrationID != nil && (currentNode == nil || silenceRun >= Self.silenceSecondsForMigration) }) {
                await switchToPending(atSilence: currentNode != nil)
            }
            let client = lock.withLockValue { currentClient }
            do {
                try await client?.send(audio)
            } catch {
                await markCurrentOfflineAndFallback(error: error)
            }
        }
    }

    private func switchToPending(atSilence: Bool) async {
        let target = lock.withLockValue { () -> NodeConfig? in
            guard !switching, !stopped, let id = pendingMigrationID else { return nil }
            switching = true
            return nodes.first { $0.id == id }
        }
        guard let node = target else {
            return
        }
        defer { lock.withLockVoid { switching = false } }
        do {
            let (token, info) = try await NodeClient.prepare(config: node)
            let client = NodeClient(
                config: node,
                token: token,
                info: info,
                sourceLocaleID: sourceLocaleID,
                targetLanguageID: targetLanguageID,
                offsetSeconds: lock.withLockValue { latestClock },
                callbacks: callbacks
            )
            try await client.connect()
            let old = lock.withLockValue { currentClient }
            lock.withLockVoid {
                currentClient = client
                currentNode = node
                currentToken = token
                currentInfo = info
                pendingMigrationID = nil
                silenceRun = 0
            }
            onMode(.hybrid(node.id))
            if atSilence {
                Task {
                    await old?.drain(timeout: .seconds(3))
                    await old?.close()
                }
            } else {
                await old?.close()
            }
        } catch {
            routeLog("select local reason=switch_failed node=\(node.id) err=\(routeErrorCode(error))")
            lock.withLockVoid {
                states[node.id] = NodeStateRecord(info: nil, rtf: nil, offlineUntil: nowSeconds() + Self.offlineDuration)
                pendingMigrationID = nil
            }
            persistState()
            if lock.withLockValue({ currentNode == nil }) {
                onMode(.local)
            }
        }
    }

    private func markCurrentOfflineAndFallback(error: Error) async {
        let failed = lock.withLockValue { currentNode }
        if let failed {
            routeLog("fallback failed_node=\(failed.id) offline_for_s=60 err=\(routeErrorCode(error))")
            lock.withLockVoid {
                var state = states[failed.id] ?? NodeStateRecord(info: nil, rtf: nil, offlineUntil: nil)
                state.offlineUntil = nowSeconds() + Self.offlineDuration
                states[failed.id] = state
                currentClient = nil
                currentNode = nil
                currentToken = nil
                currentInfo = nil
            }
            persistState()
        }
        onMode(.local)
        await probeOnce()
    }

    private func updateSilence(_ audio: TimedAudio) {
        let seconds = audio.duration
        guard let channel = audio.buffer.floatChannelData?[0] else {
            lock.withLockVoid { silenceRun = 0 }
            return
        }
        let count = Int(audio.buffer.frameLength)
        guard count > 0 else { return }
        var sum = 0.0
        for i in 0..<count {
            let v = Double(channel[i])
            sum += v * v
        }
        let rms = sqrt(sum / Double(count))
        lock.withLockVoid {
            silenceRun = rms < Self.silenceThreshold ? silenceRun + seconds : 0
        }
    }

    private func persistState() {
        let snapshot = lock.withLockValue { states }
        do {
            try Self.atomicWriteJSON(snapshot, to: stateURL)
        } catch {
            routeLog("state_write_failed err=\(type(of: error))")
        }
    }

    static func loadNodes(from url: URL = defaultNodesURL) -> [NodeConfig] {
        guard let data = try? Data(contentsOf: url),
              let decoded = try? JSONDecoder().decode([NodeConfig].self, from: data) else {
            return []
        }
        var seen = Set<String>()
        return decoded.filter { node in
            guard !seen.contains(node.id) else { return false }
            seen.insert(node.id)
            return true
        }
    }

    static func loadState(from url: URL = defaultStateURL) -> [String: NodeStateRecord] {
        guard let data = try? Data(contentsOf: url),
              let decoded = try? JSONDecoder().decode([String: NodeStateRecord].self, from: data) else {
            return [:]
        }
        return decoded
    }

    static func atomicWriteJSON<T: Encodable>(_ value: T, to url: URL) throws {
        let data = try JSONEncoder().encode(value)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let tmp = url.deletingLastPathComponent()
            .appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        FileManager.default.createFile(atPath: tmp.path, contents: data, attributes: [.posixPermissions: 0o600])
        chmod(tmp.path, 0o600)
        if FileManager.default.fileExists(atPath: url.path) {
            try FileManager.default.removeItem(at: url)
        }
        try FileManager.default.moveItem(at: tmp, to: url)
        chmod(url.path, 0o600)
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        let goodInfo = NodeInfo(
            v: 2, nodeID: "n1", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.08),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm"
        )
        let betterInfo = NodeInfo(
            v: 2, nodeID: "n2", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.02),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm"
        )
        let busyInfo = NodeInfo(
            v: 2, nodeID: "n3", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.01),
            translator: "apple", busy: true, onAC: true, inUse: false, worker: "warm"
        )
        let batteryInfo = NodeInfo(
            v: 2, nodeID: "n4", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.01),
            translator: "apple", busy: false, onAC: false, inUse: false, worker: "warm"
        )
        let base = RouteCandidate(id: "base", expectedNodeID: "n1", info: goodInfo, rttMS: 20, isLocal: false, offlineUntil: nil)
        let better = RouteCandidate(id: "better", expectedNodeID: "n2", info: betterInfo, rttMS: 20, isLocal: false, offlineUntil: nil)
        check(RoutingDecider.choose(candidates: [base, better], now: 0, localAccurate: true)?.id == "better",
              "更优节点上线应被选中")
        check(RoutingDecider.choose(candidates: [better, RouteCandidate(id: "off", expectedNodeID: "n1", info: goodInfo, rttMS: 10, isLocal: false, offlineUntil: 100)], now: 1, localAccurate: true)?.id == "better",
              "offline_until 内节点应排除")
        check(RoutingDecider.choose(candidates: [RouteCandidate(id: "busy", expectedNodeID: "n3", info: busyInfo, rttMS: 10, isLocal: false, offlineUntil: nil)], now: 0, localAccurate: true) == nil,
              "busy 节点应排除")
        check(RoutingDecider.choose(candidates: [RouteCandidate(id: "mismatch", expectedNodeID: "wrong", info: goodInfo, rttMS: 10, isLocal: false, offlineUntil: nil)], now: 0, localAccurate: true) == nil,
              "node_id 不匹配应排除")
        // 节点下线：所有节点 offline_until 在未来，应返回 nil
        check(RoutingDecider.choose(candidates: [
            RouteCandidate(id: "off1", expectedNodeID: "n1", info: goodInfo, rttMS: 10, isLocal: false, offlineUntil: 100),
            RouteCandidate(id: "off2", expectedNodeID: "n2", info: betterInfo, rttMS: 10, isLocal: false, offlineUntil: 200),
        ], now: 1, localAccurate: true) == nil,
              "所有节点下线时应返回 nil")
        check(RoutingDecider.score(RouteCandidate(id: "battery", expectedNodeID: "n4", info: batteryInfo, rttMS: 10, isLocal: false, offlineUntil: nil), now: 0, localAccurate: true)!.score
              < RoutingDecider.score(base, now: 0, localAccurate: true)!.score,
              "电池供电应扣分")
        var gate = MigrationGate()
        let c = RouteScore(id: "base", score: 50, reason: "")
        let b = RouteScore(id: "better", score: 70, reason: "")
        check(!gate.observe(best: b, current: c), "第一次领先不应迁移")
        check(gate.observe(best: b, current: c), "连续两次领先应迁移")
        return failures
    }
}

private func routeLog(_ message: String) {
    FileHandle.standardError.write(Data("rslite.route \(message)\n".utf8))
}

private func routeErrorCode(_ error: Error) -> String {
    if let error = error as? NodeClientError {
        return error.reasonCode
    }
    return String(describing: type(of: error))
}

private func nowSeconds() -> Double {
    Date().timeIntervalSince1970
}

private extension Duration {
    var secondsValue: Double {
        Double(components.seconds) + Double(components.attoseconds) / 1e18
    }
}

private extension String {
    var expandingTilde: String {
        (self as NSString).expandingTildeInPath
    }
}
