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
    // 机器状态，与 Python router.RouteWeights 同一套（2026-10-12 Acrobat 退出后 swap_used_gb 仍高）
    var memWarnPenalty = 8.0
    var memCriticalPenalty = 25.0
    var swapPenalty = 6.0
    var swapHeavyPenalty = 15.0
    // macOS vm_stat 页按 16KB 估算：500 页/分≈8MB/分，5000 页/分≈80MB/分。
    // 用当前换出速率判定内存压力；累计 swapUsedGB 只展示，不参与罚分。
    var swapoutRate = 500.0
    var swapoutHeavyRate = 5_000.0
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
    /// 节点报的模型名 → 打分表的键（与 Python router.canonical_asr_model 同口径）。
    /// 2026-10-12：Mac 报 `mlx-community/whisper-large-v3-turbo`、Windows 报 `large-v3-turbo`，
    /// 都查不到表、全落默认 25 分。
    static func canonicalASRModel(_ model: String) -> String {
        var name = (model.split(separator: "/").last.map(String.init) ?? "")
            .trimmingCharacters(in: .whitespaces).lowercased()
        if !name.isEmpty, !name.hasPrefix("whisper-"), name != "fake" {
            name = "whisper-" + name
        }
        return name
    }

    /// 先精确匹配；量化/变体后缀（-q4、-8bit）按最长前缀匹配并扣 2 分。
    static func asrRank(_ model: String, _ ranks: [String: Double], default fallback: Double = 25) -> Double {
        let name = canonicalASRModel(model)
        if let exact = ranks[name] { return exact }
        let prefixes = ranks.keys.filter { name.hasPrefix($0 + "-") }
        if let best = prefixes.max(by: { $0.count < $1.count }), let rank = ranks[best] {
            return rank - 2
        }
        return fallback
    }

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
        let asr = RoutingDecider.asrRank(model, weights.asrRank)
        // translator_ok 缺失按 true。false 只把 tr 记 0，节点仍可当识别用，不加额外罚分。
        let ranked = weights.translatorRank[translator] ?? (translator.hasPrefix("ollama:") ? 16 : 12)
        let tr = info.translatorQualityEnabled ? ranked : 0
        let rtf = info.asr.rtf ?? candidate.rttMS / 1000.0
        var penalties = 0.0
        if info.inUse { penalties += weights.inUsePenalty }
        if !info.onAC { penalties += weights.batteryPenalty }
        if candidate.rttMS > 100 { penalties += weights.rttPenalty }
        if candidate.isLocal && !localAccurate { penalties += weights.localUnderP8Penalty }
        if info.memPressure == "warn" { penalties += weights.memWarnPenalty }
        if info.memPressure == "critical" { penalties += weights.memCriticalPenalty }
        if let rate = info.swapoutRate {
            if rate > weights.swapoutHeavyRate {
                penalties += weights.swapHeavyPenalty
            } else if rate > weights.swapoutRate {
                penalties += weights.swapPenalty
            }
        }
        let bonus = candidate.isLocal ? weights.noNetworkBonus : 0
        let score = asr + tr - weights.speedK * rtf - penalties + bonus
        let reason = String(
            format: "quality=%.1f speed=%.1f penalties=%.1f bonus=%.1f rtt_ms=%.0f mem=%@/%@",
            asr + tr, -weights.speedK * rtf, penalties, bonus, candidate.rttMS,
            info.memPressure ?? "-", info.swapoutRate.map { String(format: "%.1f", $0) } ?? "-"
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

/// 云端状态机看节点层的四个事实。云端不是节点，不会出现在这份清单里。
struct NodeTierFacts: Sendable, Equatable {
    var enabled: Bool
    var healthy: Bool
    var activeNodeID: String?
    var isSelf: Bool
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

private struct TimedAudioReplayBuffer {
    private let maxSeconds: Double
    private var frames: [TimedAudio] = []

    init(maxSeconds: Double) {
        self.maxSeconds = max(0, maxSeconds)
    }

    mutating func append(_ audio: TimedAudio) {
        guard maxSeconds > 0, audio.duration > 0 else {
            return
        }
        frames.append(audio)
        trim(keepingAudioAfter: Self.audioEndSeconds(audio) - maxSeconds)
    }

    func frames(after cursor: Double?) -> [TimedAudio] {
        guard let cursor else {
            return frames
        }
        return frames.filter { Self.audioEndSeconds($0) > cursor }
    }

    mutating func removeAll() {
        frames.removeAll()
    }

    private mutating func trim(keepingAudioAfter cutoff: Double) {
        while let first = frames.first, Self.audioEndSeconds(first) <= cutoff {
            frames.removeFirst()
        }
    }

    private static func audioEndSeconds(_ audio: TimedAudio) -> Double {
        audio.startSeconds + audio.duration
    }
}

@available(macOS 27.0, *)
final class NodeRouter: @unchecked Sendable {
    static let localUDSID = "local_uds"
    private static let probeInterval: Duration = .seconds(30)
    private static let offlineDuration = 60.0
    private static let replayBufferSeconds = Double(NodeClient.preReadyAudioSeconds)

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
    private var connectingClient: NodeClient?
    private var connectingSession: NodeSessionRef?
    private var connectingReplaySession: NodeSessionRef?
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
    private var confirmedLocalHWHash: String?
    private var replayBuffer = TimedAudioReplayBuffer(maxSeconds: replayBufferSeconds)
    private var nodeAudioSentThrough: Double?
    private var connectingAudioSentThrough: Double?
    /// 云端档为真时不把音频送去节点。默认开，Sprint 001 的连接行为不变。
    private var nodeTierEnabled: Bool
    /// 云端要收回节点会话：等到静音点再关，且不记 offline、不上报「本机」。
    private var releaseNodeAtSilence = false
    /// select 选出的节点。云端档关掉连接时也保留，降级时不用再等一轮 30s 探测。
    private var preferredNodeID: String?
    /// 用户钉住的节点 id。nil 表示不指定，选点与原来一样。
    private let pinnedNode: String?
    private var announcedMissingPin = false
    private var lastPinnedDownNotice: String?
    private var probeWaiter: CheckedContinuation<Void, Never>?
    private var probeKick = false

    init(
        sourceLocaleID: String,
        targetLanguageID: String,
        callbacks: NodeCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void,
        nodesURL: URL = NodeRouter.defaultNodesURL,
        stateURL: URL = NodeRouter.defaultStateURL,
        localAccurate: Bool = Capability.supportsAccurateLocal(),
        nodeTierEnabled: Bool = true,
        pinnedNode: String? = nil
    ) {
        self.sourceLocaleID = sourceLocaleID
        self.targetLanguageID = targetLanguageID
        self.callbacks = callbacks
        self.onMode = onMode
        self.nodesURL = nodesURL
        self.stateURL = stateURL
        self.localAccurate = localAccurate
        self.nodeTierEnabled = nodeTierEnabled
        self.pinnedNode = AppSettings.normalizedPin(pinnedNode)
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

    static var localUDSPath: String {
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        return (home as NSString).appendingPathComponent("Library/Application Support/rs-node/gw.sock")
    }

    func start(fanout: AudioFanout) {
        lock.withLockVoid {
            stopped = false
            nodes = Self.loadNodes(from: nodesURL)
            states = Self.loadState(from: stateURL)
            replayBuffer.removeAll()
            nodeAudioSentThrough = nil
            connectingAudioSentThrough = nil
        }
        let stream = fanout.subscribe(capacity: AudioFanout.defaultCapacity)
        probeTask = Task { await self.probeLoop() }
        audioTask = Task { await self.audioLoop(stream: stream, fanout: fanout) }
    }

    /// 云端档打开时关掉节点连接；降级时再打开。健康节点已经探测过就立刻作为待连接，否则马上再探一次。
    func setNodeTierEnabled(_ enabled: Bool) {
        let shouldProbe = lock.withLockValue { () -> Bool in
            nodeTierEnabled = enabled
            if enabled {
                releaseNodeAtSilence = false
                if currentNode == nil, pendingMigrationID == nil, let preferred = preferredNodeID {
                    pendingMigrationID = preferred
                }
                return preferredNodeID == nil
            }
            pendingMigrationID = nil
            let sessionOpen = currentNode != nil || currentClient != nil || connectingClient != nil
            releaseNodeAtSilence = sessionOpen
            return false
        }
        if shouldProbe {
            requestProbeSoon()
        }
    }

    /// 给云端状态机。isSelf 恒为 false：打分前已经丢掉指向本机的清单条目。
    func tierFacts() -> NodeTierFacts {
        lock.withLockValue {
            NodeTierFacts(
                enabled: !nodes.isEmpty || preferredNodeID != nil,
                healthy: preferredNodeID != nil,
                activeNodeID: currentNode?.id,
                isSelf: false
            )
        }
    }

    func stop() async {
        lock.withLockVoid {
            stopped = true
            switching = false
            switchGeneration += 1
        }
        // 先唤醒等待中的探测，再 cancel。只恢复已挂起的 continuation，避免下一轮误踢。
        resumeProbeWaiter(setKickIfIdle: false)
        probeTask?.cancel()
        audioTask?.cancel()
        lock.withLockValue { switchTask }?.cancel()
        let (client, connecting) = lock.withLockValue { (currentClient, connectingClient) }
        await client?.close()
        if let connecting, !Self.sameClient(connecting, client) {
            await connecting.close()
        }
        lock.withLockVoid {
            currentClient = nil
            currentSession = nil
            connectingClient = nil
            connectingSession = nil
            connectingReplaySession = nil
            currentNode = nil
            currentToken = nil
            currentInfo = nil
            replayBuffer.removeAll()
            nodeAudioSentThrough = nil
            connectingAudioSentThrough = nil
            pendingMigrationID = nil
        }
    }

    /// 音频源结束后收尾：等音频循环退出，对当前节点 drain（最多 5s）把尾部句子收回来再关闭。
    func finish() async {
        await audioTask?.value
        let client = lock.withLockValue { stopped ? nil : currentClient }
        resumeProbeWaiter(setKickIfIdle: false)
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
        while !Task.isCancelled && !lock.withLockValue({ stopped }) {
            // 30s 和「马上再探」谁先到听谁的。取消只唤醒已经挂起的等待，不留下 kick。
            await withTaskGroup(of: Void.self) { group in
                group.addTask { [self] in
                    do {
                        try await Task.sleep(for: Self.probeInterval)
                    } catch {
                        return
                    }
                    if !Task.isCancelled {
                        self.resumeProbeWaiter(setKickIfIdle: true)
                    }
                }
                group.addTask { [self] in
                    await withTaskCancellationHandler {
                        await self.waitForNextProbe()
                    } onCancel: {
                        self.resumeProbeWaiter(setKickIfIdle: false)
                    }
                }
                _ = await group.next()
                group.cancelAll()
            }
            if Task.isCancelled || lock.withLockValue({ stopped }) {
                return
            }
            await probeOnce()
        }
    }

    private func requestProbeSoon() {
        resumeProbeWaiter(setKickIfIdle: true)
    }

    private func resumeProbeWaiter(setKickIfIdle: Bool) {
        lock.lock()
        let waiter = probeWaiter
        probeWaiter = nil
        if waiter == nil && setKickIfIdle {
            probeKick = true
        }
        lock.unlock()
        waiter?.resume()
    }

    private func waitForNextProbe() async {
        await withCheckedContinuation { (continuation: CheckedContinuation<Void, Never>) in
            self.lock.lock()
            if self.probeKick || Task.isCancelled {
                self.probeKick = false
                self.lock.unlock()
                continuation.resume()
                return
            }
            self.probeWaiter = continuation
            self.lock.unlock()
        }
    }

    private func probeOnce() async {
        let allConfigs = lock.withLockValue { nodes }
        let decision = Self.pinnedCandidateDecision(nodeIDs: allConfigs.map(\.id), pinnedNode: pinnedNode)
        if case .missing(let missingID) = decision {
            let tell = lock.withLockValue { () -> Bool in
                if announcedMissingPin { return false }
                announcedMissingPin = true
                return true
            }
            if tell {
                callbacks.onStatus(Self.pinnedMissingStatus(id: missingID))
            }
        }
        let configs = Self.configsForProbe(allConfigs, pinnedNode: pinnedNode)
        var candidates: [RouteCandidate] = []
        let localHWHash = Capability.hwHash()
        var localInfo: NodeInfo?
        let lastConfirmedLocalNodeID = lock.withLockValue { states[Self.localUDSID]?.info?.nodeID }

        let tLocal = ContinuousClock().now
        do {
            let info = try await NodeClient.prepareUDS(path: Self.localUDSPath)
            if Self.sameNonEmptyHash(info.hwHash, localHWHash) {
                localInfo = info
                let rtt = max(1, tLocal.duration(to: ContinuousClock().now).secondsValue * 1000)
                var stateInfo = info
                if lock.withLockValue({ currentNode?.id == Self.localUDSID }) {
                    stateInfo.busy = false
                }
                let offlineUntil = lock.withLockValue { () -> Double? in
                    confirmedLocalHWHash = localHWHash
                    let retained = Self.retainedOfflineUntilForProbeSuccess(
                        states[Self.localUDSID]?.offlineUntil,
                        now: nowSeconds()
                    )
                    states[Self.localUDSID] = NodeStateRecord(info: stateInfo, rtf: stateInfo.asr.rtf, offlineUntil: retained)
                    if currentNode?.id == Self.localUDSID {
                        currentToken = ""
                        currentInfo = stateInfo
                    }
                    return retained
                }
                candidates.append(RouteCandidate(
                    id: Self.localUDSID,
                    expectedNodeID: info.nodeID,
                    info: stateInfo,
                    rttMS: rtt,
                    isLocal: true,
                    offlineUntil: offlineUntil
                ))
            } else {
                routeLog("probe \(Self.localUDSID) rejected=hw_hash_mismatch")
            }
        } catch {
            routeLog("probe \(Self.localUDSID) failed err=\(routeErrorCode(error))")
            lock.withLockVoid {
                var record = states[Self.localUDSID] ?? NodeStateRecord(info: nil, rtf: nil, offlineUntil: nil)
                record.rtf = nil
                states[Self.localUDSID] = record
            }
        }

        let knownLocalNodeID = Self.knownLocalNodeID(localInfo: localInfo, fallbackNodeID: lastConfirmedLocalNodeID)
        for node in configs {
            if Self.isManifestLocalDuplicate(node, knownLocalNodeID: knownLocalNodeID) {
                routeLog("probe node=\(node.id) skipped=local_duplicate")
                lock.withLockVoid { states.removeValue(forKey: node.id) }
                continue
            }
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
                let offlineUntil = lock.withLockValue { () -> Double? in
                    let retained = Self.retainedOfflineUntilForProbeSuccess(
                        states[node.id]?.offlineUntil,
                        now: nowSeconds()
                    )
                    states[node.id] = NodeStateRecord(info: info, rtf: info.asr.rtf, offlineUntil: retained)
                    if currentNode?.id == node.id {
                        currentToken = token
                        currentInfo = info
                    }
                    return retained
                }
                candidates.append(RouteCandidate(
                    id: node.id, expectedNodeID: node.nodeID, info: info, rttMS: rtt, isLocal: false, offlineUntil: offlineUntil
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
        let manifestIDs = lock.withLockValue { nodes.map(\.id) }
        let adjusted = candidates.map { candidate in
            var c = candidate
            c.offlineUntil = lock.withLockValue { states[candidate.id]?.offlineUntil }
            return c
        }
        let best = Self.routeSelection(
            candidates: adjusted,
            nodeIDs: manifestIDs,
            pinnedNode: pinnedNode,
            now: now,
            localAccurate: localAccurate,
            weights: weights
        )
        let currentScore: RouteScore? = lock.withLockValue {
            guard let node = currentNode, let info = currentInfo else { return nil }
            return Self.currentRouteScore(
                node: node,
                info: info,
                offlineUntil: states[node.id]?.offlineUntil,
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
        // 云端档关闭时只记住最优节点，不观察迁移门：观察 nil 会把节点之间的两轮计数清掉。
        let followUp = lock.withLockValue { () -> (notice: String?, drop: NodeSessionRef?) in
            preferredNodeID = best?.id
            let scoredGate: Bool
            if !nodeTierEnabled || releaseNodeAtSilence || currentNode == nil {
                scoredGate = false
            } else {
                var gateCopy = gate
                scoredGate = gateCopy.observe(best: best, current: currentScore)
                gate = gateCopy
            }
            let gateAllows = Self.migrationGateAllows(
                nodeIDs: manifestIDs,
                pinnedNode: pinnedNode,
                bestID: best?.id,
                currentNodeID: currentNode?.id,
                scoredGateAllows: scoredGate
            )
            switch Self.pendingMigrationUpdate(
                nodeTierEnabled: nodeTierEnabled,
                releaseNodeAtSilence: releaseNodeAtSilence,
                currentNodeID: currentNode?.id,
                bestID: best?.id,
                gateAllows: gateAllows
            ) {
            case .set(let id):
                pendingMigrationID = id
            case .keep:
                break
            }
            let notice = Self.pinnedUnavailableNotice(
                nodeIDs: manifestIDs,
                pinnedNode: pinnedNode,
                bestID: best?.id
            )
            let emitNotice = notice != lastPinnedDownNotice ? notice : nil
            lastPinnedDownNotice = notice
            let drop = Self.shouldDropPinnedSession(
                nodeIDs: manifestIDs,
                pinnedNode: pinnedNode,
                bestID: best?.id,
                currentNodeID: currentNode?.id
            ) && !switching ? currentSession : nil
            return (emitNotice, drop)
        }
        if let notice = followUp.notice {
            callbacks.onStatus(notice)
        }
        if let session = followUp.drop {
            await dropPinnedSession(session)
        }
    }

    /// 指定节点已经不可用，而且没有本机节点可迁：拆掉当前会话，字幕留在本机流水线。
    /// 不额外写 offline_until，下一轮探测看到它恢复就按「当前没有会话」接回去。
    private func dropPinnedSession(_ session: NodeSessionRef) async {
        let client = lock.withLockValue { () -> NodeClient? in
            guard currentSession == session, !switching else { return nil }
            let client = currentClient
            currentClient = nil
            currentSession = nil
            currentNode = nil
            currentToken = nil
            currentInfo = nil
            pendingMigrationID = nil
            return client
        }
        guard let client else { return }
        await client.close()
        onMode(.local)
    }

    private static func currentRouteScore(
        node: NodeConfig,
        info: NodeInfo,
        offlineUntil: Double?,
        now: Double,
        localAccurate: Bool,
        weights: RouteWeights = .default
    ) -> RouteScore? {
        RoutingDecider.score(
                RouteCandidate(
                    id: node.id,
                    expectedNodeID: node.id == localUDSID ? info.nodeID : node.nodeID,
                    info: info,
                    rttMS: 1,
                    isLocal: node.id == localUDSID,
                    offlineUntil: offlineUntil
                ),
                now: now,
                localAccurate: localAccurate,
                weights: weights
        )
    }

    private static func sameClient(_ lhs: NodeClient?, _ rhs: NodeClient?) -> Bool {
        guard let lhs, let rhs else {
            return lhs == nil && rhs == nil
        }
        return lhs === rhs
    }

    private static func audioEndSeconds(_ audio: TimedAudio) -> Double {
        audio.startSeconds + audio.duration
    }

    private static func shouldReplayBufferedAudio(currentClient: NodeClient?, currentNode: NodeConfig?) -> Bool {
        currentClient == nil && currentNode == nil
    }

    private func markAudioDeliveredToNode(_ audio: TimedAudio) {
        let end = Self.audioEndSeconds(audio)
        lock.withLockVoid {
            nodeAudioSentThrough = max(nodeAudioSentThrough ?? end, end)
        }
    }

    private func markAudioDeliveredToConnecting(_ audio: TimedAudio, session: NodeSessionRef?) {
        guard let session else {
            return
        }
        let end = Self.audioEndSeconds(audio)
        lock.withLockVoid {
            if connectingSession == session {
                connectingAudioSentThrough = max(connectingAudioSentThrough ?? end, end)
            } else if currentSession == session {
                nodeAudioSentThrough = max(nodeAudioSentThrough ?? end, end)
            }
        }
    }

    private func commitConnectingAudioCursor() {
        if let cursor = connectingAudioSentThrough {
            nodeAudioSentThrough = max(nodeAudioSentThrough ?? cursor, cursor)
        }
        connectingAudioSentThrough = nil
    }

    private static func maxCursor(_ lhs: Double?, _ rhs: Double?) -> Double? {
        switch (lhs, rhs) {
        case let (lhs?, rhs?):
            return max(lhs, rhs)
        case let (lhs?, nil):
            return lhs
        case let (nil, rhs?):
            return rhs
        case (nil, nil):
            return nil
        }
    }

    private func audioLoop(stream: AsyncStream<TimedAudio>, fanout: AudioFanout) async {
        for await audio in stream {
            if Task.isCancelled { return }
            lock.withLockVoid {
                latestClock = audio.startSeconds
                replayBuffer.append(audio)
            }
            // prepare/connect 最长十几秒：放到独立 Task，音频循环不能被它堵住，
            // 否则 AudioFanout 丢旧帧、节点时钟落后（R3-2）。switching 由 switchToPending 内部预约防重入。
            // 云端收回会话优先于连接：同一次静音里不能又连上又拆掉。
            let action = lock.withLockValue {
                Self.nodeTierAction(
                    stopped: stopped,
                    switching: switching,
                    nodeTierEnabled: nodeTierEnabled,
                    releaseNodeAtSilence: releaseNodeAtSilence,
                    pendingMigrationID: pendingMigrationID,
                    silenceRun: silenceRun,
                    hasCurrentNode: currentNode != nil
                )
            }
            switch action {
            case .release:
                await releaseNodeForCloudTier()
            case .connect(let atSilence):
                let task = Task { await self.switchToPending(atSilence: atSilence) }
                lock.withLockVoid { switchTask = task }
            case nil:
                break
            }
            let (client, session, connecting, connectingSession, connectingIsReplaying) = lock.withLockValue {
                (
                    currentClient,
                    currentSession,
                    connectingClient,
                    connectingSession,
                    connectingSession != nil && connectingReplaySession == connectingSession
                )
            }
            do {
                if let connecting, !Self.sameClient(connecting, client), !connectingIsReplaying {
                    do {
                        try await connecting.send(audio)
                        markAudioDeliveredToConnecting(audio, session: connectingSession)
                    } catch {
                        await handleConnectingSendError(error, session: connectingSession)
                    }
                }
                if let client {
                    try await client.send(audio)
                    markAudioDeliveredToNode(audio)
                }
            } catch {
                if Self.isAudioGap(error) {
                    if let session {
                        await endSessionForAudioGap(session: session)
                    }
                    continue
                }
                if isCancellationOrStopped(error) {
                    return
                }
                if let session {
                    await markCurrentOfflineAndFallback(error: error, session: session)
                }
            }
        }
    }

    /// 云端档在静音点拆掉节点会话。不写 offline_until，也不上报本机：状态行这时属于云端。
    private func releaseNodeForCloudTier() async {
        let (client, connecting) = lock.withLockValue { () -> (NodeClient?, NodeClient?) in
            let client = currentClient
            let connecting = connectingClient
            currentClient = nil
            currentSession = nil
            connectingClient = nil
            connectingSession = nil
            connectingReplaySession = nil
            connectingAudioSentThrough = nil
            currentNode = nil
            currentToken = nil
            currentInfo = nil
            pendingMigrationID = nil
            releaseNodeAtSilence = false
            nodeAudioSentThrough = nil
            return (client, connecting)
        }
        if let client {
            let id = UUID()
            lock.withLockVoid {
                drainTasks[id] = Task {
                    await client.drain(timeout: .seconds(3))
                    await client.close()
                    self.lock.withLockVoid { _ = self.drainTasks.removeValue(forKey: id) }
                }
            }
        }
        if let connecting, !Self.sameClient(connecting, client) {
            await connecting.close()
        }
    }

    enum NodeTierAction: Equatable {
        case connect(atSilence: Bool)
        case release
    }

    enum PendingMigrationUpdate: Equatable {
        case set(String?)
        case keep
    }

    /// 节点层关闭时的音频循环决策。默认参数下和原来的「无会话立刻连、有会话等 0.6s」一致。
    static func nodeTierAction(
        stopped: Bool,
        switching: Bool,
        nodeTierEnabled: Bool,
        releaseNodeAtSilence: Bool,
        pendingMigrationID: String?,
        silenceRun: Double,
        hasCurrentNode: Bool
    ) -> NodeTierAction? {
        guard !stopped, !switching else { return nil }
        if releaseNodeAtSilence {
            guard silenceRun >= CloudSegmenter.silenceSeconds else { return nil }
            return .release
        }
        guard nodeTierEnabled, pendingMigrationID != nil else { return nil }
        guard !hasCurrentNode || silenceRun >= CloudSegmenter.silenceSeconds else { return nil }
        return .connect(atSilence: hasCurrentNode)
    }

    /// 静音只认 CloudSegmenter 的判定（20ms RMS 0.005、静音 ≥0.6s）。这里不再另量一整块 RMS。
    static func silenceRun(fromCloudAtSilence atSilence: Bool) -> Double {
        atSilence ? CloudSegmenter.silenceSeconds : 0
    }

    func noteCloudSilence(_ atSilence: Bool) {
        let run = Self.silenceRun(fromCloudAtSilence: atSilence)
        lock.withLockVoid { silenceRun = run }
    }

    /// 待连接的三态：关闭时清掉；没有当前会话时直接采用最优；门没过就保留已有意图。
    static func pendingMigrationUpdate(
        nodeTierEnabled: Bool,
        releaseNodeAtSilence: Bool,
        currentNodeID: String?,
        bestID: String?,
        gateAllows: Bool
    ) -> PendingMigrationUpdate {
        if !nodeTierEnabled || releaseNodeAtSilence {
            return .set(nil)
        }
        if currentNodeID == nil {
            return .set(bestID)
        }
        if gateAllows, let bestID, bestID != currentNodeID {
            return .set(bestID)
        }
        return .keep
    }

    private func switchToPending(atSilence: Bool) async {
        let reservation = lock.withLockValue { () -> (NodeConfig?, Int, Bool)? in
            guard !switching, !stopped, let id = pendingMigrationID else { return nil }
            let shouldReplayBufferedAudio = Self.shouldReplayBufferedAudio(
                currentClient: currentClient,
                currentNode: currentNode
            )
            switching = true
            switchGeneration += 1
            if id == Self.localUDSID {
                return (
                    NodeConfig(id: Self.localUDSID, nodeID: "", url: "", tokenFile: ""),
                    switchGeneration,
                    shouldReplayBufferedAudio
                )
            }
            guard let node = nodes.first(where: { $0.id == id }) else {
                switching = false
                return nil
            }
            return (node, switchGeneration, shouldReplayBufferedAudio)
        }
        guard let (nodeOpt, generation, shouldReplayBufferedAudio) = reservation, var node = nodeOpt else {
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
        var createdSession: NodeSessionRef?
        var published = false
        do {
            let token: String
            let info: NodeInfo
            if node.id == Self.localUDSID {
                info = try await NodeClient.prepareUDS(path: Self.localUDSPath)
                let probedHWHash = lock.withLockValue { confirmedLocalHWHash }
                if Self.hasNonEmptyHashMismatch(info.hwHash, probedHWHash) {
                    throw NodeClientError.nodeIDMismatch(expected: probedHWHash ?? "local_hw_hash", got: info.hwHash)
                }
                node.nodeID = info.nodeID
                token = ""
            } else {
                let p = try await NodeClient.prepare(config: node)
                token = p.0
                info = p.1
            }
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
                udsPath: node.id == Self.localUDSID ? Self.localUDSPath : nil,
                sourceLocaleID: sourceLocaleID,
                targetLanguageID: targetLanguageID,
                offsetSeconds: lock.withLockValue { latestClock },
                sessionGeneration: sessionGeneration,
                callbacks: wrapped
            )
            created = client
            let newSession = NodeSessionRef(nodeID: node.id, generation: sessionGeneration)
            createdSession = newSession
            lock.withLockVoid {
                connectingClient = client
                connectingSession = newSession
                connectingReplaySession = shouldReplayBufferedAudio ? newSession : nil
                connectingAudioSentThrough = nil
            }
            if shouldReplayBufferedAudio {
                try await replayBufferedAudio(to: client, session: newSession)
            }
            try await client.connect()
            // 检查与发布在同一把锁内：并发 select 不能在两者之间改写 pendingMigrationID（R4-1）
            let old = lock.withLockValue { () -> NodeClient?? in
                guard Self.canPublishSwitch(
                    stopped: stopped,
                    switching: switching,
                    switchGeneration: switchGeneration,
                    reservedGeneration: generation,
                    pendingMigrationID: pendingMigrationID,
                    nodeID: node.id
                ) else {
                    return nil
                }
                let previous = currentClient
                commitConnectingAudioCursor()
                currentClient = client
                currentSession = newSession
                connectingClient = nil
                connectingSession = nil
                connectingReplaySession = nil
                currentNode = node
                currentToken = token
                currentInfo = info
                Self.clearPendingIfMatches(&pendingMigrationID, nodeID: node.id)
                silenceRun = 0
                return .some(previous)
            }
            guard let old = old else {
                await client.close()
                lock.withLockVoid {
                    if Self.sameClient(connectingClient, client) {
                        connectingClient = nil
                        connectingSession = nil
                        connectingReplaySession = nil
                        connectingAudioSentThrough = nil
                    }
                }
                return
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
            if Self.isAudioGap(error) {
                if let createdSession {
                    await endConnectingSessionForAudioGap(createdSession)
                } else if !published {
                    await created?.close()
                }
                return
            }
            if !published {
                await created?.close()
                lock.withLockVoid {
                    if Self.sameClient(connectingClient, created) {
                        connectingClient = nil
                        connectingSession = nil
                        connectingReplaySession = nil
                        connectingAudioSentThrough = nil
                    }
                }
            }
            if isCancellationOrStopped(error) {
                return
            }
            routeLog("select local reason=switch_failed node=\(node.id) err=\(routeErrorCode(error))")
            lock.withLockVoid {
                if Self.sameClient(connectingClient, created) {
                    connectingClient = nil
                    connectingSession = nil
                    connectingReplaySession = nil
                    connectingAudioSentThrough = nil
                }
                states[node.id] = NodeStateRecord(info: nil, rtf: nil, offlineUntil: nowSeconds() + Self.offlineDuration)
                Self.clearPendingIfMatches(&pendingMigrationID, nodeID: node.id)
            }
            persistState()
            if lock.withLockValue({ currentNode == nil }) {
                onMode(.local)
            }
        }
    }

    private func replayBufferedAudio(to client: NodeClient, session: NodeSessionRef) async throws {
        while true {
            let batch = lock.withLockValue { () -> [TimedAudio]? in
                guard !stopped, connectingSession == session else {
                    return nil
                }
                let cursor = Self.maxCursor(nodeAudioSentThrough, connectingAudioSentThrough)
                let frames = replayBuffer.frames(after: cursor)
                if frames.isEmpty {
                    connectingReplaySession = nil
                }
                return frames
            }
            guard let batch else {
                return
            }
            guard !batch.isEmpty else {
                return
            }
            for audio in batch {
                try await client.send(audio)
                markAudioDeliveredToConnecting(audio, session: session)
            }
        }
    }

    /// 音频断档过长：干净结束当前会话（不标 offline、不罚时），保留迁移意图，
    /// 下一块音频到达时 audioLoop 因 currentNode==nil 立即重开（节点时钟用新的 latestClock 重建）。
    private func endSessionForAudioGap(session: NodeSessionRef) async {
        let old = lock.withLockValue { () -> NodeClient? in
            guard currentSession == session, let node = currentNode else { return nil }
            let client = currentClient
            currentClient = nil
            currentSession = nil
            currentNode = nil
            currentToken = nil
            currentInfo = nil
            if pendingMigrationID == nil {
                pendingMigrationID = node.id
            }
            return client
        }
        guard let old else { return }
        routeLog("audio_gap session_end node=\(session.nodeID) (not a node fault)")
        onMode(.local)
        await old.close()
    }

    private func handleConnectingSendError(_ error: Error, session: NodeSessionRef?) async {
        guard let session else {
            return
        }
        if Self.isAudioGap(error) {
            await endConnectingSessionForAudioGap(session)
            return
        }
        if isCancellationOrStopped(error) {
            return
        }
    }

    private func endConnectingSessionForAudioGap(_ session: NodeSessionRef) async {
        let old = lock.withLockValue { () -> NodeClient? in
            guard connectingSession == session else { return nil }
            let client = connectingClient
            connectingClient = nil
            connectingSession = nil
            connectingReplaySession = nil
            connectingAudioSentThrough = nil
            if pendingMigrationID == nil {
                pendingMigrationID = session.nodeID
            }
            return client
        }
        guard let old else { return }
        routeLog("audio_gap session_end node=\(session.nodeID) connecting (not a node fault)")
        await old.close()
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
            guard node.id != localUDSID else { return false }
            guard !seen.contains(node.id) else { return false }
            seen.insert(node.id)
            return true
        }
    }

    static func sameNonEmptyHash(_ lhs: String, _ rhs: String) -> Bool {
        !lhs.isEmpty && !rhs.isEmpty && lhs == rhs
    }

    static func hasNonEmptyHashMismatch(_ lhs: String, _ rhs: String?) -> Bool {
        guard let rhs, !lhs.isEmpty, !rhs.isEmpty else { return false }
        return lhs != rhs
    }

    static func isManifestLocalDuplicate(_ node: NodeConfig, knownLocalNodeID: String?) -> Bool {
        guard let knownLocalNodeID, !knownLocalNodeID.isEmpty else { return false }
        return node.nodeID == knownLocalNodeID
    }

    static func knownLocalNodeID(localInfo: NodeInfo?, fallbackNodeID: String?) -> String? {
        if let nodeID = localInfo?.nodeID, !nodeID.isEmpty {
            return nodeID
        }
        if let fallbackNodeID, !fallbackNodeID.isEmpty {
            return fallbackNodeID
        }
        return nil
    }

    static func retainedOfflineUntilForProbeSuccess(_ offlineUntil: Double?, now: Double) -> Double? {
        if let offlineUntil, offlineUntil > now {
            return offlineUntil
        }
        return nil
    }

    static func candidateIDsForSelfTest(
        configs: [NodeConfig],
        localInfo: NodeInfo?,
        lastConfirmedLocalNodeID: String? = nil,
        probe: (NodeConfig) -> NodeInfo?
    ) -> [String] {
        var ids: [String] = []
        if localInfo != nil {
            ids.append(localUDSID)
        }
        let knownLocalNodeID = knownLocalNodeID(localInfo: localInfo, fallbackNodeID: lastConfirmedLocalNodeID)
        for node in configs where !isManifestLocalDuplicate(node, knownLocalNodeID: knownLocalNodeID) {
            guard probe(node) != nil else {
                continue
            }
            ids.append(node.id)
        }
        return ids
    }

    static func loadState(from url: URL = defaultStateURL) -> [String: NodeStateRecord] {
        guard let data = try? Data(contentsOf: url),
              let decoded = try? JSONDecoder().decode([String: NodeStateRecord].self, from: data) else {
            return [:]
        }
        return decoded
    }

    /// 同目录临时文件（0600）写满 -> fsync -> rename(2) 覆盖；任何一步失败都不动目标文件。
    /// fchmod/fsync/write 任一失败 -> 关 fd、unlink 临时文件、抛错，绝不 rename（R4-3）。
    /// `sync` 可注入（默认 fsync），仅供 selftest 模拟失败。
    static func atomicWriteJSON<T: Encodable>(
        _ value: T,
        to url: URL,
        sync: (Int32) -> Int32 = { fsync($0) }
    ) throws {
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
                    if n == 0 {
                        throw POSIXError(.EIO)
                    }
                    offset += n
                }
            }
            if fchmod(fd, 0o600) != 0 {
                throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
            }
            if sync(fd) != 0 {
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
        ok = true
    }

    enum PinnedCandidateDecision: Equatable {
        case unrestricted
        case only(String)
        case missing(String)
    }

    /// 不指定：保持全部候选。指定的 id 在清单里：只留它和本机。不在清单里：只留本机。
    static func pinnedCandidateDecision(nodeIDs: [String], pinnedNode: String?) -> PinnedCandidateDecision {
        guard let pin = AppSettings.normalizedPin(pinnedNode) else {
            return .unrestricted
        }
        if nodeIDs.contains(pin) {
            return .only(pin)
        }
        return .missing(pin)
    }

    static func filterRouteCandidates(
        candidates: [RouteCandidate],
        nodeIDs: [String],
        pinnedNode: String?
    ) -> [RouteCandidate] {
        switch pinnedCandidateDecision(nodeIDs: nodeIDs, pinnedNode: pinnedNode) {
        case .unrestricted:
            return candidates
        case .only(let id):
            return candidates.filter { $0.isLocal || $0.id == id }
        case .missing:
            return candidates.filter(\.isLocal)
        }
    }

    static func configsForProbe(_ nodes: [NodeConfig], pinnedNode: String?) -> [NodeConfig] {
        switch pinnedCandidateDecision(nodeIDs: nodes.map(\.id), pinnedNode: pinnedNode) {
        case .unrestricted:
            return nodes
        case .only(let id):
            return nodes.filter { $0.id == id }
        case .missing:
            return []
        }
    }

    /// 未指定时与 `RoutingDecider.choose` 相同。
    /// 指定节点能打分时就用它，不因为别的节点分数更高而改选。
    /// 指定节点不在清单里，或当前打不出分（离线、忙、鉴权失败）：只在本机候选里选。
    static func routeSelection(
        candidates: [RouteCandidate],
        nodeIDs: [String],
        pinnedNode: String?,
        now: Double,
        localAccurate: Bool,
        weights: RouteWeights = .default
    ) -> RouteScore? {
        let decision = pinnedCandidateDecision(nodeIDs: nodeIDs, pinnedNode: pinnedNode)
        let filtered = filterRouteCandidates(candidates: candidates, nodeIDs: nodeIDs, pinnedNode: pinnedNode)
        if case .only(let id) = decision,
           let pinned = filtered.first(where: { !$0.isLocal && $0.id == id }),
           let score = RoutingDecider.score(pinned, now: now, localAccurate: localAccurate, weights: weights) {
            return score
        }
        return RoutingDecider.choose(
            candidates: filtered,
            now: now,
            localAccurate: localAccurate,
            weights: weights
        )
    }

    /// 未指定时沿用原来的迁移门。指定节点能用就要接回去；它不能用且当前还连着它，就离开。
    static func migrationGateAllows(
        nodeIDs: [String],
        pinnedNode: String?,
        bestID: String?,
        currentNodeID: String?,
        scoredGateAllows: Bool
    ) -> Bool {
        guard case .only(let pinned) = pinnedCandidateDecision(nodeIDs: nodeIDs, pinnedNode: pinnedNode) else {
            return scoredGateAllows
        }
        if bestID == pinned && currentNodeID != pinned {
            return true
        }
        if bestID != pinned && currentNodeID == pinned {
            return true
        }
        return scoredGateAllows
    }

    static func shouldDropPinnedSession(
        nodeIDs: [String],
        pinnedNode: String?,
        bestID: String?,
        currentNodeID: String?
    ) -> Bool {
        guard case .only(let pinned) = pinnedCandidateDecision(nodeIDs: nodeIDs, pinnedNode: pinnedNode) else {
            return false
        }
        return bestID == nil && currentNodeID == pinned
    }

    static func pinnedUnavailableNotice(nodeIDs: [String], pinnedNode: String?, bestID: String?) -> String? {
        guard case .only(let id) = pinnedCandidateDecision(nodeIDs: nodeIDs, pinnedNode: pinnedNode) else {
            return nil
        }
        guard bestID != id else { return nil }
        return id + DesignTokens.Text.nodeUnavailableSuffix
    }

    static func pinnedMissingStatus(id: String) -> String {
        "\(id) 不在节点列表，已用本机"
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
        let ranks = RouteWeights.default.asrRank
        check(RoutingDecider.asrRank("mlx-community/whisper-large-v3-turbo", ranks) == 40, "Mac 节点模型名带仓库前缀")
        check(RoutingDecider.asrRank("large-v3-turbo", ranks) == 40, "Windows 节点模型名无 whisper- 前缀")
        check(RoutingDecider.asrRank("mlx-community/whisper-large-v3-turbo-q4", ranks) == 38, "量化变体按前缀扣 2 分")
        check(RoutingDecider.asrRank("whisper-small", ranks) == 25, "未知模型默认 25")
        // 机器状态：更快的节点在内存严重 / 正在大量换页时应让位；字段缺失（旧节点）不罚分
        var sickInfo = betterInfo
        sickInfo.memPressure = "critical"
        let sick = RouteCandidate(id: "sick", expectedNodeID: "n2", info: sickInfo, rttMS: 20, isLocal: false, offlineUntil: nil)
        check(RoutingDecider.choose(candidates: [base, sick], now: 0, localAccurate: true)?.id == "base",
              "内存严重的节点应让位")
        var swappyInfo = betterInfo
        swappyInfo.swapUsedGB = 35
        swappyInfo.swapoutRate = 6_000
        let swappy = RouteCandidate(id: "swappy", expectedNodeID: "n2", info: swappyInfo, rttMS: 20, isLocal: false, offlineUntil: nil)
        check(RoutingDecider.choose(candidates: [base, swappy], now: 0, localAccurate: true)?.id == "base",
              "正在大量换页的节点应让位")
        var accumulatedSwap = betterInfo
        accumulatedSwap.swapUsedGB = 35
        accumulatedSwap.swapoutRate = 0
        let accumulated = RouteCandidate(
            id: "accumulated", expectedNodeID: "n2", info: accumulatedSwap, rttMS: 20, isLocal: false, offlineUntil: nil
        )
        check(RoutingDecider.choose(candidates: [base, accumulated], now: 0, localAccurate: true)?.id == "accumulated",
              "只有累计 swap 高但换页率为 0 时不罚分")
        let plainScore = RoutingDecider.score(better, now: 0, localAccurate: true)?.score
        var lightSwap = betterInfo
        lightSwap.swapUsedGB = 35
        lightSwap.swapoutRate = nil
        lightSwap.memPressure = "normal"
        let light = RouteCandidate(id: "light", expectedNodeID: "n2", info: lightSwap, rttMS: 20, isLocal: false, offlineUntil: nil)
        check(RoutingDecider.score(light, now: 0, localAccurate: true)?.score == plainScore,
              "正常内存与未知换页率不罚分")
        if let data = #"{"v":2,"node_id":"n","hw_hash":"h","asr":{},"translator":"apple","busy":false,"on_ac":true,"in_use":false,"worker":"warm","mem_pressure":"warn","swap_used_gb":12.5,"swapout_rate":6000}"#.data(using: .utf8),
           let decoded = try? JSONDecoder().decode(NodeInfo.self, from: data) {
            check(decoded.memPressure == "warn" && decoded.swapUsedGB == 12.5 && decoded.swapoutRate == 6_000,
                  "解析 mem_pressure / swap_used_gb / swapout_rate")
        } else {
            check(false, "解析 mem_pressure / swap_used_gb / swapout_rate")
        }
        let localCandidate = RouteCandidate(
            id: localUDSID, expectedNodeID: "n1", info: goodInfo, rttMS: 5, isLocal: true, offlineUntil: nil
        )
        let manifestIDs = ["base", "better", "win"]
        let winBusy = RouteCandidate(id: "win", expectedNodeID: "n3", info: busyInfo, rttMS: 10, isLocal: false, offlineUntil: nil)
        let pool = [localCandidate, base, better, winBusy]
        check(Self.routeSelection(candidates: pool, nodeIDs: manifestIDs, pinnedNode: nil, now: 0, localAccurate: true)?.id
              == RoutingDecider.choose(candidates: pool, now: 0, localAccurate: true)?.id,
              "pinned 为 nil 时选择与原来的 choose 一致")
        check(Self.filterRouteCandidates(candidates: pool, nodeIDs: manifestIDs, pinnedNode: nil).map(\.id) == ["local_uds", "base", "better", "win"],
              "pinned 为 nil 时候选不过滤")
        check(Self.filterRouteCandidates(candidates: pool, nodeIDs: manifestIDs, pinnedNode: "win").map(\.id) == ["local_uds", "win"],
              "pinned 存在时只留该 id 和本机")
        check(Self.filterRouteCandidates(candidates: pool, nodeIDs: manifestIDs, pinnedNode: "ghost").map(\.id) == ["local_uds"],
              "pinned 不存在时只留本机")
        check(Self.filterRouteCandidates(candidates: pool, nodeIDs: manifestIDs, pinnedNode: "  ").map(\.id) == ["local_uds", "base", "better", "win"],
              "空白 pinned 视为不指定")
        check(Self.routeSelection(candidates: pool, nodeIDs: manifestIDs, pinnedNode: "base", now: 0, localAccurate: true)?.id == "base",
              "指定节点可用时选它，不选更快的其它节点")
        check(Self.routeSelection(candidates: pool, nodeIDs: manifestIDs, pinnedNode: "win", now: 0, localAccurate: true)?.id == localUDSID,
              "指定节点不可用时只回本机，不选其它节点")
        check(Self.routeSelection(candidates: [base, better, winBusy], nodeIDs: manifestIDs, pinnedNode: "win", now: 0, localAccurate: true) == nil,
              "指定节点不可用且没有本机候选时不改选其它节点")
        check(Self.routeSelection(candidates: pool, nodeIDs: manifestIDs, pinnedNode: "ghost", now: 0, localAccurate: true)?.id == localUDSID,
              "pinned 不在清单时当作本机")
        let winConfig = NodeConfig(id: "win", nodeID: "n3", url: "ws://win:8791", tokenFile: "/tmp/t")
        let miniConfig = NodeConfig(id: "mini2", nodeID: "n2", url: "ws://mini2:8791", tokenFile: "/tmp/t")
        check(Self.configsForProbe([winConfig, miniConfig], pinnedNode: nil).map(\.id) == ["win", "mini2"],
              "未指定时探测全部节点")
        check(Self.configsForProbe([winConfig, miniConfig], pinnedNode: "mini2").map(\.id) == ["mini2"],
              "指定节点时只探测该 id")
        check(Self.configsForProbe([winConfig, miniConfig], pinnedNode: "ghost").isEmpty,
              "指定节点不在清单时不探测其它节点")
        check(Self.migrationGateAllows(nodeIDs: manifestIDs, pinnedNode: nil, bestID: "b", currentNodeID: "a", scoredGateAllows: false) == false,
              "未指定时沿用原来的迁移门")
        check(Self.migrationGateAllows(nodeIDs: manifestIDs, pinnedNode: "win", bestID: "win", currentNodeID: localUDSID, scoredGateAllows: false),
              "指定节点恢复后应接回，不等分数差")
        check(Self.migrationGateAllows(nodeIDs: manifestIDs, pinnedNode: "win", bestID: localUDSID, currentNodeID: "win", scoredGateAllows: false),
              "指定节点不可用时应离开它")
        check(Self.shouldDropPinnedSession(nodeIDs: manifestIDs, pinnedNode: "win", bestID: nil, currentNodeID: "win"),
              "指定节点不可用且没有本机节点会话时应断开")
        check(!Self.shouldDropPinnedSession(nodeIDs: manifestIDs, pinnedNode: nil, bestID: nil, currentNodeID: "win"),
              "未指定时不因这条规则断开")
        check(Self.pinnedUnavailableNotice(nodeIDs: manifestIDs, pinnedNode: "win", bestID: nil) == "win" + DesignTokens.Text.nodeUnavailableSuffix,
              "指定节点不可用的状态文案")
        check(Self.pinnedUnavailableNotice(nodeIDs: manifestIDs, pinnedNode: "win", bestID: localUDSID) == "win" + DesignTokens.Text.nodeUnavailableSuffix,
              "回到本机节点时也提示指定节点不可用")
        check(Self.pinnedUnavailableNotice(nodeIDs: manifestIDs, pinnedNode: "win", bestID: "win") == nil,
              "指定节点可用时不报不可用")
        check(Self.pinnedMissingStatus(id: "ghost") == "ghost 不在节点列表，已用本机",
              "清单里没有该节点的提示")
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
        let slowInfo = NodeInfo(
            v: 2, nodeID: "n1", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.5),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm"
        )
        let localBadP8 = RouteCandidate(id: localUDSID, expectedNodeID: "n1", info: goodInfo, rttMS: 1, isLocal: true, offlineUntil: nil)
        let localGoodP8 = RouteCandidate(id: localUDSID, expectedNodeID: "n1", info: goodInfo, rttMS: 1, isLocal: true, offlineUntil: nil)
        let localSlowP8 = RouteCandidate(id: localUDSID, expectedNodeID: "n1", info: slowInfo, rttMS: 1, isLocal: true, offlineUntil: nil)
        check(RoutingDecider.score(localBadP8, now: 0, localAccurate: false)!.score < RoutingDecider.score(base, now: 0, localAccurate: false)!.score, "本机节点 + P8 不满足时大幅扣分")
        check(RoutingDecider.choose(candidates: [base, localGoodP8], now: 0, localAccurate: true)?.id == localUDSID,
              "同 rtf 下本机节点应因无网络加分胜出")
        check(RoutingDecider.choose(candidates: [better, localSlowP8], now: 0, localAccurate: true)?.id == "better",
              "远端明显更优时应选远端")
        let ollamaDown = NodeInfo(
            v: 2, nodeID: "od", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.08),
            translator: "ollama:qwen", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: false
        )
        let ollamaUp = NodeInfo(
            v: 2, nodeID: "ou", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.08),
            translator: "ollama:qwen", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: true
        )
        let ollamaLegacy = NodeInfo(
            v: 2, nodeID: "ou", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.08),
            translator: "ollama:qwen", busy: false, onAC: true, inUse: false, worker: "warm"
        )
        let downCandidate = RouteCandidate(id: "down", expectedNodeID: "od", info: ollamaDown, rttMS: 20, isLocal: false, offlineUntil: nil)
        let upCandidate = RouteCandidate(id: "up", expectedNodeID: "ou", info: ollamaUp, rttMS: 20, isLocal: false, offlineUntil: nil)
        if let downScore = RoutingDecider.score(downCandidate, now: 0, localAccurate: true),
           let upScore = RoutingDecider.score(upCandidate, now: 0, localAccurate: true),
           let legacyScore = RoutingDecider.score(
            RouteCandidate(id: "up", expectedNodeID: "ou", info: ollamaLegacy, rttMS: 20, isLocal: false, offlineUntil: nil),
            now: 0, localAccurate: true
           ) {
            check(upScore.score - downScore.score == 16,
                  "translator_ok=false 时 tr 记 0，不加额外罚分")
            check(downScore.reason.contains("quality=40.0") && upScore.reason.contains("quality=56.0"),
                  "translator_ok=false 只把质量项里的 tr 清掉")
            check(legacyScore.score == upScore.score, "translator_ok 缺失按 true，分数与显式 true 相同")
        } else {
            check(false, "translator_ok=false 的节点仍可作识别")
        }
        check(RoutingDecider.choose(candidates: [downCandidate, upCandidate], now: 0, localAccurate: true)?.id == "up",
              "翻译不可用时不应再靠 tr 赢过同条件的可用节点")
        let fastDown = NodeInfo(
            v: 2, nodeID: "fd", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.02),
            translator: "ollama:qwen", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: false
        )
        let slowUp = NodeInfo(
            v: 2, nodeID: "su", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 1),
            translator: "ollama:qwen", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: true
        )
        check(RoutingDecider.choose(candidates: [
            RouteCandidate(id: "fast", expectedNodeID: "fd", info: fastDown, rttMS: 20, isLocal: false, offlineUntil: nil),
            RouteCandidate(id: "slow", expectedNodeID: "su", info: slowUp, rttMS: 20, isLocal: false, offlineUntil: nil),
        ], now: 0, localAccurate: true)?.id == "fast",
              "翻译不可用只把 tr 记 0，更快的识别节点仍可胜出")
        let appleDown = NodeInfo(
            v: 2, nodeID: "ad", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.08),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: false
        )
        let appleUp = NodeInfo(
            v: 2, nodeID: "au", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.08),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: true
        )
        if let appleDownScore = RoutingDecider.score(
            RouteCandidate(id: "ad", expectedNodeID: "ad", info: appleDown, rttMS: 20, isLocal: false, offlineUntil: nil),
            now: 0, localAccurate: true
        ), let appleUpScore = RoutingDecider.score(
            RouteCandidate(id: "au", expectedNodeID: "au", info: appleUp, rttMS: 20, isLocal: false, offlineUntil: nil),
            now: 0, localAccurate: true
        ) {
            check(appleUpScore.score - appleDownScore.score == 20,
                  "translator_ok=false 时 apple 的 tr 也记 0，罚分幅度等于原 tr")
        } else {
            check(false, "translator_ok=false 的 apple 节点仍可作识别")
        }
        check(Self.currentRouteScore(
            node: NodeConfig(id: localUDSID, nodeID: "", url: "", tokenFile: ""),
            info: goodInfo,
            offlineUntil: nil,
            now: 0,
            localAccurate: true
        )?.score == RoutingDecider.score(localGoodP8, now: 0, localAccurate: true)?.score,
              "在任本机节点重打分应使用实际 node_id 且 isLocal=true")
        let localClient = NodeClient(
            config: NodeConfig(id: localUDSID, nodeID: "n1", url: "", tokenFile: ""),
            token: "",
            info: goodInfo,
            udsPath: Self.localUDSPath,
            sourceLocaleID: "de-DE",
            targetLanguageID: "zh-Hans",
            offsetSeconds: 0,
            sessionGeneration: 1,
            callbacks: NodeCallbacks()
        )
        check(localClient.udsPathForSelfTest() == Self.localUDSPath,
              "NodeClient 本机 UDS 会话路径必须来自 NodeRouter.localUDSPath")
        let audioFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: 16_000, channels: 1, interleaved: false)!
        func makeTimedAudio(start: Double, seconds: Double, value: Float = 0.01) -> TimedAudio {
            let frames = AVAudioFrameCount((seconds * audioFormat.sampleRate).rounded())
            let buffer = AVAudioPCMBuffer(pcmFormat: audioFormat, frameCapacity: frames)!
            buffer.frameLength = frames
            if let channel = buffer.floatChannelData?[0] {
                for i in 0..<Int(frames) {
                    channel[i] = value
                }
            }
            return TimedAudio(buffer: buffer, startSeconds: start)
        }
        do {
            var buffer = TimedAudioReplayBuffer(maxSeconds: Self.replayBufferSeconds)
            for i in 0..<3 {
                buffer.append(makeTimedAudio(start: Double(i) * 0.1, seconds: 0.1))
            }
            let replayed = buffer.frames(after: nil)
            var offset: Double?
            var sentSamples = 0
            var pads: [Int] = []
            for audio in replayed {
                if offset == nil {
                    offset = audio.startSeconds
                    pads.append(0)
                } else {
                    pads.append(NodeClient.boundedGapPadSamples(
                        offset: offset ?? 0,
                        sentSamples: sentSamples,
                        audioStart: audio.startSeconds,
                        tolerance: 0
                    ) ?? -1)
                }
                sentSamples += (pads.last ?? 0) + Int((audio.duration * 16_000).rounded())
            }
            check(replayed.map { String(format: "%.1f", $0.startSeconds) } == ["0.0", "0.1", "0.2"],
                  "无客户端期间的音频应按样本时钟顺序回放")
            check(offset == replayed.first?.startSeconds && pads == [0, 0, 0] && sentSamples == 4_800,
                  "回放首帧应成为节点 offset，连续帧不补零")
            check(Self.shouldReplayBufferedAudio(currentClient: nil, currentNode: nil),
                  "首次接入 / 回退本机后重连应启用回放")
            check(!Self.shouldReplayBufferedAudio(
                currentClient: localClient,
                currentNode: NodeConfig(id: "remote", nodeID: "n2", url: "ws://remote:8791", tokenFile: "/tmp/t")
            ), "已有当前节点的迁移不得回放历史音频")
        }
        do {
            var buffer = TimedAudioReplayBuffer(maxSeconds: Self.replayBufferSeconds)
            for i in 0...NodeClient.preReadyAudioSeconds {
                buffer.append(makeTimedAudio(start: Double(i), seconds: 1.0))
            }
            let replayed = buffer.frames(after: nil)
            check(replayed.count == NodeClient.preReadyAudioSeconds && replayed.first?.startSeconds == 1.0,
                  "路由回放缓冲超过 15s 应丢最早音频")
        }
        do {
            var buffer = TimedAudioReplayBuffer(maxSeconds: Self.replayBufferSeconds)
            for i in 0..<5 {
                buffer.append(makeTimedAudio(start: Double(i), seconds: 1.0))
            }
            let replayed = buffer.frames(after: 2.0)
            check(replayed.map(\.startSeconds) == [2.0, 3.0, 4.0],
                  "路由回放只应取尚未发给任何节点的音频")
        }
        check(Self.sameNonEmptyHash("", "") == false, "hw_hash 空串不得匹配")
        check(Self.sameNonEmptyHash("h", "h"), "非空 hw_hash 相同才匹配")
        check(!Self.hasNonEmptyHashMismatch("", "h"), "本机 hw_hash 复核：空串不得判 mismatch")
        check(!Self.hasNonEmptyHashMismatch("h", nil), "本机 hw_hash 复核：缺少探测值不得判 mismatch")
        check(Self.hasNonEmptyHashMismatch("h1", "h2"), "本机 hw_hash 复核：双方非空且不一致才 mismatch")
        check(Self.retainedOfflineUntilForProbeSuccess(100, now: 1) == 100,
              "回退后立即 /v1/info 探测成功不得清掉 offline_until")
        check(Self.retainedOfflineUntilForProbeSuccess(100, now: 101) == nil,
              "过期 offline_until 可在探测成功后清掉")
        let manifestLocal = NodeConfig(id: "mini2", nodeID: "n1", url: "ws://mini2:8791", tokenFile: "/tmp/token")
        let manifestRemote = NodeConfig(id: "remote", nodeID: "n2", url: "ws://remote:8791", tokenFile: "/tmp/token")
        let remoteDifferentHW = NodeInfo(
            v: 2, nodeID: "n2", hwHash: "remote-hw",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.02),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm"
        )
        check(Self.candidateIDsForSelfTest(
            configs: [manifestLocal, manifestRemote],
            localInfo: goodInfo,
            probe: { $0.id == "remote" ? remoteDifferentHW : goodInfo }
        ) == [localUDSID, "remote"], "nodes.json 中同 node_id 本机条目应与 UDS 去重")
        var probedManifestIDs: [String] = []
        check(Self.candidateIDsForSelfTest(
            configs: [manifestLocal, manifestRemote],
            localInfo: nil,
            lastConfirmedLocalNodeID: "n1",
            probe: {
                probedManifestIDs.append($0.id)
                return $0.id == "remote" ? remoteDifferentHW : goodInfo
            }
        ) == ["remote"], "已确认本机 node_id 的清单条目应在 prepare/token 前跳过")
        check(probedManifestIDs == ["remote"], "本机清单条目不得为了 hw_hash 触发 prepare/token")
        check(Self.candidateIDsForSelfTest(
            configs: [],
            localInfo: goodInfo,
            probe: { _ in nil }
        ) == [localUDSID], "空 nodes.json 仍应构造 UDS 探测候选")
        do {
            let dir = URL(fileURLWithPath: NSTemporaryDirectory()).appendingPathComponent("rslite-loadnodes-\(UUID().uuidString)")
            try FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
            let nodesFile = dir.appendingPathComponent("nodes.json")
            let raw = """
            [{"id":"local_uds","node_id":"bad","url":"ws://bad","token_file":"/tmp/t"},{"id":"remote","node_id":"n2","url":"ws://remote","token_file":"/tmp/t"}]
            """
            try Data(raw.utf8).write(to: nodesFile)
            check(Self.loadNodes(from: nodesFile).map(\.id) == ["remote"], "nodes.json 不得占用 local_uds id")
            try? FileManager.default.removeItem(at: dir)
        } catch {
            check(false, "loadNodes local_uds id 自检异常：\(error)")
        }
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
        // R4-3：fsync 失败 -> 抛错、不 rename（旧内容保留）、无残留临时文件
        do {
            try atomicWriteJSON(["a": 3], to: file)
            var threw = false
            do {
                try atomicWriteJSON(["a": 4], to: file, sync: { _ in errno = EIO; return -1 })
            } catch {
                threw = true
            }
            let back = try JSONDecoder().decode([String: Int].self, from: Data(contentsOf: file))
            let leftovers = try FileManager.default.contentsOfDirectory(atPath: dir.path)
            check(threw, "fsync 失败应抛错")
            check(back == ["a": 3], "fsync 失败不得 rename 覆盖目标")
            check(leftovers == ["node-state.json"], "fsync 失败应 unlink 临时文件")
        } catch {
            check(false, "fsync 注入测试异常：\(error)")
        }
        try? FileManager.default.removeItem(at: dir)
        // R4-1：清迁移意图只清自己的
        var pend: String? = "other"
        clearPendingIfMatches(&pend, nodeID: "better")
        check(pend == "other", "R4-1：pending 被改写后不清除新意图")
        pend = "better"
        clearPendingIfMatches(&pend, nodeID: "better")
        check(pend == nil, "R4-1：pending 匹配时清除")
        // R4-2：audioGap 为非故障
        check(isAudioGap(NodeClientError.audioGap), "audioGap 识别")
        check(!isAudioGap(NodeClientError.pingTimeout), "pingTimeout 不是 audioGap")
        check(isCancellationOrStopped(NodeClientError.audioGap, taskIsCancelled: false, routerStopped: false),
              "R4-2：audioGap 视为非故障")
        check(!shouldFallbackOnClosed(error: NodeClientError.audioGap, closed: a, current: a, routerStopped: false),
              "R4-2：audioGap 不触发故障回退")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: true, releaseNodeAtSilence: false,
            pendingMigrationID: "n1", silenceRun: 0, hasCurrentNode: false
        ) == .connect(atSilence: false), "无当前节点时首次连接应立即发生")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: true, releaseNodeAtSilence: false,
            pendingMigrationID: "n1", silenceRun: 0.59, hasCurrentNode: true
        ) == nil, "已有节点时静音不足不应迁移")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: true, releaseNodeAtSilence: false,
            pendingMigrationID: "n1", silenceRun: 0.6, hasCurrentNode: true
        ) == .connect(atSilence: true), "已有节点且静音达到 0.6s 才迁移")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: true, releaseNodeAtSilence: false,
            pendingMigrationID: nil, silenceRun: 1, hasCurrentNode: false
        ) == nil, "没有迁移目标时不应连接")
        check(Self.nodeTierAction(
            stopped: true, switching: false, nodeTierEnabled: true, releaseNodeAtSilence: false,
            pendingMigrationID: "n1", silenceRun: 1, hasCurrentNode: false
        ) == nil, "停止后不应连接")
        check(Self.nodeTierAction(
            stopped: false, switching: true, nodeTierEnabled: true, releaseNodeAtSilence: false,
            pendingMigrationID: "n1", silenceRun: 1, hasCurrentNode: false
        ) == nil, "切换进行中不应重入")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: false, releaseNodeAtSilence: true,
            pendingMigrationID: "n1", silenceRun: 0.6, hasCurrentNode: true
        ) == .release, "云端档静音点应释放节点，且优先于连接")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: false, releaseNodeAtSilence: true,
            pendingMigrationID: nil, silenceRun: 0.59, hasCurrentNode: true
        ) == nil, "释放节点也要等到静音点")
        check(Self.silenceRun(fromCloudAtSilence: true) == CloudSegmenter.silenceSeconds,
              "节点静音只消费分段器的静音判定")
        check(Self.silenceRun(fromCloudAtSilence: false) == 0,
              "分段器未静音时节点不得自行累计静音")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: false, releaseNodeAtSilence: true,
            pendingMigrationID: nil, silenceRun: Self.silenceRun(fromCloudAtSilence: true), hasCurrentNode: true
        ) == .release, "分段器判定静音后才释放节点")
        check(Self.nodeTierAction(
            stopped: false, switching: false, nodeTierEnabled: false, releaseNodeAtSilence: true,
            pendingMigrationID: nil, silenceRun: Self.silenceRun(fromCloudAtSilence: false), hasCurrentNode: true
        ) == nil, "分段器未静音时不释放节点")
        check(Self.pendingMigrationUpdate(
            nodeTierEnabled: false, releaseNodeAtSilence: false, currentNodeID: nil, bestID: "n1", gateAllows: true
        ) == .set(nil), "节点层关闭时应清掉待连接")
        check(Self.pendingMigrationUpdate(
            nodeTierEnabled: true, releaseNodeAtSilence: false, currentNodeID: nil, bestID: "n1", gateAllows: false
        ) == .set("n1"), "无当前节点时待连接应是最优节点")
        check(Self.pendingMigrationUpdate(
            nodeTierEnabled: true, releaseNodeAtSilence: false, currentNodeID: "a", bestID: "b", gateAllows: false
        ) == .keep, "门未通过时不应清掉已有迁移意图")
        check(Self.pendingMigrationUpdate(
            nodeTierEnabled: true, releaseNodeAtSilence: false, currentNodeID: "a", bestID: "b", gateAllows: true
        ) == .set("b"), "门通过时应改写迁移目标")
        failures.append(contentsOf: NodeClient.selfTest().map { "NodeClient: \($0)" })
        return failures
    }

    /// audioGap 属于「非故障」：isCancellationOrStopped 把它并入，故 markCurrentOfflineAndFallback / shouldFallbackOnClosed 都不会罚节点。
    static func isAudioGap(_ error: Error) -> Bool {
        if case NodeClientError.audioGap = error { return true }
        return false
    }

    private static func isCancellationOrStopped(
        _ error: Error,
        taskIsCancelled: Bool,
        routerStopped: Bool
    ) -> Bool {
        if taskIsCancelled || routerStopped {
            return true
        }
        if error is CancellationError || isAudioGap(error) {
            return true
        }
        if let urlError = error as? URLError, urlError.code == .cancelled {
            return true
        }
        let nsError = error as NSError
        return nsError.domain == NSURLErrorDomain && nsError.code == NSURLErrorCancelled
    }

    /// 只清自己的迁移意图：pending 已被并发 select 改写成别的节点就保留（R4-1）。
    static func clearPendingIfMatches(_ pending: inout String?, nodeID: String) {
        if pending == nodeID {
            pending = nil
        }
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
