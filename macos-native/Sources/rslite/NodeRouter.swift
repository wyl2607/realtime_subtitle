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
    private var currentSession: NodeSessionRef?
    private var failedSessions = Set<NodeSessionRef>()
    private var switchTask: Task<Void, Never>?
    private var drainTasks: [UUID: Task<Void, Never>] = [:]
    private var audioTask: Task<Void, Never>?
    private var probeTask: Task<Void, Never>?
    private var gate = MigrationGate()
    private var pendingMigrationID: String?
    private var silenceRun = 0.0
    private var latestClock = 0.0
    private var stopped = false
    private var switching = false
    private var switchGeneration = 0
    private var nodeSessionGenerations: [String: Int] = [:]

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
        lock.withLockVoid {
            stopped = true
            switching = false
            switchGeneration += 1
        }
        probeTask?.cancel()
        audioTask?.cancel()
        lock.withLockValue { switchTask }?.cancel()
        let client = lock.withLockValue { currentClient }
        await client?.close()
        lock.withLockVoid {
            currentClient = nil
            currentSession = nil
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
        // 迁移中的旧会话也要把尾部句子收回来（各自 drain ≤3s + close ≤2s，有上限）
        let migrating = lock.withLockValue { Array(drainTasks.values) }
        for task in migrating {
            await task.value
        }
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
                // 探测失败：旧 info 已不可信，清掉缓存，不参与打分
                let old = lock.withLockValue { () -> NodeStateRecord? in
                    var record = states[node.id] ?? NodeStateRecord(info: nil, rtf: nil, offlineUntil: nil)
                    record.info = nil
                    record.rtf = nil
                    states[node.id] = record
                    return record
                }
                candidates.append(RouteCandidate(
                    id: node.id,
                    expectedNodeID: node.nodeID,
                    info: nil,
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
            // prepare/connect 最长十几秒：放到独立 Task，音频循环不能被它堵住，
            // 否则 AudioFanout 丢旧帧、节点时钟落后（R3-2）。switching 由 switchToPending 内部预约防重入。
            let trigger = lock.withLockValue { () -> Bool? in
                guard !stopped, !switching, pendingMigrationID != nil else { return nil }
                guard currentNode == nil || silenceRun >= Self.silenceSecondsForMigration else { return nil }
                return currentNode != nil
            }
            if let atSilence = trigger {
                let task = Task { await self.switchToPending(atSilence: atSilence) }
                lock.withLockVoid { switchTask = task }
            }
            let (client, session) = lock.withLockValue { (currentClient, currentSession) }
            do {
                try await client?.send(audio)
            } catch {
                if isCancellationOrStopped(error) {
                    return
                }
                if let session {
                    await markCurrentOfflineAndFallback(error: error, session: session)
                }
            }
        }
    }

    private func switchToPending(atSilence: Bool) async {
        let reservation = lock.withLockValue { () -> (NodeConfig, Int)? in
            guard !switching, !stopped, let id = pendingMigrationID else { return nil }
            switching = true
            switchGeneration += 1
            guard let node = nodes.first(where: { $0.id == id }) else {
                switching = false
                return nil
            }
            return (node, switchGeneration)
        }
        guard let (node, generation) = reservation else {
            return
        }
        defer {
            lock.withLockVoid {
                if switchGeneration == generation {
                    switching = false
                }
            }
        }
        var created: NodeClient?
        var published = false
        do {
            let (token, info) = try await NodeClient.prepare(config: node)
            guard lock.withLockValue({
                Self.canPublishSwitch(
                    stopped: stopped,
                    switching: switching,
                    switchGeneration: switchGeneration,
                    reservedGeneration: generation,
                    pendingMigrationID: pendingMigrationID,
                    nodeID: node.id
                )
            }) else {
                return
            }
            let sessionGeneration = lock.withLockValue {
                let next = (nodeSessionGenerations[node.id] ?? 0) + 1
                nodeSessionGenerations[node.id] = next
                return next
            }
            // 生产 onClosed 先转给上层（HybridEngine 清缓存），再交给 router 判故障回退（R3-1）
            var wrapped = callbacks
            let upstreamClosed = callbacks.onClosed
            wrapped.onClosed = { [weak self] nodeID, generation, error in
                upstreamClosed(nodeID, generation, error)
                self?.handleClosed(NodeSessionRef(nodeID: nodeID, generation: generation), error: error)
            }
            let client = NodeClient(
                config: node,
                token: token,
                info: info,
                sourceLocaleID: sourceLocaleID,
                targetLanguageID: targetLanguageID,
                offsetSeconds: lock.withLockValue { latestClock },
                sessionGeneration: sessionGeneration,
                callbacks: wrapped
            )
            created = client
            try await client.connect()
            guard lock.withLockValue({
                Self.canPublishSwitch(
                    stopped: stopped,
                    switching: switching,
                    switchGeneration: switchGeneration,
                    reservedGeneration: generation,
                    pendingMigrationID: pendingMigrationID,
                    nodeID: node.id
                )
            }) else {
                await client.close()
                return
            }
            let old = lock.withLockValue { currentClient }
            lock.withLockVoid {
                currentClient = client
                currentSession = NodeSessionRef(nodeID: node.id, generation: sessionGeneration)
                currentNode = node
                currentToken = token
                currentInfo = info
                pendingMigrationID = nil
                silenceRun = 0
            }
            published = true
            onMode(.hybrid(node.id))
            if atSilence, let old {
                let id = UUID()
                lock.withLockVoid {
                    drainTasks[id] = Task {
                        await old.drain(timeout: .seconds(3))
                        await old.close()
                        self.lock.withLockVoid { _ = self.drainTasks.removeValue(forKey: id) }
                    }
                }
            } else {
                await old?.close()
            }
        } catch {
            if !published {
                await created?.close()
            }
            if isCancellationOrStopped(error) {
                return
            }
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

    /// 来自 onClosed 的故障入口：与 send 失败共用 markCurrentOfflineAndFallback（唯一回退入口）。
    private func handleClosed(_ session: NodeSessionRef, error: Error?) {
        let (current, isStopped) = lock.withLockValue { (currentSession, stopped) }
        guard Self.shouldFallbackOnClosed(error: error, closed: session, current: current, routerStopped: isStopped),
              let error else {
            return
        }
        Task { await self.markCurrentOfflineAndFallback(error: error, session: session) }
    }

    /// 只回退「出事的那个会话」：session 已不是当前会话（已迁移/已回退）就忽略，
    /// 并发的 send 失败、心跳超时、receive 断开只会触发一次。
    private func markCurrentOfflineAndFallback(error: Error, session: NodeSessionRef) async {
        guard !isCancellationOrStopped(error) else {
            return
        }
        let target = lock.withLockValue { () -> (NodeConfig, NodeClient?)? in
            guard currentSession == session, let node = currentNode, !failedSessions.contains(session) else {
                return nil
            }
            failedSessions.insert(session)
            return (node, currentClient)
        }
        guard let (failed, client) = target else {
            return
        }
        routeLog("fallback failed_node=\(failed.id) offline_for_s=60 err=\(routeErrorCode(error))")
        // 先关 client（释放 URLSession delegate），再清引用
        await client?.close()
        lock.withLockVoid {
            var state = states[failed.id] ?? NodeStateRecord(info: nil, rtf: nil, offlineUntil: nil)
            state.offlineUntil = nowSeconds() + Self.offlineDuration
            states[failed.id] = state
            if currentSession == session {
                currentClient = nil
                currentSession = nil
                currentNode = nil
                currentToken = nil
                currentInfo = nil
            }
            failedSessions.remove(session)
        }
        persistState()
        onMode(.local)
        await probeOnce()
    }

    /// 纯函数：onClosed 是否应触发节点故障回退。
    /// 只有「当前选中会话」带错误关闭才算；主动关闭（error=nil）、旧会话、取消/已停止都不算。
    static func shouldFallbackOnClosed(
        error: Error?,
        closed: NodeSessionRef,
        current: NodeSessionRef?,
        routerStopped: Bool
    ) -> Bool {
        guard let error, closed == current else {
            return false
        }
        return !isCancellationOrStopped(error, taskIsCancelled: false, routerStopped: routerStopped)
    }

    private func isCancellationOrStopped(_ error: Error) -> Bool {
        Self.isCancellationOrStopped(
            error,
            taskIsCancelled: Task.isCancelled,
            routerStopped: lock.withLockValue { stopped }
        )
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

    /// 同目录临时文件（0600）写满 -> fsync -> rename(2) 覆盖；任何一步失败都不动目标文件。
    static func atomicWriteJSON<T: Encodable>(_ value: T, to url: URL) throws {
        let data = try JSONEncoder().encode(value)
        try FileManager.default.createDirectory(
            at: url.deletingLastPathComponent(),
            withIntermediateDirectories: true,
            attributes: [.posixPermissions: 0o700]
        )
        let tmp = url.deletingLastPathComponent()
            .appendingPathComponent(".\(url.lastPathComponent).\(UUID().uuidString).tmp")
        let fd = open(tmp.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        guard fd >= 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        var ok = false
        defer {
            if !ok {
                unlink(tmp.path)
            }
        }
        do {
            try data.withUnsafeBytes { raw in
                var offset = 0
                while offset < raw.count {
                    let n = write(fd, raw.baseAddress! + offset, raw.count - offset)
                    if n < 0 {
                        if errno == EINTR { continue }
                        throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
                    }
                    offset += n
                }
            }
            fchmod(fd, 0o600)
            fsync(fd)
        } catch {
            close(fd)
            throw error
        }
        close(fd)
        guard rename(tmp.path, url.path) == 0 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        ok = true
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
        check(Self.canPublishSwitch(
            stopped: false,
            switching: true,
            switchGeneration: 2,
            reservedGeneration: 2,
            pendingMigrationID: "better",
            nodeID: "better"
        ), "有效切换预约应允许发布")
        check(!Self.canPublishSwitch(
            stopped: true,
            switching: true,
            switchGeneration: 2,
            reservedGeneration: 2,
            pendingMigrationID: "better",
            nodeID: "better"
        ), "stop 后挂起切换不得发布")
        check(!Self.canPublishSwitch(
            stopped: false,
            switching: false,
            switchGeneration: 2,
            reservedGeneration: 2,
            pendingMigrationID: "better",
            nodeID: "better"
        ), "switching 被取消后不得发布")
        check(!Self.canPublishSwitch(
            stopped: false,
            switching: true,
            switchGeneration: 3,
            reservedGeneration: 2,
            pendingMigrationID: "better",
            nodeID: "better"
        ), "切换代号变化后旧 client 不得发布")
        check(!Self.canPublishSwitch(
            stopped: false,
            switching: true,
            switchGeneration: 2,
            reservedGeneration: 2,
            pendingMigrationID: "other",
            nodeID: "better"
        ), "pending 目标变化后旧 client 不得发布")
        check(Self.isCancellationOrStopped(CancellationError(), taskIsCancelled: false, routerStopped: false),
              "CancellationError 不应算节点故障")
        check(Self.isCancellationOrStopped(URLError(.cancelled), taskIsCancelled: false, routerStopped: false),
              "URLError.cancelled 不应算节点故障")
        check(Self.isCancellationOrStopped(NSError(domain: NSURLErrorDomain, code: NSURLErrorCancelled), taskIsCancelled: false, routerStopped: false),
              "NSURLErrorCancelled 不应算节点故障")
        check(Self.isCancellationOrStopped(NodeClientError.connection("socket closed"), taskIsCancelled: true, routerStopped: false),
              "Task 已取消时发送失败不应算节点故障")
        check(Self.isCancellationOrStopped(NodeClientError.connection("socket closed"), taskIsCancelled: false, routerStopped: true),
              "router stop 后发送失败不应算节点故障")
        check(!Self.isCancellationOrStopped(NodeClientError.connection("close 1006"), taskIsCancelled: false, routerStopped: false),
              "真实连接错误仍应算节点故障")
        // R3-1：onClosed 故障判定
        let a = NodeSessionRef(nodeID: "A", generation: 2)
        let aOld = NodeSessionRef(nodeID: "A", generation: 1)
        let ioError = NodeClientError.pingTimeout
        check(Self.shouldFallbackOnClosed(error: ioError, closed: a, current: a, routerStopped: false),
              "当前会话带错误关闭应回退")
        check(!Self.shouldFallbackOnClosed(error: nil, closed: a, current: a, routerStopped: false),
              "主动关闭（无错误）不应回退")
        check(!Self.shouldFallbackOnClosed(error: ioError, closed: aOld, current: a, routerStopped: false),
              "旧会话出错不应回退当前会话")
        check(!Self.shouldFallbackOnClosed(error: ioError, closed: a, current: nil, routerStopped: false),
              "已无当前会话不应回退")
        check(!Self.shouldFallbackOnClosed(error: ioError, closed: a, current: a, routerStopped: true),
              "router 已停止不应回退")
        check(!Self.shouldFallbackOnClosed(error: URLError(.cancelled), closed: a, current: a, routerStopped: false),
              "取消类错误不应回退")
        // R3-6：原子写
        let dir = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("rslite-selftest-\(UUID().uuidString)")
        let file = dir.appendingPathComponent("node-state.json")
        do {
            try atomicWriteJSON(["a": 1], to: file)
            try atomicWriteJSON(["a": 2], to: file)
            let back = try JSONDecoder().decode([String: Int].self, from: Data(contentsOf: file))
            let perm = (try FileManager.default.attributesOfItem(atPath: file.path)[.posixPermissions] as? NSNumber)?.intValue ?? 0
            let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir.path)
            check(back == ["a": 2], "原子写覆盖后内容应为最新")
            check(perm & 0o777 == 0o600, "原子写后权限应为 0600")
            check(leftovers == ["node-state.json"], "原子写不应留下临时文件")
        } catch {
            check(false, "原子写失败：\(error)")
        }
        try? FileManager.default.removeItem(at: dir)
        failures.append(contentsOf: NodeClient.selfTest().map { "NodeClient: \($0)" })
        return failures
    }

    private static func isCancellationOrStopped(
        _ error: Error,
        taskIsCancelled: Bool,
        routerStopped: Bool
    ) -> Bool {
        if taskIsCancelled || routerStopped {
            return true
        }
        if error is CancellationError {
            return true
        }
        if let urlError = error as? URLError, urlError.code == .cancelled {
            return true
        }
        let nsError = error as NSError
        return nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled
    }

    private static func canPublishSwitch(
        stopped: Bool,
        switching: Bool,
        switchGeneration: Int,
        reservedGeneration: Int,
        pendingMigrationID: String?,
        nodeID: String
    ) -> Bool {
        !stopped && switching && switchGeneration == reservedGeneration && pendingMigrationID == nodeID
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
