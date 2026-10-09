import Foundation

@available(macOS 27.0, *)
protocol SubtitleEngine: AnyObject, Sendable {
    func start() async throws
    func pause() async
    func resume() async
    func stop() async
    func waitUntilFinished() async
}

@available(macOS 27.0, *)
extension Pipeline: SubtitleEngine {}

enum SubtitleMode: Sendable, Equatable {
    case local
    case hybrid(String)
    case refining

    var label: String {
        switch self {
        case .local: return "本机"
        case .hybrid(let node): return "混合·\(node)"
        case .refining: return "精修中"
        }
    }
}

enum RSLiteMode: String, Sendable {
    case auto
    case local
    case hybrid
}

@available(macOS 27.0, *)
final class HybridEngine: SubtitleEngine, @unchecked Sendable {
    private let config: PipelineConfig
    private let mode: RSLiteMode
    private let callbacks: PipelineCallbacks
    private let onMode: @Sendable (SubtitleMode) -> Void
    private let onNodeFinal: @Sendable (String, Double?, Double?, String, String?) -> Void
    private let lock = NSLock()

    private var local: Pipeline?
    private var router: NodeRouter?
    private var currentFanout: AudioFanout?
    private var activeNodeSessions: [String: Int] = [:]
    private var selectedNodeSession: NodeSessionKey?
    private var pendingNodeFinals: [NodeSessionKey: [Int: (String, Double?, Double?)]] = [:]

    private struct NodeSessionKey: Hashable {
        var nodeID: String
        var generation: Int
    }

    init(
        config: PipelineConfig,
        mode: RSLiteMode,
        callbacks: PipelineCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void,
        onNodeFinal: @escaping @Sendable (String, Double?, Double?, String, String?) -> Void = { _, _, _, _, _ in }
    ) {
        self.config = config
        self.mode = mode
        self.callbacks = callbacks
        self.onMode = onMode
        self.onNodeFinal = onNodeFinal
    }

    func start() async throws {
        onMode(.local)
        let wrapped = PipelineCallbacks(
            onVolatile: callbacks.onVolatile,
            onFinal: callbacks.onFinal,
            onTranslation: callbacks.onTranslation,
            onStatus: callbacks.onStatus,
            onFinished: callbacks.onFinished,
            onFanoutReady: { [weak self] fanout in
                self?.startRouterIfNeeded(fanout: fanout)
                self?.callbacks.onFanoutReady(fanout)
            }
        )
        let pipeline = Pipeline(config: config, callbacks: wrapped)
        lock.withLockVoid { local = pipeline }
        try await pipeline.start()
    }

    func pause() async {
        await stopRouter(clearPending: true, clearFanout: true)
        await local?.pause()
        onMode(.local)
    }

    func resume() async {
        await local?.resume()
        ensureRouterRunning()
    }

    func stop() async {
        await stopRouter(clearPending: true, clearFanout: true)
        await local?.stop()
    }

    func waitUntilFinished() async {
        await local?.waitUntilFinished()
        await router?.finish()
    }

    private func startRouterIfNeeded(fanout: AudioFanout) {
        lock.withLockVoid { currentFanout = fanout }
        ensureRouterRunning()
    }

    private func ensureRouterRunning() {
        let fanout = lock.withLockValue { () -> AudioFanout? in
            guard Self.shouldRunRouter(mode: mode, hasFanout: currentFanout != nil, hasRouter: router != nil) else {
                return nil
            }
            return currentFanout
        }
        guard let fanout else {
            return
        }
        let nodeCallbacks = NodeCallbacks(
            // 模式标签由 NodeRouter 在切换成功后统一用清单里的 id 上报，这里不重复报
            onReady: { [weak self] nodeID, generation, _ in
                guard let self else { return }
                self.lock.withLockVoid {
                    self.clearPending(for: nodeID)
                    self.activeNodeSessions[nodeID] = generation
                    self.pendingNodeFinals[NodeSessionKey(nodeID: nodeID, generation: generation)] = [:]
                }
            },
            onFinal: { [weak self] nodeID, generation, id, text, t0, t1 in
                guard let self else { return }
                let shouldShowRefining = self.lock.withLockValue { () -> Bool in
                    guard self.activeNodeSessions[nodeID] == generation else {
                        return false
                    }
                    let key = NodeSessionKey(nodeID: nodeID, generation: generation)
                    var node = self.pendingNodeFinals[key] ?? [:]
                    node[id] = (text, t0, t1)
                    self.pendingNodeFinals[key] = node
                    return self.selectedNodeSession == key
                }
                if shouldShowRefining {
                    self.onMode(.refining)
                }
            },
            onTranslation: { [weak self] nodeID, generation, id, translation in
                guard let self else { return }
                let result = self.lock.withLockValue { () -> ((String, Double?, Double?), Bool)? in
                    guard activeNodeSessions[nodeID] == generation else {
                        return nil
                    }
                    let key = NodeSessionKey(nodeID: nodeID, generation: generation)
                    guard var node = pendingNodeFinals[key], let final = node.removeValue(forKey: id) else {
                        return nil
                    }
                    pendingNodeFinals[key] = node
                    return (final, selectedNodeSession == key)
                }
                if let (final, isSelected) = result {
                    self.onNodeFinal(nodeID, final.1, final.2, final.0, translation)
                    if isSelected {
                        self.onMode(.hybrid(nodeID))
                    }
                }
            },
            onStatus: callbacks.onStatus,
            onClosed: { [weak self] nodeID, generation, _ in
                guard let self else { return }
                self.lock.withLockVoid {
                    let key = NodeSessionKey(nodeID: nodeID, generation: generation)
                    self.pendingNodeFinals.removeValue(forKey: key)
                    guard self.activeNodeSessions[nodeID] == generation else {
                        return
                    }
                    self.activeNodeSessions.removeValue(forKey: nodeID)
                    self.clearPending(for: nodeID)
                    if self.selectedNodeSession == key {
                        self.selectedNodeSession = nil
                    }
                }
            }
        )
        let router = NodeRouter(
            sourceLocaleID: config.sourceLocaleID,
            targetLanguageID: config.targetLanguageID,
            callbacks: nodeCallbacks,
            onMode: { [weak self, onMode] mode in
                self?.recordRouterMode(mode)
                onMode(mode)
            }
        )
        lock.withLockVoid { self.router = router }
        router.start(fanout: fanout)
    }

    private func stopRouter(clearPending: Bool, clearFanout: Bool) async {
        let router = lock.withLockValue { () -> NodeRouter? in
            let current = self.router
            self.router = nil
            if clearFanout {
                currentFanout = nil
            }
            activeNodeSessions.removeAll()
            selectedNodeSession = nil
            if clearPending {
                pendingNodeFinals.removeAll()
            }
            return current
        }
        await router?.stop()
    }

    private func clearPending(for nodeID: String) {
        pendingNodeFinals = pendingNodeFinals.filter { $0.key.nodeID != nodeID }
    }

    private func recordRouterMode(_ mode: SubtitleMode) {
        lock.withLockVoid {
            selectedNodeSession = Self.selectedSession(after: mode, activeSessions: activeNodeSessions, previous: selectedNodeSession)
        }
    }

    private static func shouldRunRouter(mode: RSLiteMode, hasFanout: Bool, hasRouter: Bool) -> Bool {
        mode != .local && hasFanout && !hasRouter
    }

    private static func selectedSession(
        after mode: SubtitleMode,
        activeSessions: [String: Int],
        previous: NodeSessionKey?
    ) -> NodeSessionKey? {
        switch mode {
        case .hybrid(let nodeID):
            guard let generation = activeSessions[nodeID] else {
                return previous
            }
            return NodeSessionKey(nodeID: nodeID, generation: generation)
        case .local:
            return nil
        case .refining:
            return previous
        }
    }

    private static func closingSessionClearsSelection(
        nodeID: String,
        generation: Int,
        activeSessions: inout [String: Int],
        selectedSession: inout NodeSessionKey?
    ) {
        let key = NodeSessionKey(nodeID: nodeID, generation: generation)
        guard activeSessions[nodeID] == generation else {
            return
        }
        activeSessions.removeValue(forKey: nodeID)
        if selectedSession == key {
            selectedSession = nil
        }
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        check(Self.shouldRunRouter(mode: .auto, hasFanout: true, hasRouter: false),
              "auto 模式有 fanout 且无 router 时应启动路由")
        check(Self.shouldRunRouter(mode: .hybrid, hasFanout: true, hasRouter: false),
              "hybrid 模式恢复后应重启路由")
        check(!Self.shouldRunRouter(mode: .local, hasFanout: true, hasRouter: false),
              "local 模式不应启动路由")
        check(!Self.shouldRunRouter(mode: .auto, hasFanout: false, hasRouter: false),
              "无 fanout 时不应启动路由")
        check(!Self.shouldRunRouter(mode: .auto, hasFanout: true, hasRouter: true),
              "已有 router 时不应重复启动")
        var sessions = ["A": 1, "B": 1]
        var selected = Self.selectedSession(
            after: .hybrid("B"),
            activeSessions: sessions,
            previous: Self.NodeSessionKey(nodeID: "A", generation: 1)
        )
        Self.closingSessionClearsSelection(
            nodeID: "A",
            generation: 1,
            activeSessions: &sessions,
            selectedSession: &selected
        )
        check(selected == Self.NodeSessionKey(nodeID: "B", generation: 1),
              "旧会话关闭不应清除当前选中节点")
        check(sessions["A"] == nil && sessions["B"] == 1,
              "旧会话关闭只应清理自己的活跃缓存")
        Self.closingSessionClearsSelection(
            nodeID: "B",
            generation: 1,
            activeSessions: &sessions,
            selectedSession: &selected
        )
        check(selected == nil,
              "当前选中会话关闭才应清除选中状态")
        return failures
    }
}
