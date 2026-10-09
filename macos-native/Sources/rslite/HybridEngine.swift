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
    private var pendingNodeFinals: [String: [Int: (String, Double?, Double?)]] = [:]

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
        await router?.stop()
        await local?.pause()
        onMode(.local)
    }

    func resume() async {
        await local?.resume()
    }

    func stop() async {
        await router?.stop()
        await local?.stop()
    }

    func waitUntilFinished() async {
        await local?.waitUntilFinished()
        await router?.finish()
    }

    private func startRouterIfNeeded(fanout: AudioFanout) {
        guard mode != .local else {
            return
        }
        let nodeCallbacks = NodeCallbacks(
            // 模式标签由 NodeRouter 在切换成功后统一用清单里的 id 上报，这里不重复报
            onReady: { _ in },
            onFinal: { [weak self] nodeID, id, text, t0, t1 in
                self?.lock.withLockVoid {
                    var node = self?.pendingNodeFinals[nodeID] ?? [:]
                    node[id] = (text, t0, t1)
                    self?.pendingNodeFinals[nodeID] = node
                }
                self?.onMode(.refining)
            },
            onTranslation: { [weak self] nodeID, id, translation in
                guard let self else { return }
                let final = self.lock.withLockValue { () -> (String, Double?, Double?)? in
                    guard var node = pendingNodeFinals[nodeID], let final = node.removeValue(forKey: id) else {
                        return nil
                    }
                    pendingNodeFinals[nodeID] = node
                    return final
                }
                if let final {
                    self.onNodeFinal(nodeID, final.1, final.2, final.0, translation)
                    self.onMode(.hybrid(nodeID))
                }
            },
            onStatus: callbacks.onStatus,
            onClosed: { [onMode] error in
                if error != nil {
                    onMode(.local)
                }
            }
        )
        let router = NodeRouter(
            sourceLocaleID: config.sourceLocaleID,
            targetLanguageID: config.targetLanguageID,
            callbacks: nodeCallbacks,
            onMode: onMode
        )
        lock.withLockVoid { self.router = router }
        router.start(fanout: fanout)
    }
}
