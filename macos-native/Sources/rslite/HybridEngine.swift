import Foundation

@available(macOS 27.0, *)
protocol SubtitleEngine: AnyObject, Sendable {
    func start() async throws
    func pause() async
    func resume() async
    func stop() async
    func waitUntilFinished() async
    /// 手动重试一条翻译失败句（本机 / 节点兜底 / 云端句同一套失败缓冲）。id 不存在返回 false。
    @discardableResult
    func retryTranslation(id: Int) -> Bool
}

private func hybridLog(_ message: String) {
    FileHandle.standardError.write(Data("rslite.route \(message)\n".utf8))
}

@available(macOS 27.0, *)
extension Pipeline: SubtitleEngine {}

enum SubtitleMode: Sendable, Equatable {
    case local
    case hybrid(String)
    case refining
    /// 有 key 且这一档正在出字。菜单栏和字幕状态行用同一句。
    case cloud
    /// 401/403 之后保持到 key 变化，优先于「混合」。
    case cloudKeyInvalid

    var label: String {
        switch self {
        case .local: return "本机"
        case .hybrid(let node): return "混合·\(node)"
        case .refining: return "精修中"
        case .cloud: return "云端·Groq"
        case .cloudKeyInvalid: return "云端 key 无效"
        }
    }
}

enum RSLiteMode: String, Sendable {
    case auto
    case local
    case hybrid
}

/// P5 替换瞬间：云端译文还没到时，先拿走被覆盖本机行里已经有的译文。
func partitionInheritedTranslation(
    lines: [SubtitleLine],
    t0: Double?,
    t1: Double?
) -> (text: String?, replaced: Set<Int>) {
    var replaced = Set<Int>()
    var texts: [String] = []
    for line in lines {
        guard LineStore.shouldReplace(line, byNodeT0: t0, t1: t1) else { continue }
        replaced.insert(line.key)
        let trimmed = line.dstText?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
        if !trimmed.isEmpty {
            texts.append(trimmed)
        }
    }
    return (texts.isEmpty ? nil : texts.joined(separator: "\n"), replaced)
}

func inheritedSubtitleTranslation(lines: [SubtitleLine], t0: Double?, t1: Double?) -> String? {
    partitionInheritedTranslation(lines: lines, t0: t0, t1: t1).text
}

@available(macOS 27.0, *)
final class HybridEngine: SubtitleEngine, @unchecked Sendable {
    private let config: PipelineConfig
    private let mode: RSLiteMode
    private let callbacks: PipelineCallbacks
    private let onMode: @Sendable (SubtitleMode) -> Void
    private let onNodeFinal: @Sendable (String, Double?, Double?, String, String?) -> Void
    private let pinnedNode: String?
    private let lock = NSLock()

    private var local: Pipeline?
    private var router: NodeRouter?
    private var currentFanout: AudioFanout?
    private var languageGeneration = 0
    private var activeNodeSessions: [String: Int] = [:]
    private var nodeSessionLanguage: [NodeSessionKey: Int] = [:]
    private var selectedNodeSession: NodeSessionKey?
    private var pendingNodeFinals: [NodeSessionKey: [Int: (String, Double?, Double?)]] = [:]
    private var settledNodeSentences: [NodeSessionKey: Set<Int>] = [:]
    private var earlyTranslateFailures: [NodeSessionKey: Set<Int>] = [:]
    private var enqueueProbe: (@Sendable (Int, String) -> Void)?
    private var cloudController: CloudUploadController?
    private var cloudTask: Task<Void, Never>?
    private var uploadTasks: [UUID: Task<Void, Never>] = [:]
    private var lastPublishedMode: SubtitleMode?
    private var lastNodeTierEnabled = true
    /// 云端或 key 无效占着状态行时，节点路由器的「混合/本机/精修中」不能把它盖掉。
    private var cloudOwnsLabel = false
    private var cloudSuspended = false
    /// 与 LineStore 的节点行键同一序列：从 -1 往下，不与本机句子 id 相撞。
    private var nextDisplayKey = -1
    private var localDrafts: [LocalDraft] = []

    private struct LocalDraft {
        var key: Int
        var t0: Double?
        var t1: Double?
        var dst: String?
    }

    private struct NodeSessionKey: Hashable {
        var nodeID: String
        var generation: Int
    }

    init(
        config: PipelineConfig,
        mode: RSLiteMode,
        callbacks: PipelineCallbacks,
        onMode: @escaping @Sendable (SubtitleMode) -> Void,
        onNodeFinal: @escaping @Sendable (String, Double?, Double?, String, String?) -> Void = { _, _, _, _, _ in },
        pinnedNode: String? = nil
    ) {
        self.config = config
        self.mode = mode
        self.callbacks = callbacks
        self.onMode = onMode
        self.onNodeFinal = onNodeFinal
        self.pinnedNode = pinnedNode
    }

    func start() async throws {
        onMode(activateCloudController())
        let wrapped = PipelineCallbacks(
            onVolatile: callbacks.onVolatile,
            onFinal: { [weak self] id, text, t0, t1 in
                self?.noteLocalFinal(id: id, t0: t0, t1: t1)
                self?.callbacks.onFinal(id, text, t0, t1)
            },
            onTranslation: { [weak self] id, text in
                self?.noteLocalTranslation(id: id, text: text)
                self?.callbacks.onTranslation(id, text)
            },
            onTranslationFailed: { [weak self] id, reason in
                // 节点句走本机兜底 / 云端句本机兜底仍失败：结局原样交给上层（id 是显示键）。
                self?.callbacks.onTranslationFailed(id, reason)
            },
            onStatus: callbacks.onStatus,
            onFinished: callbacks.onFinished,
            onFanoutReady: { [weak self] fanout in
                // 必须在 fanout.start 之前订阅，否则云端和节点都漏掉开头。
                self?.startCloudIfNeeded(fanout: fanout)
                self?.startRouterIfNeeded(fanout: fanout)
                self?.callbacks.onFanoutReady(fanout)
            }
        )
        let pipeline = Pipeline(config: config, callbacks: wrapped)
        lock.withLockVoid { local = pipeline }
        try await pipeline.start()
    }

    func pause() async {
        lock.withLockVoid { cloudSuspended = true }
        cloudController?.cancelInflight()
        cancelUploadTasks()
        await local?.pause()
        await cloudTask?.value
        await cancelAndJoinUploads()
        lock.withLockVoid {
            cloudTask = nil
            cloudOwnsLabel = false
            lastPublishedMode = .local
        }
        await stopRouter(clearPending: true, clearFanout: true)
        onMode(.local)
    }

    func resume() async {
        lock.withLockVoid { cloudSuspended = false }
        await local?.resume()
        ensureCloudRunning()
        ensureRouterRunning()
    }

    func stop() async {
        lock.withLockVoid { cloudSuspended = true }
        cloudController?.cancelInflight()
        cancelUploadTasks()
        await stopRouter(clearPending: true, clearFanout: true)
        await local?.stop()
        await cloudTask?.value
        await cancelAndJoinUploads()
        lock.withLockVoid {
            cloudTask = nil
            cloudController = nil
        }
    }

    func waitUntilFinished() async {
        await local?.waitUntilFinished()
        await cloudTask?.value
        let uploads = lock.withLockValue { Array(uploadTasks.values) }
        for task in uploads {
            await task.value
        }
        await router?.finish()
    }

    /// 失败句缓冲在本机 Pipeline 里（节点兜底 / 云端句也用它的负数显示键入队），交给它重试。
    @discardableResult
    func retryTranslation(id: Int) -> Bool {
        let pipeline = lock.withLockValue { local }
        return pipeline?.retryTranslation(id: id) ?? false
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
                self?.openNodeSession(nodeID: nodeID, generation: generation)
            },
            onFinal: { [weak self] nodeID, generation, id, text, t0, t1 in
                self?.ingestNodeFinal(nodeID: nodeID, generation: generation, id: id, text: text, t0: t0, t1: t1)
            },
            onTranslation: { [weak self] nodeID, generation, id, translation in
                self?.ingestNodeTranslation(nodeID: nodeID, generation: generation, id: id, text: translation)
            },
            onStatus: callbacks.onStatus,
            onTranslateFailed: { [weak self] nodeID, generation, id in
                self?.ingestNodeTranslateFailed(nodeID: nodeID, generation: generation, id: id)
            },
            onClosed: { [weak self] nodeID, generation, _ in
                self?.closeNodeSession(nodeID: nodeID, generation: generation)
            }
        )
        let nodeTierEnabled = lock.withLockValue { lastNodeTierEnabled }
        let router = NodeRouter(
            sourceLocaleID: config.sourceLocaleID,
            targetLanguageID: config.targetLanguageID,
            callbacks: nodeCallbacks,
            onMode: { [weak self, onMode] mode in
                guard let self else { return }
                if self.lock.withLockValue({ self.cloudOwnsLabel }) {
                    return
                }
                self.recordRouterMode(mode)
                onMode(mode)
            },
            nodeTierEnabled: nodeTierEnabled,
            pinnedNode: pinnedNode
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
            nodeSessionLanguage.removeAll()
            selectedNodeSession = nil
            if clearPending {
                pendingNodeFinals.removeAll()
                settledNodeSentences.removeAll()
                earlyTranslateFailures.removeAll()
            }
            return current
        }
        await router?.stop()
    }

    private func clearPending(for nodeID: String) {
        pendingNodeFinals = pendingNodeFinals.filter { $0.key.nodeID != nodeID }
        settledNodeSentences = settledNodeSentences.filter { $0.key.nodeID != nodeID }
        earlyTranslateFailures = earlyTranslateFailures.filter { $0.key.nodeID != nodeID }
        nodeSessionLanguage = nodeSessionLanguage.filter { $0.key.nodeID != nodeID }
    }

    private func openNodeSession(nodeID: String, generation: Int) {
        lock.withLockVoid {
            clearPending(for: nodeID)
            activeNodeSessions[nodeID] = generation
            let key = NodeSessionKey(nodeID: nodeID, generation: generation)
            nodeSessionLanguage[key] = languageGeneration
            pendingNodeFinals[key] = [:]
        }
    }

    private enum NodeSentenceAction {
        case showTranslation(text: String, t0: Double?, t1: Double?, dst: String)
        case fallback(text: String, t0: Double?, t1: Double?)
    }

    private enum IngestStep {
        case none
        case refining
        case publish(NodeSentenceAction, isSelected: Bool)
    }

    private func closeNodeSession(nodeID: String, generation: Int) {
        lock.withLockVoid {
            let closingActive = activeNodeSessions[nodeID] == generation
            Self.applySessionClosed(
                nodeID: nodeID,
                generation: generation,
                pendingFinals: &pendingNodeFinals,
                activeSessions: &activeNodeSessions,
                selectedSession: &selectedNodeSession
            )
            let key = NodeSessionKey(nodeID: nodeID, generation: generation)
            settledNodeSentences.removeValue(forKey: key)
            earlyTranslateFailures.removeValue(forKey: key)
            nodeSessionLanguage.removeValue(forKey: key)
            if closingActive {
                settledNodeSentences = settledNodeSentences.filter { $0.key.nodeID != nodeID }
                earlyTranslateFailures = earlyTranslateFailures.filter { $0.key.nodeID != nodeID }
                nodeSessionLanguage = nodeSessionLanguage.filter { $0.key.nodeID != nodeID }
            }
        }
    }

    private func withSession<T>(
        nodeID: String,
        generation: Int,
        _ body: (
            inout [Int: (String, Double?, Double?)],
            inout Set<Int>,
            inout Set<Int>,
            Bool
        ) -> T
    ) -> T? {
        lock.withLockValue { () -> T? in
            guard activeNodeSessions[nodeID] == generation else { return nil }
            let key = NodeSessionKey(nodeID: nodeID, generation: generation)
            guard nodeSessionLanguage[key] == languageGeneration else {
                hybridLog("stale_language_generation node=\(nodeID) session=\(generation) current=\(languageGeneration)")
                return nil
            }
            var pending = pendingNodeFinals[key] ?? [:]
            var settled = settledNodeSentences[key] ?? []
            var early = earlyTranslateFailures[key] ?? []
            let selected = selectedNodeSession == key
            let result = body(&pending, &settled, &early, selected)
            pendingNodeFinals[key] = pending
            settledNodeSentences[key] = settled
            earlyTranslateFailures[key] = early
            return result
        }
    }

    /// 先到的一边占坑。失败回退之后再来的 translation（包括空串）不再上屏。
    private static func storeFinal(
        id: Int,
        text: String,
        t0: Double?,
        t1: Double?,
        pending: inout [Int: (String, Double?, Double?)],
        settled: inout Set<Int>,
        early: inout Set<Int>
    ) -> NodeSentenceAction? {
        if settled.contains(id) {
            return nil
        }
        if early.remove(id) != nil {
            settled.insert(id)
            return .fallback(text: text, t0: t0, t1: t1)
        }
        pending[id] = (text, t0, t1)
        return nil
    }

    private static func claimTranslation(
        id: Int,
        dst: String,
        pending: inout [Int: (String, Double?, Double?)],
        settled: inout Set<Int>
    ) -> NodeSentenceAction? {
        if settled.contains(id) {
            return nil
        }
        guard let sentence = pending.removeValue(forKey: id) else {
            return nil
        }
        settled.insert(id)
        return .showTranslation(text: sentence.0, t0: sentence.1, t1: sentence.2, dst: dst)
    }

    private static func claimTranslateFailed(
        id: Int,
        pending: inout [Int: (String, Double?, Double?)],
        settled: inout Set<Int>,
        early: inout Set<Int>
    ) -> NodeSentenceAction? {
        if settled.contains(id) {
            return nil
        }
        guard let sentence = pending.removeValue(forKey: id) else {
            early.insert(id)
            return nil
        }
        settled.insert(id)
        return .fallback(text: sentence.0, t0: sentence.1, t1: sentence.2)
    }

    private func ingestNodeFinal(nodeID: String, generation: Int, id: Int, text: String, t0: Double?, t1: Double?) {
        let step = withSession(nodeID: nodeID, generation: generation) { pending, settled, early, selected in
            if let action = Self.storeFinal(
                id: id, text: text, t0: t0, t1: t1,
                pending: &pending, settled: &settled, early: &early
            ) {
                return IngestStep.publish(action, isSelected: selected)
            }
            if selected, pending[id] != nil {
                return IngestStep.refining
            }
            return IngestStep.none
        } ?? .none
        apply(nodeID: nodeID, step: step)
    }

    private func ingestNodeTranslation(nodeID: String, generation: Int, id: Int, text: String) {
        let step = withSession(nodeID: nodeID, generation: generation) { pending, settled, _, selected in
            guard let action = Self.claimTranslation(id: id, dst: text, pending: &pending, settled: &settled) else {
                return IngestStep.none
            }
            return IngestStep.publish(action, isSelected: selected)
        } ?? .none
        apply(nodeID: nodeID, step: step)
    }

    private func ingestNodeTranslateFailed(nodeID: String, generation: Int, id: Int) {
        let step = withSession(nodeID: nodeID, generation: generation) { pending, settled, early, selected in
            guard let action = Self.claimTranslateFailed(
                id: id, pending: &pending, settled: &settled, early: &early
            ) else {
                return IngestStep.none
            }
            return IngestStep.publish(action, isSelected: selected)
        } ?? .none
        apply(nodeID: nodeID, step: step)
    }

    private func apply(nodeID: String, step: IngestStep) {
        switch step {
        case .none:
            return
        case .refining:
            if !lock.withLockValue({ cloudOwnsLabel }) {
                onMode(.refining)
            }
        case .publish(let action, let isSelected):
            publish(nodeID: nodeID, isSelected: isSelected, action: action)
        }
    }

    /// 节点译文和本机回退都占一个显示键，好跟 LineStore 的负数键对齐。
    /// 回退的译文走 Apple 翻译队列，按这个键回来；先到的一边已经占坑，后到的不再覆盖。
    private func publish(nodeID: String, isSelected: Bool, action: NodeSentenceAction) {
        let key = allocateDisplayKey()
        switch action {
        case .showTranslation(let text, let t0, let t1, let dst):
            _ = consumeLocalTranslations(t0: t0, t1: t1)
            onNodeFinal(nodeID, t0, t1, text, dst)
        case .fallback(let text, let t0, let t1):
            let inherited = consumeLocalTranslations(t0: t0, t1: t1)
            onNodeFinal(nodeID, t0, t1, text, inherited)
            let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
            if !trimmed.isEmpty {
                enqueueLocalTranslation(id: key, text: trimmed)
            }
        }
        if isSelected, !lock.withLockValue({ cloudOwnsLabel }) {
            onMode(.hybrid(nodeID))
        }
    }

    private func enqueueLocalTranslation(id: Int, text: String) {
        if let probe = lock.withLockValue({ enqueueProbe }) {
            probe(id, text)
            return
        }
        lock.withLockValue { local }?.enqueueTranslation(id: id, text: text)
    }

    private func recordRouterMode(_ mode: SubtitleMode) {
        lock.withLockVoid {
            selectedNodeSession = Self.selectedSession(after: mode, activeSessions: activeNodeSessions, previous: selectedNodeSession)
            lastPublishedMode = mode
            cloudOwnsLabel = mode == .cloud || mode == .cloudKeyInvalid
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
        case .local, .cloud, .cloudKeyInvalid:
            return nil
        case .refining:
            return previous
        }
    }

    /// 会话关闭的状态清理：生产 onClosed 与 selftest 共用这一个函数。
    private static func applySessionClosed(
        nodeID: String,
        generation: Int,
        pendingFinals: inout [NodeSessionKey: [Int: (String, Double?, Double?)]],
        activeSessions: inout [String: Int],
        selectedSession: inout NodeSessionKey?
    ) {
        let key = NodeSessionKey(nodeID: nodeID, generation: generation)
        pendingFinals.removeValue(forKey: key)
        guard activeSessions[nodeID] == generation else {
            return
        }
        activeSessions.removeValue(forKey: nodeID)
        pendingFinals = pendingFinals.filter { $0.key.nodeID != nodeID }
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
        var pending: [NodeSessionKey: [Int: (String, Double?, Double?)]] = [
            NodeSessionKey(nodeID: "A", generation: 1): [1: ("x", nil, nil)],
            NodeSessionKey(nodeID: "B", generation: 1): [2: ("y", nil, nil)],
        ]
        Self.applySessionClosed(
            nodeID: "A",
            generation: 1,
            pendingFinals: &pending,
            activeSessions: &sessions,
            selectedSession: &selected
        )
        check(pending.count == 1 && pending[NodeSessionKey(nodeID: "B", generation: 1)] != nil,
              "旧会话关闭只应清自己的待定 final")
        check(selected == Self.NodeSessionKey(nodeID: "B", generation: 1),
              "旧会话关闭不应清除当前选中节点")
        check(sessions["A"] == nil && sessions["B"] == 1,
              "旧会话关闭只应清理自己的活跃缓存")
        Self.applySessionClosed(
            nodeID: "B",
            generation: 1,
            pendingFinals: &pending,
            activeSessions: &sessions,
            selectedSession: &selected
        )
        check(selected == nil,
              "当前选中会话关闭才应清除选中状态")
        check(Self.selectedSession(after: .cloud, activeSessions: ["A": 1], previous: Self.NodeSessionKey(nodeID: "A", generation: 1)) == nil,
              "切到云端应清掉节点选中")
        check(Self.selectedSession(after: .cloudKeyInvalid, activeSessions: ["A": 1], previous: Self.NodeSessionKey(nodeID: "A", generation: 1)) == nil,
              "key 无效应清掉节点选中")
        do {
            var store = LineStore()
            store.addLocal(key: 1, t0: 0, t1: 10, text: "Hallo")
            store.setTranslation(key: 1, text: "你好")
            let inherited = partitionInheritedTranslation(lines: store.lines, t0: 0, t1: 10).text
            let key = Self.displayKey(at: 0)
            store.applyNode(nodeID: CloudUploadController.cloudNodeID, t0: 0, t1: 10, srcText: "Guten Tag", dstText: inherited)
            check(store.lines.count == 1 && store.lines[0].key == key, "云端句使用不与本机冲突的 key")
            check(key < 0, "云端句 key 为负数，不与本机句子 id 冲突")
            check(store.lines[0].dstText == "你好", "替换瞬间不出现空译文")
            store.setTranslation(key: key, text: "晚安")
            check(store.lines[0].dstText == "晚安", "云端句最终带译文")
        }
        check(Self.shouldRestartCloud(suspended: false, hasController: true, hasFanout: true, taskRunning: false),
              "暂停后恢复应重新拉起 cloudTask")
        check(!Self.shouldRestartCloud(suspended: true, hasController: true, hasFanout: true, taskRunning: false),
              "暂停未解除时不应拉起 cloudTask")
        check(!Self.shouldRestartCloud(suspended: false, hasController: true, hasFanout: true, taskRunning: true),
              "cloudTask 仍在跑时不应再开一路上传")
        check(Self.fanoutReadyStartsCloud(mode: .auto, suspended: false),
              "start 与 resume 的 fanout 就绪都应拉起云端")
        check(!Self.fanoutReadyStartsCloud(mode: .auto, suspended: true),
              "暂停中不应拉起 cloudTask")
        check(!Self.fanoutReadyStartsCloud(mode: .local, suspended: false),
              "本机模式不拉起云端")
        final class NodeCapture: @unchecked Sendable {
            var finals: [(String, String?)] = []
            var enqueued: [(Int, String)] = []
            var modes: [SubtitleMode] = []
        }
        func makeEngine(_ capture: NodeCapture) -> HybridEngine {
            let engine = HybridEngine(
                config: PipelineConfig(sourceSpec: "file:/dev/null", sourceLocaleID: "de-DE", targetLanguageID: "zh-Hans"),
                mode: .hybrid,
                callbacks: PipelineCallbacks(),
                onMode: { capture.modes.append($0) },
                onNodeFinal: { _, _, _, src, dst in capture.finals.append((src, dst)) }
            )
            engine.enqueueProbe = { capture.enqueued.append(($0, $1)) }
            return engine
        }
        func select(_ engine: HybridEngine, _ nodeID: String, _ generation: Int) {
            engine.openNodeSession(nodeID: nodeID, generation: generation)
            engine.recordRouterMode(.hybrid(nodeID))
        }
        do {
            let capture = NodeCapture()
            let engine = makeEngine(capture)
            select(engine, "yzwdmsnlmm", 2)
            engine.noteLocalFinal(id: 1, t0: 0, t1: 2)
            engine.noteLocalTranslation(id: 1, text: "继承译文")
            engine.ingestNodeFinal(nodeID: "yzwdmsnlmm", generation: 2, id: 7, text: "Guten Tag", t0: 0, t1: 2)
            engine.ingestNodeTranslateFailed(nodeID: "yzwdmsnlmm", generation: 2, id: 7)
            check(capture.finals.count == 1 && capture.finals[0].0 == "Guten Tag" && capture.finals[0].1 == "继承译文",
                  "翻译失败先继承本机译文上屏，不留空")
            check(capture.enqueued.count == 1
                  && capture.enqueued[0].0 == Self.displayKey(at: 0)
                  && capture.enqueued[0].1 == "Guten Tag",
                  "翻译失败按该句显示键入本机翻译队列")
            check(capture.modes == [.refining, .hybrid("yzwdmsnlmm")],
                  "翻译失败后离开精修，回到该节点的混合态")
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 2, id: 7, text: "")
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 2, id: 7, text: "节点译文")
            check(capture.finals.count == 1 && capture.enqueued.count == 1 && engine.nextDisplayKey == -2,
                  "失败后迟到的节点译文（含空串）不得覆盖，也不得再占显示键")
        }
        do {
            let capture = NodeCapture()
            let engine = makeEngine(capture)
            select(engine, "yzwdmsnlmm", 1)
            engine.ingestNodeFinal(nodeID: "yzwdmsnlmm", generation: 1, id: 3, text: "Abend", t0: 1, t1: 2)
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 1, id: 3, text: "晚安")
            engine.ingestNodeTranslateFailed(nodeID: "yzwdmsnlmm", generation: 1, id: 3)
            check(capture.finals.count == 1 && capture.finals[0].0 == "Abend" && capture.finals[0].1 == "晚安"
                  && capture.enqueued.isEmpty,
                  "节点译文先到后不得再入本机翻译队列")
            check(engine.nextDisplayKey == -2, "节点译文先到仍占用一个显示键")
        }
        do {
            let capture = NodeCapture()
            let engine = makeEngine(capture)
            select(engine, "yzwdmsnlmm", 1)
            engine.ingestNodeTranslateFailed(nodeID: "yzwdmsnlmm", generation: 1, id: 1)
            engine.ingestNodeFinal(nodeID: "yzwdmsnlmm", generation: 1, id: 2, text: "Andere", t0: 0, t1: 1)
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 1, id: 2, text: "别的")
            engine.ingestNodeFinal(nodeID: "yzwdmsnlmm", generation: 1, id: 1, text: "Ziel", t0: 2, t1: 3)
            check(capture.finals.count == 2
                  && capture.finals[0].0 == "Andere" && capture.finals[0].1 == "别的"
                  && capture.finals[1].0 == "Ziel" && capture.finals[1].1 == nil,
                  "失败先到只回退对应 id，不抢走其它句")
            check(capture.enqueued.count == 1
                  && capture.enqueued[0].0 == Self.displayKey(at: 1)
                  && capture.enqueued[0].1 == "Ziel",
                  "失败先于 final 到达时，final 到达后按下一个显示键入队")
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 1, id: 1, text: "")
            check(capture.finals.count == 2 && capture.enqueued.count == 1,
                  "回退之后的空译文不得覆盖")
            engine.ingestNodeTranslateFailed(nodeID: "yzwdmsnlmm", generation: 9, id: 5)
            check(capture.enqueued.count == 1, "旧会话的 translate_failed 不得入队")
        }
        do {
            let capture = NodeCapture()
            let engine = makeEngine(capture)
            select(engine, "yzwdmsnlmm", 1)
            let staleLanguageGeneration = engine.currentLanguageGeneration()
            engine.advanceLanguageGenerationForSelfTest()
            let currentLanguageGeneration = engine.currentLanguageGeneration()

            engine.ingestNodeFinal(nodeID: "yzwdmsnlmm", generation: 1, id: 1, text: "Alt", t0: 0, t1: 1)
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 1, id: 1, text: "旧译文")
            check(capture.finals.isEmpty && capture.enqueued.isEmpty,
                  "语言切换前的节点 final/translation 不得上屏或入本机翻译队列")

            select(engine, "yzwdmsnlmm", 2)
            engine.ingestNodeFinal(nodeID: "yzwdmsnlmm", generation: 2, id: 2, text: "Neu", t0: 1, t1: 2)
            engine.ingestNodeTranslation(nodeID: "yzwdmsnlmm", generation: 2, id: 2, text: "新译文")
            check(capture.finals.count == 1 && capture.finals[0].0 == "Neu" && capture.finals[0].1 == "新译文",
                  "语言切换后的节点结果应按最新代正常上屏")

            let staleCloud = CloudControlEffect(
                mode: .cloud,
                nodeTierEnabled: false,
                lines: [CloudP5Line(t0: 3, t1: 4, text: "Cloud alt")],
                discard: false,
                cancelInflight: false,
                publishStatus: false,
                yieldStatusToNode: false
            )
            engine.applyCloudEffect(staleCloud, generation: staleLanguageGeneration)
            check(capture.finals.count == 1, "语言切换前发出的云端结果不得上屏")

            let currentCloud = CloudControlEffect(
                mode: .cloud,
                nodeTierEnabled: false,
                lines: [CloudP5Line(t0: 4, t1: 5, text: "Cloud neu")],
                discard: false,
                cancelInflight: false,
                publishStatus: false,
                yieldStatusToNode: false
            )
            engine.applyCloudEffect(currentCloud, generation: currentLanguageGeneration)
            check(capture.finals.count == 2 && capture.finals[1].0 == "Cloud neu",
                  "语言切换后发出的云端结果应按最新代正常上屏")
        }
        failures.append(contentsOf: CloudSegmenter.selfTest())
        failures.append(contentsOf: CloudUploadController.selfTest())
        return failures
    }

    /// 只有非本机模式才建云端调度。key 和账本都在这时读，selftest 不走 start。
    private func activateCloudController() -> SubtitleMode {
        guard mode != .local else { return .local }
        let controller = CloudUploadController(
            localeID: config.sourceLocaleID,
            ledger: QuotaLedger(),
            readSwitch: {
                let key = CloudConfigStore.readKey()
                return CloudSwitchSnapshot(hasKey: key != nil, enabled: CloudConfigStore.readEnabled(), key: key)
            },
            makeClient: { CloudClient(key: $0) }
        )
        let effect = controller.snapshot(node: NodeTierFacts(enabled: false, healthy: false, activeNodeID: nil, isSelf: false))
        lock.withLockVoid {
            cloudController = controller
            lastNodeTierEnabled = effect.nodeTierEnabled
            lastPublishedMode = effect.mode
            cloudOwnsLabel = effect.mode == .cloud || effect.mode == .cloudKeyInvalid
        }
        return effect.mode
    }

    private func ensureCloudRunning() {
        let fanout = lock.withLockValue { () -> AudioFanout? in
            let fanout = currentFanout ?? local?.fanout
            guard Self.shouldRestartCloud(
                suspended: cloudSuspended,
                hasController: cloudController != nil,
                hasFanout: fanout != nil,
                taskRunning: cloudTask != nil
            ) else {
                return nil
            }
            return fanout
        }
        if let fanout {
            startCloudIfNeeded(fanout: fanout)
        }
    }

    static func shouldRestartCloud(suspended: Bool, hasController: Bool, hasFanout: Bool, taskRunning: Bool) -> Bool {
        !suspended && hasController && hasFanout && !taskRunning
    }

    static func fanoutReadyStartsCloud(mode: RSLiteMode, suspended: Bool) -> Bool {
        mode != .local && !suspended
    }

    static func displayKey(at index: Int) -> Int {
        -1 - index
    }

    private func currentLanguageGeneration() -> Int {
        lock.withLockValue { languageGeneration }
    }

    private func isStaleLanguageGeneration(_ generation: Int, where label: String) -> Bool {
        let current = currentLanguageGeneration()
        guard generation == current else {
            hybridLog("stale_language_generation \(label) generation=\(generation) current=\(current)")
            return true
        }
        return false
    }

    private func advanceLanguageGenerationForSelfTest() {
        lock.withLockVoid {
            languageGeneration += 1
        }
    }

    private func allocateDisplayKey() -> Int {
        lock.withLockValue {
            let key = nextDisplayKey
            nextDisplayKey -= 1
            return key
        }
    }

    private func noteLocalFinal(id: Int, t0: Double?, t1: Double?) {
        lock.withLockVoid {
            localDrafts.removeAll { $0.key == id }
            localDrafts.append(LocalDraft(key: id, t0: t0, t1: t1, dst: nil))
            if localDrafts.count > 200 {
                localDrafts.removeFirst(localDrafts.count - 200)
            }
        }
    }

    private func noteLocalTranslation(id: Int, text: String) {
        lock.withLockVoid {
            guard let index = localDrafts.firstIndex(where: { $0.key == id }) else { return }
            localDrafts[index].dst = text
        }
    }

    private func consumeLocalTranslations(t0: Double?, t1: Double?) -> String? {
        lock.withLockValue {
            let lines = localDrafts.map {
                SubtitleLine(key: $0.key, t0: $0.t0, t1: $0.t1, source: .local, srcText: "", dstText: $0.dst)
            }
            let parted = partitionInheritedTranslation(lines: lines, t0: t0, t1: t1)
            if !parted.replaced.isEmpty {
                localDrafts.removeAll { parted.replaced.contains($0.key) }
            }
            return parted.text
        }
    }

    private func emitCloudLine(_ line: CloudP5Line) {
        let key = allocateDisplayKey()
        let inherited = consumeLocalTranslations(t0: line.t0, t1: line.t1)
        onNodeFinal(CloudUploadController.cloudNodeID, line.t0, line.t1, line.text, inherited)
        lock.withLockValue { local }?.enqueueTranslation(id: key, text: line.text)
    }

    private func startCloudIfNeeded(fanout: AudioFanout) {
        let controller = lock.withLockValue { () -> CloudUploadController? in
            guard Self.fanoutReadyStartsCloud(mode: mode, suspended: cloudSuspended) else { return nil }
            cloudTask?.cancel()
            return cloudController
        }
        guard let controller else { return }
        let stream = fanout.subscribe()
        let task = Task { [weak self] in
            guard let self else { return }
            await self.runCloud(stream: stream, controller: controller)
        }
        lock.withLockVoid { cloudTask = task }
    }

    private func runCloud(stream: AsyncStream<TimedAudio>, controller: CloudUploadController) async {
        for await audio in stream {
            if Task.isCancelled || cloudIsSuspended() { return }
            let effect = controller.ingest(audio: audio, now: Date(), node: currentNodeFacts())
            forwardSilence(effect.atSilence)
            applyCloudEffect(effect.control)
            for segment in effect.uploads {
                if Task.isCancelled || cloudIsSuspended() { return }
                enqueueUpload(segment, controller: controller)
            }
        }
        if Task.isCancelled || cloudIsSuspended() { return }
        // 文件放完没有下一段音频。短段不能再干等 1.5s，否则尾句发不出去。
        let tail = controller.finish(now: Date(), node: currentNodeFacts())
        forwardSilence(tail.atSilence)
        applyCloudEffect(tail.control)
        for segment in tail.uploads {
            enqueueUpload(segment, controller: controller)
        }
        let uploads = lock.withLockValue { Array(uploadTasks.values) }
        for task in uploads {
            await task.value
        }
    }

    private func enqueueUpload(_ segment: EmittedCloudSegment, controller: CloudUploadController) {
        CloudTiming.shared.write { now in
            String(
                format: "rslite.cloud seg id=%d a0=%.3f a1=%.3f dur=%.3f reason=%@ t=%.3f\n",
                locale: Locale(identifier: "en_US_POSIX"),
                segment.id,
                segment.speechA0,
                segment.speechA1,
                segment.duration,
                segment.reason,
                now
            )
        }
        let id = UUID()
        let languageGeneration = currentLanguageGeneration()
        let task = Task { [weak self] in
            guard !Task.isCancelled else { return }
            let now = Date()
            guard let client = controller.prepareSend(segment, now: now) else { return }
            let facts = self?.currentNodeFacts() ?? NodeTierFacts(enabled: false, healthy: false, activeNodeID: nil, isSelf: false)
            if Task.isCancelled {
                let effect = controller.resolveFailure(id: segment.id, error: CancellationError(), now: Date(), node: facts)
                self?.applyCloudEffect(effect, generation: languageGeneration)
                return
            }
            do {
                let result = try await client.transcribe(
                    pcm16Mono16k: segment.pcm16,
                    a0: segment.audioOrigin,
                    duration: segment.duration,
                    language: controller.groqLanguageCode,
                    segmentID: segment.id
                )
                let effect = controller.resolveSuccess(id: segment.id, result: result, now: Date(), node: self?.currentNodeFacts() ?? facts)
                self?.applyCloudEffect(effect, generation: languageGeneration)
            } catch {
                let effect = controller.resolveFailure(id: segment.id, error: error, now: Date(), node: self?.currentNodeFacts() ?? facts)
                self?.applyCloudEffect(effect, generation: languageGeneration)
            }
        }
        lock.withLockVoid { uploadTasks[id] = task }
        Task { [weak self] in
            await task.value
            self?.lock.withLockVoid { _ = self?.uploadTasks.removeValue(forKey: id) }
        }
    }

    private func applyCloudEffect(_ effect: CloudControlEffect, generation: Int? = nil) {
        if let generation, isStaleLanguageGeneration(generation, where: "cloud_result") {
            return
        }
        let decision = lock.withLockValue { () -> (Bool, Bool, NodeRouter?) in
            if effect.yieldStatusToNode {
                cloudOwnsLabel = false
                lastPublishedMode = nil
            }
            var publish = false
            if effect.publishStatus && lastPublishedMode != effect.mode {
                lastPublishedMode = effect.mode
                cloudOwnsLabel = effect.mode == .cloud || effect.mode == .cloudKeyInvalid
                selectedNodeSession = Self.selectedSession(
                    after: effect.mode,
                    activeSessions: activeNodeSessions,
                    previous: selectedNodeSession
                )
                publish = true
            }
            let tierChanged = lastNodeTierEnabled != effect.nodeTierEnabled
            if tierChanged {
                lastNodeTierEnabled = effect.nodeTierEnabled
            }
            return (publish, tierChanged, router)
        }
        if !effect.discard {
            for line in effect.lines {
                emitCloudLine(line)
            }
        }
        if decision.0 {
            onMode(effect.mode)
        }
        if decision.1 {
            decision.2?.setNodeTierEnabled(effect.nodeTierEnabled)
        }
    }

    private func forwardSilence(_ atSilence: Bool) {
        lock.withLockValue { router }?.noteCloudSilence(atSilence)
    }

    private func currentNodeFacts() -> NodeTierFacts {
        let router = lock.withLockValue { self.router }
        return router?.tierFacts() ?? NodeTierFacts(enabled: false, healthy: false, activeNodeID: nil, isSelf: false)
    }

    private func cloudIsSuspended() -> Bool {
        lock.withLockValue { cloudSuspended }
    }

    private func cancelUploadTasks() {
        let tasks = lock.withLockValue { Array(uploadTasks.values) }
        for task in tasks {
            task.cancel()
        }
    }

    private func cancelAndJoinUploads() async {
        cloudController?.cancelInflight()
        let tasks = lock.withLockValue { Array(uploadTasks.values) }
        for task in tasks {
            task.cancel()
        }
        for task in tasks {
            await task.value
        }
    }
}
