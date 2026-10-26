import CoreMedia
import AudioToolbox
import os
import Darwin.Mach
import Foundation
import Speech
import Translation

/// 广播扩展与未来的麦克风采集共用的字幕路由引擎。
/// 不引入 ReplayKit，主 App 才能复用同一套节点、本机、云端与状态逻辑。
final class SubtitleEngine {
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "ext")
    private let source: String

    /// ReplayKit 适配层在需要终止广播时接入；引擎本身不依赖扩展 API。
    var onFatalError: ((Error) -> Void)?

    init(source: String) {
        self.source = source
    }

    private var blockCount: UInt64 = 0
    private var lastWriteTime: CFAbsoluteTime = 0
    private var lastStatusWriteTime: CFAbsoluteTime = 0
    private var lastLinesJSON: String = ""
    private var accumulatedRMS: Float = 0
    private var rmsSampleCount: Int = 0
    private var sampleRate: Double = 0
    private var channelCount: UInt32 = 0
    private var formatString: String = "unknown"
    private var formatFlags: UInt32 = 0
    private var isNonInterleaved: Bool = false

    private var converter: PCMConverter?
    private var session: NodeSession?
    private var localEngine: AnyObject?
    private var engineMode: String = "auto"
    private var autoFallback: AutoFallback?
    /// 当前音频往哪走：云端 / 节点 / 本机（自动模式下由路由状态机决定）。
    private var routingTier: ProviderTier = .node
    private var localSentenceActive = false
    private var nodeName: String = ""
    private var readyReceived = false
    private var framesSent: UInt64 = 0
    private var finalsCount: Int = 0
    private var translationsCount: Int = 0
    private var preReadyDropped: UInt64 = 0
    private var lastStatusCode: String = ""
    private var fatalErrorOccurred = false
    private var fatalErrorReason: String = ""

    // MARK: - 云端档（Groq）

    /// 云端行的 P5 来源标识。与 mac rslite 同一个字符串，四端对同一句的判定才一致。
    private static let cloudNodeID = "cloud:groq"
    /// 交给 Groq 的语言码（locale 的头一段，与 mac rslite 的 groqLanguage 同口径）。
    private var cloudLanguage: String {
        let source = nodeConfig?.src ?? "de"
        let head = source.split(whereSeparator: { $0 == "-" || $0 == "_" }).first.map(String.init) ?? source
        return head.isEmpty ? "de" : head.lowercased()
    }
    /// 一帧 16k 单声道 s16le = 0.1 秒（PCMConverter 攒的帧长）。
    private static let frameSeconds = Double(PCMConverter.frameBytes) / 2.0 / 16_000.0

    private var cloudSegmenter: CloudSegmenter?
    private var cloudClient: CloudClient?
    private var cloudLedger: QuotaLedger?
    /// 只做翻译的引擎：云端只给原文，译文要用本机 TranslationSession。
    private var cloudTranslator: AnyObject?
    /// 只翻译引擎的轮次标识：跟着引擎一起换，迟到的回调据此丢弃。
    private var cloudTranslatorToken = ""
    /// 本机翻译能不能用（准备成功后为 true）。云端这一档的前提条件之一。
    private var cloudTranslationAvailable = false
    /// 同一时间只保留一段上传音频：广播扩展只有 ~50MB，排队会无界增长。
    private var cloudUploadBusy = false
    /// probe 状态只发一段试探（冷却到期后的那一次）。
    private var cloudProbeUsed = false
    /// 开关/key 变了就 +1：在飞的结果回来时对不上就丢掉。
    private var cloudGeneration: UInt64 = 0
    private var cloudKeyRevision: Int = 0
    private var cloudDroppedSegments: Int = 0
    private var cloudUploadedSegments: Int = 0
    private var cloudNextLineID = 1
    private var cloudSessionCode = ""
    /// 样本时钟：转换器已吐出的音频秒数。云端段与本机句的时间都用它对齐。
    private var sampleClock: Double = 0
    /// 本机引擎启动时的样本时钟：本机句的时间 = 起点 + 识别结果里的区间。
    private var localClockOrigin: Double = 0
    /// P5 用的行仓库（覆盖判定在 LineStore 里，规则与 mac rslite 同一份）。
    private var p5Store = LineStore()
    /// P5 仓库的键 → 显示用的（会话代号, 句子 id）。
    private var p5Identity: [Int: (session: String, id: Int)] = [:]
    private var p5NextKey = 1

    // 重连状态
    private var reconnectPolicy = ReconnectPolicy()
    private var reconnectCount: Int = 0
    private var nodeConfig: NodeConfig?

    private var recentLines: [RecentLine] = []
    private let maxRecentLines = 3
    private let stateQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.state")
    private let connectionQueue = DispatchQueue(label: "com.wyl2607.rsios.spike.connection")
    private var broadcastActive = false
    /// 所有节点异步回调都带着创建时的代号；必须在 stateQueue 上核对，不能跨轮次写状态。
    private var runGate = EngineRunGate()

    /// 引擎会话代号：每次开一个新的引擎会话换一个新代号。
    ///
    /// 为什么需要：本机引擎的 id 每次启动都从 1 开始，节点会话的 id 也按会话重新计数。
    /// 主 App 靠 id 去重（同 id 视为同一句、译文后到就改写那一行），
    /// 只看 id 的话，后一个会话的第 1 句会把前一个会话的第 1 句覆盖掉，
    /// 用户看到的是字幕被改写而不是追加。带上代号之后主 App 用「代号 + id」当键。
    private var localSessionCode: String = ""
    private var nodeSessionCode: String = ""
    /// 节点重连不会开启新的引擎轮次：单独记住会话身份，旧连接的迟到回调才不会碰到新连接。
    private var nodeSessionToken: UUID?

    /// 本机这边原文已定稿、译文还没收尾的句子。
    /// 切回节点要等它们收尾，否则最后一句只剩原文。
    private var pendingLocalSettles: Set<Int> = []

    struct RecentLine: Codable, SessionScopedLine {
        let id: Int
        let src: String
        var dst: String
        /// 产生这一句的引擎会话代号：主 App 用「代号 + id」去重。
        let session: String
    }

    func start() {
        let generation = stateQueue.sync { runGate.begin() }
        logger.info("Subtitle engine started source=\(self.source)")
        engineMode = SharedKeychain.readEngineMode()
        // 真机状态行必须直接暴露本轮实际读到的档位，才能区分保存失败与路由问题。
        writeStatus("started mode=\(engineMode)")

        if engineMode == "local" {
            // broadcastActive 只在这里（和 auto/node 的启动处）置真：startLocalEngine 会先确认它。
            stateQueue.sync {
                broadcastActive = true
                // 本机档也要清掉上轮节点遗留的退避和会话，状态行才不会显示旧 reconn。
                reconnectCount = 0
                reconnectPolicy = ReconnectPolicy()
                session = nil
                nodeSessionToken = nil
            }
            if #available(iOS 26.0, *) {
                startLocalEngine()
            } else {
                writeStatus("last=local_needs_ios26")
                stateQueue.sync {
                    fatalErrorOccurred = true
                    fatalErrorReason = "fatal:local_needs_ios26"
                }
            }
            return
        }

        if engineMode == "auto" {
            startAutoMode(generation: generation)
            return
        }

        guard let json = SharedKeychain.readNodeConfig(),
              let data = json.data(using: .utf8),
              let config = try? JSONDecoder().decode(NodeConfig.self, from: data) else {
            writeStatus("no_config")
            return
        }

        nodeName = config.name
        nodeConfig = config
        converter = PCMConverter()
        stateQueue.sync {
            readyReceived = false
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            session = nil
            nodeSessionToken = nil
            broadcastActive = true
            reconnectPolicy = ReconnectPolicy()
            reconnectCount = 0
        }

        startPinnedSession(config, generation: generation)
    }

    func finish() {
        logger.info("Subtitle engine finished source=\(self.source)")
        
        if engineMode == "local" {
            stateQueue.sync {
                broadcastActive = false
                runGate.end()
            }
            stopLocalEngine()
            converter = nil
            writeStatus("finished")
            return
        }

        let currentSession = stateQueue.sync { () -> NodeSession? in
            broadcastActive = false
            runGate.end()
            // 决策器也要知道广播结束了：之后迟到的节点事件不许再拉起本机引擎。
            self.autoFallback?.broadcastEnded()
            let currentSession = session
            session = nil
            nodeSessionToken = nil
            return currentSession
        }
        currentSession?.flush()
        currentSession?.close()
        if engineMode == "auto" {
            stopLocalEngine()
            stopCloudTier()
        }
        let translator = stateQueue.sync { () -> AnyObject? in
            let translator = cloudTranslator
            cloudTranslator = nil
            cloudTranslatorToken = ""
            cloudTranslationAvailable = false
            return translator
        }
        if #available(iOS 26.0, *) {
            (translator as? LocalEngine)?.stop()
        }
        converter = nil
        writeStatus("finished")
    }

    // MARK: - 自动模式（C1/C2）：决策全在 AutoFallback，这里只做接线

    private var autoLocalEngineAvailable: Bool {
        if #available(iOS 26.0, *) { return true }
        return false
    }

    private func startAutoMode(generation: Int) {
        converter = PCMConverter()
        // 云端档的事实：key / 开关 / 本机能不能翻译。
        // 翻译能不能用是异步查的（要起 TranslationSession），先按「不能」算，
        // 准备好了再 updateCloud 让路由状态机重新选档。
        autoFallback = AutoFallback(
            nodeName: "电脑节点",
            localAvailable: autoLocalEngineAvailable,
            cloud: readCloudAvailability()
        )
        stateQueue.sync {
            readyReceived = false
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            session = nil
            nodeSessionToken = nil
            broadcastActive = true
            reconnectPolicy = ReconnectPolicy()
            reconnectCount = 0
            routingTier = .node
            localSentenceActive = false
            pendingLocalSettles.removeAll()
            cloudSessionCode = newSessionCode()
            // 云端这一档的运行时状态：每次广播从零开始。
            cloudTranslationAvailable = false
            cloudUploadBusy = false
            cloudProbeUsed = false
            cloudDroppedSegments = 0
            cloudUploadedSegments = 0
            cloudNextLineID = 1
            // 代次跨轮单调：上一轮在飞的云端响应回来时必须判过期，
            // 归零会让它和本轮的请求撞上同一个代次。
            cloudGeneration &+= 1
            sampleClock = 0
            p5Store = LineStore()
            p5Identity.removeAll()
            p5NextKey = 1
        }

        guard let json = SharedKeychain.readNodeConfig(),
              let data = json.data(using: .utf8),
              let config = try? JSONDecoder().decode(NodeConfig.self, from: data) else {
            // 没配对过节点：自动模式直接落到本机（iOS < 26 则只显示状态）。
            writeStatus("no_config")
            applyAutoDecision(handleAutoEvent(.nodeFailed))
            // 节点没有，但云端有：云端这一档照样能用。
            prepareCloudTier()
            return
        }

        nodeName = config.name
        nodeConfig = config
        autoFallback?.nodeName = config.name
        autoFallback?.setNodeEnabled(true)
        startPinnedSession(config, generation: generation)
        // 云端档：先确认本机翻译能用，再决定要不要进云端档。
        prepareCloudTier()
    }

    /// 在 stateQueue 上改 AutoFallback 并取回决策；动作本身在锁外执行，
    /// 因为启动/停止本机引擎也会进 stateQueue。
    private func handleAutoEvent(_ event: AutoFallbackEvent) -> AutoFallbackDecision? {
        stateQueue.sync {
            guard engineMode == "auto", var fallback = autoFallback else { return nil }
            let decision = fallback.handle(event)
            autoFallback = fallback
            return decision
        }
    }

    private func applyAutoDecision(_ decision: AutoFallbackDecision?) {
        guard let decision else { return }
        switch decision.action {
        case .keep:
            break
        case .startLocal:
            if #available(iOS 26.0, *) {
                startLocalEngine()
            }
        case .stopLocalAndSwitchToNode:
            stopLocalEngine()
            stateQueue.sync {
                routingTier = .node
                localSentenceActive = false
                // 本机档已经离开：上一轮的本机失败码留着会让状态行提示误报。
                if Self.isLocalFailureCode(lastStatusCode) {
                    lastStatusCode = ""
                }
            }
        case .startCloud:
            startCloudTier()
        case .stopCloudAndSwitchToNode:
            stopCloudTier()
            stateQueue.sync {
                routingTier = .node
                // 云端档依赖本机翻译，失败码同样可能是本机残留的。
                if Self.isLocalFailureCode(lastStatusCode) {
                    lastStatusCode = ""
                }
            }
        case .stopCloudAndStartLocal:
            stopCloudTier()
            if #available(iOS 26.0, *) {
                startLocalEngine()
            }
        case .stopCloud:
            // 没有档位能接手：只把云端停掉，节点重连成功后由 nodeReady 接管。
            stopCloudTier()
            stateQueue.sync { routingTier = .node }
        }
        if let status = decision.status {
            writeStatus("last=\(status)")
        }
    }

    /// 本机这一档自己的失败码。清的时候只认这些，节点状态（closed=… / ready 等）不能碰。
    private static func isLocalFailureCode(_ code: String) -> Bool {
        if code.hasPrefix("asr_") { return true }
        switch code {
        case "analyzer_format_failed", "tx_unavailable", "tx_prepare_failed", "local_needs_ios26":
            return true
        default:
            return false
        }
    }

    /// 冷却到点：路由状态机进 probe，下一段只发一次试探。
    private func handleAutoTick() {
        let decision = stateQueue.sync { () -> AutoFallbackDecision? in
            guard engineMode == "auto", var fallback = autoFallback else { return nil }
            let result = fallback.tick(now: Date())
            autoFallback = fallback
            return result
        }
        guard let decision else { return }
        applyAutoDecision(decision)
    }

    private func stopLocalEngine() {
        let engine = stateQueue.sync { () -> AnyObject? in
            // 在锁内清掉引用：回调的闸门也在锁内核对它，迟到的回调到这里就进不来了。
            let engine = localEngine
            localEngine = nil
            // 引擎没了，在途的译文不会再回调：把没收尾的句子清掉，
            // 否则「本机还忙」的判断会一直挂着。
            pendingLocalSettles.removeAll()
            localSentenceActive = false
            return engine
        }
        if #available(iOS 26.0, *) {
            (engine as? LocalEngine)?.stop()
        }
    }

    /// 新会话代号：随机十六进制，不含制表符（它会被写进存档文件的行尾）。
    private func newSessionCode() -> String {
        let hex = UUID().uuidString.replacingOccurrences(of: "-", with: "")
        return String(hex.prefix(12))
    }

    // MARK: - 云端档（Groq）

    /// 云端的事实：Keychain 里有 key、用户开了开关。
    ///
    /// 翻译能不能用这里不判断——要起 TranslationSession 才知道，
    /// 由 `prepareCloudTier` 准备好之后再来改。
    /// key 本身不进来：这里只带一个版本号（key 变了版本号就变）。
    private func readCloudAvailability() -> CloudAvailability {
        let key = SharedKeychain.readGroqKey()
        let translationAvailable = stateQueue.sync { cloudTranslationAvailable }
        return CloudAvailability(
            hasKey: key != nil,
            enabled: SharedKeychain.readCloudEnabled(),
            translationAvailable: translationAvailable,
            keyRevision: key?.hashValue ?? 0
        )
    }

    /// 每 5 秒看一眼 Keychain：用户可能在主 App 里改了开关或清了 key。
    /// 事实没变时状态机不会产生任何动作，这里只是白跑一次读。
    private func refreshCloudAvailability() {
        guard engineMode == "auto" else { return }
        updateCloudAvailability(readCloudAvailability())
    }

    /// 云端这一档的前置条件：云端只给原文，译文要用本机 TranslationSession。
    ///
    /// 翻译用不了（iOS < 26、系统翻译没装、准备失败）时这一档直接不参与路由：
    /// 状态机那边按「开关关」处理（switch_off），规则仍在 Routing.swift 里。
    private func prepareCloudTier() {
        guard #available(iOS 26.0, *) else { return }
        let availability = readCloudAvailability()
        guard availability.hasKey, availability.enabled else { return }
        let token = newSessionCode()
        let engine = LocalEngine(
            mode: .translateOnly,
            onFinal: { _, _, _ in },
            onTranslation: { [weak self] id, text in
                self?.handleCloudTranslation(id: id, text: text, token: token)
            },
            onSentenceSettled: { _ in },
            onStatus: { [weak self] status in
                self?.handleCloudTranslatorStatus(status, token: token)
            }
        )
        let shouldStart = stateQueue.sync { () -> Bool in
            guard cloudTranslator == nil else { return false }
            cloudTranslator = engine
            cloudTranslatorToken = token
            return true
        }
        if shouldStart {
            Task {
                await engine.start()
            }
        }
    }

    /// 只翻译引擎的状态：准备好了才让云端这一档参与路由。
    private func handleCloudTranslatorStatus(_ status: String, token: String) {
        let availabilityChanged = stateQueue.sync { () -> Bool in
            guard broadcastActive,
                  cloudTranslator != nil,
                  cloudTranslatorToken == token else { return false }
            switch status {
            case "tx_ready":
                cloudTranslationAvailable = true
                return true
            case "tx_unavailable", "tx_prepare_failed":
                cloudTranslationAvailable = false
                return true
            default:
                return false
            }
        }
        guard availabilityChanged else { return }
        updateCloudAvailability(readCloudAvailability())
    }

    /// 把云端的事实喂给路由状态机（key/开关变了要在这里反映出来）。
    private func updateCloudAvailability(_ availability: CloudAvailability) {
        // key 换了：客户端要换（Authorization 头里带的是旧 key）。
        if let key = SharedKeychain.readGroqKey(), key.hashValue != cloudKeyRevision {
            rotateCloudClient(key: key)
        }
        let outcome = stateQueue.sync { () -> (AutoFallbackDecision?, Bool) in
            guard engineMode == "auto", var fallback = autoFallback else { return (nil, false) }
            let result = fallback.updateCloud(availability)
            autoFallback = fallback
            return (result, fallback.shouldCancelInflight)
        }
        // 开关关了 / key 被清了：在飞请求要作废，迟到的结果要丢掉。
        if outcome.1 {
            cloudClient?.cancelInflightRequests()
            // 作废的回执不再清 busy，这里推进代次时一并清掉，否则之后的段全被当成「在飞」丢掉。
            stateQueue.sync {
                cloudGeneration &+= 1
                cloudUploadBusy = false
            }
        }
        guard let decision = outcome.0 else { return }
        applyAutoDecision(decision)
    }

    /// 换 key：先作废旧 key 的在飞请求（推进代次 + 取消旧客户端），再建新客户端。
    /// 顺序不能反——先换的话取消会落到新客户端上，旧 key 的迟到 401 还能把新 key 打成无效。
    private func rotateCloudClient(key: String) {
        let old = cloudClient
        if HandoffRules.keyRotated(clientRevision: old == nil ? nil : cloudKeyRevision, keyRevision: key.hashValue) {
            stateQueue.sync {
                cloudGeneration &+= 1
                cloudUploadBusy = false
                cloudProbeUsed = false
            }
            old?.cancelInflightRequests()
        }
        cloudClient = CloudClient(key: key)
        cloudKeyRevision = key.hashValue
    }

    /// 进云端档：音频改喂云端分段器，译文走本机翻译。
    private func startCloudTier() {
        guard #available(iOS 26.0, *) else { return }
        guard let key = SharedKeychain.readGroqKey() else { return }
        let revision = key.hashValue
        if cloudClient == nil {
            cloudClient = CloudClient(key: key)
            cloudKeyRevision = revision
        } else if cloudKeyRevision != revision {
            rotateCloudClient(key: key)
        }
        if cloudLedger == nil {
            cloudLedger = QuotaLedger(url: Self.quotaLedgerURL())
        }
        if cloudSegmenter == nil {
            cloudSegmenter = CloudSegmenter()
        }
        // 本机识别不再需要：云端给原文，本机只做翻译。
        stopLocalEngine()
        stateQueue.sync {
            routingTier = .cloud
            cloudProbeUsed = false
        }
    }

    /// 退出云端档：在飞请求作废，分段器丢掉（回来时重新开始，不带着旧缓冲）。
    private func stopCloudTier() {
        stateQueue.sync {
            cloudGeneration &+= 1
            cloudUploadBusy = false
            cloudProbeUsed = false
        }
        cloudClient?.cancelInflightRequests()
        cloudSegmenter = nil
    }

    /// 额度账本放扩展自己的容器目录：扩展与主 App 没有共享容器（只有 Keychain 共享组）。
    private static func quotaLedgerURL() -> URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.urls(for: .documentDirectory, in: .userDomainMask).first
            ?? URL(fileURLWithPath: NSTemporaryDirectory())
        return base.appendingPathComponent("rs-quota-ledger.json")
    }

    /// 一帧 16k 单声道 s16le → 浮点样本（分段器要的是 -1…1 的幅度）。
    private static func floatSamples(_ pcm: Data) -> [Float] {
        let count = pcm.count / 2
        guard count > 0 else { return [] }
        var samples = [Float](repeating: 0, count: count)
        pcm.withUnsafeBytes { raw in
            let bytes = raw.bindMemory(to: UInt8.self)
            for index in 0..<count {
                let low = UInt16(bytes[index * 2])
                let high = UInt16(bytes[index * 2 + 1])
                let value = Int16(bitPattern: low | (high << 8))
                samples[index] = Float(value) / 32768.0
            }
        }
        return samples
    }

    /// 一帧音频喂给云端分段器；切出的段立刻排队上传。
    private func feedCloudFrame(_ frame: Data, start: Double) {
        guard let segmenter = cloudSegmenter else { return }
        let output = segmenter.append(samples: Self.floatSamples(frame), startSeconds: start)
        for segment in output.segments {
            uploadCloudSegment(segment)
        }
    }

    /// 传一段音频给 Groq。
    ///
    /// 同一时间只保留一段上传音频：广播扩展只有 ~50MB，排队会无界增长，
    /// 在飞期间新出的段直接丢（计数写在状态行里）。
    private func uploadCloudSegment(_ segment: EmittedCloudSegment) {
        let busy = stateQueue.sync { cloudUploadBusy }
        if busy {
            stateQueue.sync { cloudDroppedSegments += 1 }
            return
        }
        guard let client = cloudClient, let ledger = cloudLedger else { return }

        // probe 状态（冷却到期后的那一次）只发一段试探。
        let skip = stateQueue.sync { () -> Bool in
            guard autoFallback?.cloudState == .probe else { return false }
            if cloudProbeUsed { return true }
            cloudProbeUsed = true
            return false
        }
        if skip { return }

        // 额度记账：每请求按 max(10, 时长) 计费（规则在 QuotaLedger 里）。
        _ = try? ledger.recordRequest(duration: segment.duration)
        // 额度事件喂给路由状态机（90% 进冷却、80% 以下才恢复的滞回在 Routing.swift 里）。
        // 可信翻日刚成立且此前进过冷却时，routeEvent 返回 quota_reset_confirmed，
        // 直接原样应用，保留 type 让路由状态机按 <80% 滞回放行。
        let quotaEvent = ledger.routeEvent()
        let quotaDecision = stateQueue.sync { () -> AutoFallbackDecision? in
            guard var fallback = autoFallback else { return nil }
            let result = fallback.applyRouteEvent(quotaEvent)
            autoFallback = fallback
            return result
        }
        if let quotaDecision {
            applyAutoDecision(quotaDecision)
        }

        let generation = stateQueue.sync { cloudGeneration }
        let language = cloudLanguage
        stateQueue.sync {
            cloudUploadBusy = true
            cloudUploadedSegments += 1
        }
        Task { [weak self] in
            do {
                let result = try await client.transcribe(
                    pcm16Mono16k: segment.pcm16,
                    a0: segment.audioOrigin,
                    duration: segment.duration,
                    language: language,
                    segmentID: segment.id
                )
                self?.handleCloudSuccess(result, segment: segment, generation: generation)
            } catch let error as CloudClientError {
                self?.handleCloudFailure(error, generation: generation)
            } catch {
                self?.handleCloudFailure(.network("unknown"), generation: generation)
            }
        }
    }

    /// 云端回了结果：喂状态机 → 按 P5 覆盖本机句 → 上屏 → 本机翻译。
    private func handleCloudSuccess(
        _ result: CloudTranscriptionResult,
        segment: EmittedCloudSegment,
        generation: UInt64
    ) {
        // 先判作废：旧代次（换过 key / 关过开关）的结果不进路由状态机、不记额度、不上屏。
        let stale = stateQueue.sync { () -> Bool in
            let stale = HandoffRules.isStaleCloudResponse(requestGeneration: generation, currentGeneration: cloudGeneration)
            if !stale { cloudUploadBusy = false }
            return stale
        }
        guard !stale else { return }
        let decision = handleAutoEvent(.cloudSegmentSucceeded)
        // 响应头里的每日剩余请求数：只用来校正 24 小时的计数，不校正分钟窗口。
        var quotaHeaders: [AnyHashable: Any] = [:]
        if let remaining = result.remainingRequests {
            quotaHeaders["x-ratelimit-remaining-requests"] = NSNumber(value: remaining)
        }
        if let resetSeconds = result.resetRequestsS {
            quotaHeaders["x-ratelimit-reset-requests"] = "\(resetSeconds)s"
        }
        if !quotaHeaders.isEmpty {
            try? cloudLedger?.correctRemainingRequests(
                quotaHeaders,
                keyFingerprint: result.keyFingerprint,
                sentAt: result.sentAt.map { Date(timeIntervalSince1970: $0) }
            )
        }
        let text = result.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else {
            applyAutoDecision(decision)
            return
        }
        // Groq 的分段时间已经映回样本时钟；没有分段就用这一段语音的区间。
        let t0 = result.segments.first?.t0 ?? segment.speechA0
        let t1 = result.segments.last?.t1 ?? segment.speechA1
        showCloudLine(t0: t0, t1: t1, text: text)
        applyAutoDecision(decision)
    }

    /// 云端这一段失败：HTTP 状态码喂给状态机（401/429/5xx 各自的规则在 Routing.swift 里），
    /// 其余按段失败处理。
    private func handleCloudFailure(_ error: CloudClientError, generation: UInt64) {
        let stale = stateQueue.sync { () -> Bool in
            let stale = HandoffRules.isStaleCloudResponse(requestGeneration: generation, currentGeneration: cloudGeneration)
            // 旧代次的回执不能清 busy：那会放行新 key 的第二段并发上传。作废时 busy 已在推进代次处清掉。
            if !stale { cloudUploadBusy = false }
            return stale
        }
        guard !stale else { return }
        switch error {
        case .httpStatus(let status, let retryAfter, _, let quota):
            var quotaHeaders: [AnyHashable: Any] = [:]
            if let remaining = quota?.remainingRequests {
                quotaHeaders["x-ratelimit-remaining-requests"] = NSNumber(value: remaining)
            }
            if let resetSeconds = quota?.resetRequestsS {
                quotaHeaders["x-ratelimit-reset-requests"] = "\(resetSeconds)s"
            }
            if !quotaHeaders.isEmpty {
                try? cloudLedger?.correctRemainingRequests(
                    quotaHeaders,
                    keyFingerprint: quota?.keyFingerprint,
                    sentAt: quota?.sentAt.map { Date(timeIntervalSince1970: $0) }
                )
            }
            applyAutoDecision(handleAutoEvent(.cloudHTTPStatus(status, retryAfter: retryAfter)))
        case .cancelled:
            break
        case .network, .invalidResponse, .decodeFailed:
            applyAutoDecision(handleAutoEvent(.cloudSegmentFailed))
        }
    }

    /// 冷却期间轮询账本：可信翻日刚成立时把路由从额度冷却里放出来。
    /// 只有 cooldownQuota 才允许应用（quota_update 会清掉 429 的 retry-after，
    /// 限流冷却必须留给它自己的 tick）。
    /// G4: 先在 stateQueue 内确认当前是 cooldownQuota，再消费记录并投递 quota_reset_confirmed；
    /// 非 cooldownQuota 时不得消费记录。
    private func pollLedgerQuotaReset() {
        guard let ledger = cloudLedger else { return }
        let decision = stateQueue.sync { () -> AutoFallbackDecision? in
            guard var fallback = autoFallback, fallback.cloudState == .cooldownQuota else { return nil }
            // 确认在配额冷却中后，再消费恢复记录（内部会落盘 consumed=true）。
            guard let percent = ledger.takeConfirmedReset() else { return nil }
            let result = fallback.applyRouteEvent(
                RouteEvent(type: "quota_reset_confirmed", status: nil, retryAfter: nil, usagePercent: percent)
            )
            autoFallback = fallback
            return result
        }
        if let decision {
            applyAutoDecision(decision)
        }
    }

    /// 云端这一句上屏：按 P5 当远程行处理（来源 cloud:groq），覆盖同一时段的本机句。
    private func showCloudLine(t0: Double, t1: Double, text: String) {
        let lineID: Int = stateQueue.sync { () -> Int in
            let replaced = p5Store.applyNode(
                nodeID: Self.cloudNodeID,
                t0: t0,
                t1: t1,
                srcText: text,
                dstText: nil
            )
            for key in replaced {
                guard let identity = p5Identity.removeValue(forKey: key) else { continue }
                recentLines.removeAll { $0.session == identity.session && $0.id == identity.id }
            }
            let id = cloudNextLineID
            cloudNextLineID += 1
            recentLines.append(RecentLine(id: id, src: text, dst: "", session: cloudSessionCode))
            if recentLines.count > maxRecentLines {
                recentLines.removeFirst()
            }
            return id
        }
        writeLatestLines()
        // 译文用本机翻译：云端只给原文。
        let translator = stateQueue.sync { cloudTranslator }
        if #available(iOS 26.0, *) {
            (translator as? LocalEngine)?.translate(text, id: lineID)
        }
    }

    /// 云端那句的译文到了。
    private func handleCloudTranslation(id: Int, text: String, token: String) {
        let accepted = stateQueue.sync { () -> Bool in
            guard broadcastActive,
                  cloudTranslator != nil,
                  cloudTranslatorToken == token else { return false }
            translationsCount += 1
            if let index = RecentLineLookup.index(in: recentLines, session: cloudSessionCode, id: id) {
                recentLines[index].dst = text
            }
            return true
        }
        guard accepted else { return }
        writeLatestLines()
    }

    /// 本机是不是还有没收尾的句子（有句子在识别，或者译文还在路上）。
    /// 必须在 stateQueue 上读。
    private var localBusy: Bool {
        localSentenceActive || !pendingLocalSettles.isEmpty
    }

    @available(iOS 26.0, *)
    private func startLocalEngine() {
        // 自动回退时 converter 已经在跑节点链路：复用同一个实例，
        // 既避免和音频线程抢写，也保住重采样器的滤波状态。
        // 广播可能已经结束（节点 close / 解析失败的回调晚于 broadcastFinished 到达）：
        // 在锁内确认，结束了就什么都不做。broadcastActive 只由 broadcastStarted / broadcastFinished 写。
        let allowed = stateQueue.sync { HandoffRules.mayStartLocal(broadcastActive: broadcastActive) }
        guard allowed else { return }
        if converter == nil {
            converter = PCMConverter()
        }
        let sessionCode = newSessionCode()
        let engine = LocalEngine(
            onFinal: { [weak self] id, text, range in
                self?.handleLocalFinal(id: id, text: text, range: range, session: sessionCode)
            },
            onTranslation: { [weak self] id, text in
                self?.handleLocalTranslation(id: id, text: text, session: sessionCode)
            },
            onSentenceSettled: { [weak self] id in
                self?.handleLocalSettled(id: id, session: sessionCode)
            },
            onStatus: { [weak self] status in
                self?.handleLocalStatus(status, session: sessionCode)
            }
        )
        let transition = stateQueue.sync { () -> (Bool, AnyObject?) in
            guard HandoffRules.mayStartLocal(broadcastActive: broadcastActive) else { return (false, nil) }
            let oldEngine = localEngine
            // 只有显式「本机」模式才把节点就绪视为真；自动模式正在等节点，
            // 这里写 true 会让切回节点之前就把音频发给一个尚未 ready 的会话。
            readyReceived = engineMode == "local"
            framesSent = 0
            finalsCount = 0
            translationsCount = 0
            preReadyDropped = 0
            lastStatusCode = ""
            fatalErrorOccurred = false
            fatalErrorReason = ""
            recentLines = []
            routingTier = .local
            localSentenceActive = false
            pendingLocalSettles.removeAll()
            // 换引擎就是换会话：新的本机引擎的 id 又从 1 开始，
            // 不换代号的话会和上一个本机会话的同 id 句对上。
            localSessionCode = sessionCode
            // 本机句的时间要跟云端段对齐：记下这一刻的样本时钟。
            localClockOrigin = sampleClock
            // 引用和路由同时切换，音频线程不会看到本机档却还拿不到本机引擎。
            localEngine = engine
            return (true, oldEngine)
        }
        guard transition.0 else { return }
        if #available(iOS 26.0, *) {
            (transition.1 as? LocalEngine)?.stop()
        }
        Task {
            await engine.start()
        }
    }

    /// 迟到的本机回调不能污染新一轮：会话代号变了、引擎没了、或者广播结束了都丢弃。
    private func handleLocalFinal(id: Int, text: String, range: LocalSentenceRange?, session: String) {
        var display = false
        let accepted = stateQueue.sync { () -> Bool in
            guard broadcastActive, localEngine != nil, localSessionCode == session else { return false }
            finalsCount += 1
            localSentenceActive = false
            pendingLocalSettles.insert(id)
            // P5：本机行进仓库（带时间，云端行才能按 P5 覆盖它）。
            // 没有整句区间就只上屏、不进仓库：拿假时间覆盖别处的句子更糟。
            if let range {
                let storeKey = p5NextKey
                p5NextKey += 1
                let t0 = localClockOrigin + range.start
                let t1 = localClockOrigin + range.end
                p5Store.addLocal(key: storeKey, t0: t0, t1: t1, text: text)
                p5Identity[storeKey] = (session: session, id: id)
                // 已经被远端行盖住的本机句不再上屏（P5）。
                display = p5Store.lines.contains { $0.key == storeKey }
            } else {
                display = true
            }
            if display {
                recentLines.append(RecentLine(id: id, src: text, dst: "", session: session))
                if recentLines.count > maxRecentLines {
                    recentLines.removeFirst()
                }
            }
            return true
        }
        guard accepted, display else { return }
        writeLatestLines()
        // 这里不发 .localSentenceFinalized：译文还在路上。
        // 早一步通知决策器，节点会立刻接手、本机引擎被停掉，
        // 在途的翻译随之取消，用户看到的就是「最后一句只有原文」。
        // 等译文落地（或翻译失败）后由 handleLocalSettled 通知。
    }

    /// 本机这一句收尾了（译文落地或翻译失败）：这时才可以交给节点。
    private func handleLocalSettled(id: Int, session: String) {
        let shouldReport = stateQueue.sync { () -> Bool in
            guard broadcastActive, localEngine != nil, localSessionCode == session else { return false }
            guard pendingLocalSettles.remove(id) != nil else { return false }
            // 还有别的句子没收尾、或用户已经开始说下一句，就继续等：
            // 切早了下一句已喂进本机 analyzer 的音频会随引擎一起丢掉。下一句收尾时再判断。
            return HandoffRules.canHandBackToNode(
                pendingSettles: pendingLocalSettles,
                sentenceActive: localSentenceActive
            )
        }
        guard shouldReport else { return }
        applyAutoDecision(handleAutoEvent(.localSentenceFinalized))
    }

    private func handleLocalTranslation(id: Int, text: String, session: String) {
        let accepted = stateQueue.sync { () -> Bool in
            guard broadcastActive, localEngine != nil, localSessionCode == session else { return false }
            translationsCount += 1
            // 会话代号一起比：本机句与云端句的 id 是两套号段，只比 id 会串行。
            if let idx = RecentLineLookup.index(in: recentLines, session: session, id: id) {
                recentLines[idx].dst = text
            }
            return true
        }
        guard accepted else { return }
        writeLatestLines()
    }

    private func handleLocalStatus(_ status: String, session: String) {
        let accepted = stateQueue.sync { () -> Bool in
            guard broadcastActive, localEngine != nil, localSessionCode == session else { return false }
            lastStatusCode = status
            return true
        }
        guard accepted else { return }
        logger.info("Local engine status: \(status)")
        if ["asr_assets_missing", "asr_unsupported", "analyzer_format_failed", "asr_failed", "asr_result_failed"].contains(where: { status.hasPrefix($0) }) {
            if engineMode == "auto" {
                // 本机引擎自己坏了：停掉它、把音频交还给节点重连，别让音频继续喂给死引擎。
                applyAutoDecision(handleAutoEvent(.localAvailabilityChanged(false)))
            } else {
                let shouldFatal = stateQueue.sync { () -> Bool in
                    guard broadcastActive, localEngine != nil, localSessionCode == session else { return false }
                    fatalErrorOccurred = true
                    fatalErrorReason = "fatal:\(status)"
                    return true
                }
                guard shouldFatal else { return }
            }
        }
        writeStatus("last=\(status)")
    }

    /// 广播适配层传入的 App 音频。ReplayKit 的 sample type 过滤留在适配层，避免引擎依赖它。
    func ingest(sampleBuffer: CMSampleBuffer) {
        blockCount += 1
        guard let formatDesc = CMSampleBufferGetFormatDescription(sampleBuffer),
              let asbd = CMAudioFormatDescriptionGetStreamBasicDescription(formatDesc) else {
            return
        }
        recordInputFormat(asbd.pointee)
        processInput(rms: computeRMS(sampleBuffer: sampleBuffer, asbd: asbd.pointee)) { converter in
            converter.append(sampleBuffer)
        }
    }

    /// 麦克风采集传入的原始 PCM。与广播入口只在转换前不同，之后复用同一条路由链。
    func ingest(pcmBuffer: AVAudioPCMBuffer) {
        blockCount += 1
        recordInputFormat(pcmBuffer.format.streamDescription.pointee)
        processInput(rms: computeRMS(pcmBuffer: pcmBuffer)) { converter in
            converter.append(pcmBuffer: pcmBuffer)
        }
    }

    /// 初始化输入格式只做一次，保持原有状态行的统计口径。
    private func recordInputFormat(_ asbd: AudioStreamBasicDescription) {
        guard sampleRate == 0 else { return }
        sampleRate = asbd.mSampleRate
        channelCount = asbd.mChannelsPerFrame
        formatFlags = asbd.mFormatFlags
        isNonInterleaved = (formatFlags & kAudioFormatFlagIsNonInterleaved) != 0
        if asbd.mFormatID == kAudioFormatLinearPCM {
            if formatFlags & kAudioFormatFlagIsFloat != 0 {
                formatString = "float32"
            } else if formatFlags & kAudioFormatFlagIsBigEndian != 0 {
                formatString = "int16be"
            } else {
                formatString = "int16le"
            }
        } else {
            formatString = "other"
        }
    }

    /// 入口共用的状态行、自动模式 tick 和转换前闸门。
    private func processInput(rms: Float, frames: (PCMConverter) -> [Data]) {
        accumulatedRMS += rms
        rmsSampleCount += 1

        let now = CFAbsoluteTimeGetCurrent()
        if now - lastWriteTime >= 1.0 {
            accumulatedRMS = 0
            rmsSampleCount = 0
            lastWriteTime = now
        }
        if now - lastStatusWriteTime >= 5.0 {
            let avgRMS = rmsSampleCount > 0 ? accumulatedRMS / Float(rmsSampleCount) : 0
            let memMB = residentMemoryMB()
            let interleavedStr = isNonInterleaved ? "0" : "1"
            let state = stateQueue.sync {
                (readyReceived, framesSent, finalsCount, translationsCount, lastStatusCode, preReadyDropped, fatalErrorOccurred, fatalErrorReason, reconnectCount, session)
            }
            // drop 只报真正丢掉的帧：尚无会话时的帧，加上当前会话缓冲满后淘汰的旧帧。
            let droppedFrames = state.5 &+ (state.9?.overflowDroppedFrameCount ?? 0)
            let readyStr = state.0 ? "1" : "0"
            let tier = stateQueue.sync { routingTier }
            let localActive = tier == .local
            let engineStr: String
            if engineMode == "local" {
                engineStr = "local"
            } else if engineMode == "auto" {
                engineStr = tier.rawValue
            } else {
                engineStr = "node"
            }
            var status = "blocks=\(blockCount) src=\(source) rate=\(Int(sampleRate)) ch=\(channelCount) fmt=\(formatString) flags=\(formatFlags) ileave=\(interleavedStr) rms=\(String(format: "%.3f", avgRMS)) mem=\(memMB) engine=\(engineStr)"
            if engineMode != "local" && !localActive {
                status += " node=\(nodeName) ready=\(readyStr) sent=\(state.1) finals=\(state.2) tx=\(state.3) last=\(state.4.isEmpty ? "-" : state.4) drop=\(droppedFrames) reconn=\(state.8)"
            } else {
                status += " finals=\(state.2) tx=\(state.3) last=\(state.4.isEmpty ? "-" : state.4)"
            }
            if tier == .cloud {
                let cloudState = stateQueue.sync { (cloudUploadedSegments, cloudDroppedSegments) }
                status += " cloud=up:\(cloudState.0) drop:\(cloudState.1)"
            }
            if state.6, !state.7.isEmpty {
                status += " \(state.7)"
            }
            writeStatus(status)
            lastStatusWriteTime = now
            // 冷却到点 → 试探一次；顺带看一眼开关/key 是不是被用户改了。
            if engineMode == "auto" {
                handleAutoTick()
                refreshCloudAvailability()
                pollLedgerQuotaReset()
            }
        }

        // 三个档位共用一条样本时钟：云端段与本机句的时间要能对上（P5 的覆盖判定靠它）。
        let fatal = stateQueue.sync { fatalErrorOccurred }
        guard !fatal, let converter = converter else { return }
        distribute(frames: frames(converter))
    }

    /// 两种音源都走这里，避免日后麦克风和广播的路由口径分叉。
    private func distribute(frames: [Data]) {
        var batchStart = 0.0
        if !frames.isEmpty {
            batchStart = stateQueue.sync { () -> Double in
                let value = sampleClock
                sampleClock += Double(frames.count) * Self.frameSeconds
                return value
            }
        }
        let localRoute = stateQueue.sync { (routingTier, localEngine) }
        if engineMode == "local" || localRoute.0 == .local {
            if #available(iOS 26.0, *) {
                guard let engine = localRoute.1 as? LocalEngine else { return }
                if !frames.isEmpty {
                    // 节点 ready 时靠这个标记判断“本机还有一句没定稿”，
                    // 避免切回节点把半句截断。
                    stateQueue.sync { localSentenceActive = true }
                    for frame in frames {
                        engine.append(pcm16k: frame)
                    }
                }
            }
            return
        }

        // 云端档：音频喂分段器，段上传 Groq。
        if engineMode == "auto", localRoute.0 == .cloud {
            for (index, frame) in frames.enumerated() {
                feedCloudFrame(frame, start: batchStart + Double(index) * Self.frameSeconds)
            }
            return
        }

        // 自动档在本机/云端时已在上面 return，故节点 ready 后的缓冲只含真正走节点的音频，
        // 不会把本机已经处理过的语音再发一次。
        let currentSession = stateQueue.sync { session }
        guard let session = currentSession else {
            // 还没有会话：这些帧白转了，计入丢弃。
            stateQueue.sync { preReadyDropped &+= UInt64(frames.count) }
            return
        }

        for frame in frames {
            if session.send(pcm16k: frame) == .sent {
                stateQueue.sync { framesSent += 1 }
            }
        }
    }

    /// 在 stateQueue 内核对轮次、活动状态和档位；节点回调还要核对连接身份，重连后旧连接才不会越权。
    private func isCurrentNodeRun(_ generation: Int, sessionToken: UUID? = nil) -> Bool {
        stateQueue.sync {
            let matchesSession = sessionToken.map { nodeSessionToken == $0 } ?? true
            return matchesSession
                && runGate.isCurrent(generation)
                && broadcastActive
                && EngineRunGate.mayTouchNode(engineMode: engineMode)
        }
    }

    /// 令牌留给重连计划核对；事件则还必须有一个活着的会话，不能让已 close 的旧连接借旧令牌复活。
    private func isCurrentNodeSession(_ generation: Int, sessionToken: UUID) -> Bool {
        stateQueue.sync {
            nodeSessionToken == sessionToken
                && session != nil
                && runGate.isCurrent(generation)
                && broadcastActive
                && EngineRunGate.mayTouchNode(engineMode: engineMode)
        }
    }

    private func startPinnedSession(_ config: NodeConfig, generation: Int) {
        connectionQueue.async { [weak self] in
            guard let self else { return }
            // 解析/建连前先挡掉旧轮次，避免 finish 后排队的重连在下一轮复活。
            guard self.isCurrentNodeRun(generation) else { return }
            let pinnedURL: URL
            do {
                pinnedURL = try TailnetPin.pinned(config.sessionURL)
            } catch {
                let status: String
                if case TailnetPin.PinError.notTailnet = error {
                    status = "not_tailnet"
                } else {
                    status = "resolve_failed"
                }
                let shouldReport = self.stateQueue.sync { () -> Bool in
                    guard self.runGate.isCurrent(generation),
                          self.broadcastActive,
                          EngineRunGate.mayTouchNode(engineMode: self.engineMode) else {
                        return false
                    }
                    self.lastStatusCode = status
                    return true
                }
                guard shouldReport else { return }
                self.writeStatus("last=\(status)")
                // 解析失败多半是临时性的（还没连上 Tailscale、DNS 抖动），
                // 所以照旧按退避策略重连；不然节点这一路就永久断了
                // （本机还能顶上，但节点再也回不来，用户以为节点坏了）。
                self.scheduleReconnect(closeCode: 1006, generation: generation)
                if self.isCurrentNodeRun(generation), self.engineMode == "auto" {
                    // 节点这一轮没连上：交给决策器决定要不要落到本机。
                    self.applyAutoDecision(self.handleAutoEvent(.nodeFailed))
                }
                return
            }

            let sessionToken = UUID()
            let sessionCode = self.newSessionCode()
            let newSession = NodeSession(
                url: pinnedURL,
                token: config.token,
                src: config.src,
                dst: config.dst,
                onEvent: { [weak self] event in
                    self?.handleNodeEvent(
                        event,
                        generation: generation,
                        sessionToken: sessionToken,
                        sessionCode: sessionCode
                    )
                },
                onClose: { [weak self] code in
                    self?.handleNodeClose(code, generation: generation, sessionToken: sessionToken)
                },
                onLoading: { [weak self] elapsed in
                    // 节点冷启动：状态行显示「<节点> 正在加载模型…」。
                    self?.handleNodeLoading(generation: generation, sessionToken: sessionToken)
                }
            )
            let transition = self.stateQueue.sync { () -> (Bool, NodeSession?) in
                guard self.runGate.isCurrent(generation),
                      self.broadcastActive,
                      EngineRunGate.mayTouchNode(engineMode: self.engineMode) else {
                    return (false, nil)
                }
                let oldSession = self.session
                self.session = newSession
                self.nodeSessionToken = sessionToken
                // 新的节点会话：节点的句子 id 重新从 1 计数，
                // 换一个新代号，主 App 才不会把它当成旧会话的同一句。
                self.nodeSessionCode = sessionCode
                return (true, oldSession)
            }
            if transition.0 {
                // 先在锁内换掉身份，再在锁外关旧连接：它排队的迟到回调会被新身份拦住。
                transition.1?.close()
                let shouldStart = self.stateQueue.sync {
                    self.runGate.isCurrent(generation)
                        && self.broadcastActive
                        && self.nodeSessionToken == sessionToken
                }
                if shouldStart {
                    newSession.start()
                } else {
                    // finish 可能恰好在换会话后发生；先关掉 B，start 的关闭闸才不会重建连接。
                    newSession.close()
                }
            }
        }
    }

    /// 按退避策略安排一次重连。节点断开、Tailnet 解析失败都走这里。
    /// - Returns: 是否真的排上了重连（false = 放弃重连或广播已结束）。
    @discardableResult
    private func scheduleReconnect(closeCode: Int, generation: Int, sessionToken: UUID? = nil) -> Bool {
        let plan = stateQueue.sync { () -> (TimeInterval?, NodeConfig?) in
            let matchesSession = sessionToken.map { nodeSessionToken == $0 } ?? true
            guard matchesSession,
                  runGate.isCurrent(generation),
                  broadcastActive,
                  EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                return (nil, nil)
            }
            guard !fatalErrorOccurred else { return (nil, nil) }
            guard let delay = reconnectPolicy.next(closeCode: closeCode) else { return (nil, nil) }
            reconnectCount += 1
            return (delay, nodeConfig)
        }

        guard let delay = plan.0, let config = plan.1 else { return false }

        // 重连期间状态行仍显示未 ready；新会话会在自身 15 秒有界缓冲内保住这段音频。
        let markedPending = stateQueue.sync { () -> Bool in
            let matchesSession = sessionToken.map { nodeSessionToken == $0 } ?? true
            guard matchesSession,
                  runGate.isCurrent(generation),
                  broadcastActive,
                  EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                return false
            }
            readyReceived = false
            return true
        }
        guard markedPending else { return false }

        // 延迟后在 connectionQueue 上重新走 startPinnedSession（含 TailnetPin 校验）
        connectionQueue.asyncAfter(deadline: .now() + delay) { [weak self] in
            guard let self else { return }
            guard self.isCurrentNodeRun(generation, sessionToken: sessionToken) else { return }
            // startPinnedSession 只会投递到 connectionQueue；不要在 stateQueue 内再同步调用它，
            // 否则后续给回调加状态核对时会形成同队列死锁。
            self.startPinnedSession(config, generation: generation)
        }
        return true
    }

    private func handleNodeEvent(
        _ event: NodeEvent,
        generation: Int,
        sessionToken: UUID,
        sessionCode: String
    ) {
        guard isCurrentNodeSession(generation, sessionToken: sessionToken) else { return }
        switch event {
        case .ready(let cold):
            // 自动模式：把「本机是否还有句子没收尾」同步给决策器，再问要不要切回。
            // 没收尾包括「正在识别」和「译文还在路上」两种：
            // 只看正在识别的话，节点恰好在译文飞回来之前就绪，最后一句就只剩原文。
            if stateQueue.sync(execute: {
                runGate.isCurrent(generation) && nodeSessionToken == sessionToken && session != nil && localBusy
            }) {
                _ = handleAutoEvent(.localSentenceStarted)
            }
            guard isCurrentNodeSession(generation, sessionToken: sessionToken) else { return }
            let decision = handleAutoEvent(.nodeReady)
            let accepted = stateQueue.sync { () -> Bool in
                guard runGate.isCurrent(generation),
                      nodeSessionToken == sessionToken,
                      session != nil,
                      broadcastActive,
                      EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                    return false
                }
                readyReceived = true
                reconnectPolicy.reset()
                // 冷启动期间状态行写成了「<节点> 正在加载模型…」；就绪后不清掉会一直显示加载中。
                lastStatusCode = HandoffRules.statusAfterReady(
                    lastStatusCode,
                    loadingSuffix: DesignTokens.Text.nodeLoadingSuffix
                )
                return true
            }
            guard accepted else { return }
            logger.info("Node ready cold=\(cold)")
            applyAutoDecision(decision)
        case .final(let id, _, _, let text):
            let accepted = stateQueue.sync { () -> Bool in
                guard runGate.isCurrent(generation),
                      nodeSessionToken == sessionToken,
                      session != nil,
                      broadcastActive,
                      EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                    return false
                }
                finalsCount += 1
                recentLines.append(RecentLine(id: id, src: text, dst: "", session: sessionCode))
                if recentLines.count > maxRecentLines {
                    recentLines.removeFirst()
                }
                return true
            }
            guard accepted else { return }
            writeLatestLines()
        case .translation(let id, let text):
            let accepted = stateQueue.sync { () -> Bool in
                guard runGate.isCurrent(generation),
                      nodeSessionToken == sessionToken,
                      session != nil,
                      broadcastActive,
                      EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                    return false
                }
                translationsCount += 1
                // 节点前后两个会话的 id 都从 1 计：只比 id 会把译文写到旧会话那一行。
                if let idx = RecentLineLookup.index(in: recentLines, session: sessionCode, id: id) {
                    recentLines[idx].dst = text
                }
                return true
            }
            guard accepted else { return }
            writeLatestLines()
        case .status(let code, let text, _):
            let accepted = stateQueue.sync { () -> Bool in
                guard runGate.isCurrent(generation),
                      nodeSessionToken == sessionToken,
                      session != nil,
                      broadcastActive,
                      EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                    return false
                }
                if code == "loading" {
                // 冷启动：状态行显示「<节点> 正在加载模型…」，
                // 别让原始 code 把它顶掉（每 5 秒一条 loading 帧）。
                    lastStatusCode = AutoFallbackStatus.nodeLoading(nodeName.isEmpty ? "电脑节点" : nodeName)
                } else {
                    lastStatusCode = code
                }
                return true
            }
            guard accepted else { return }
            logger.info("Node status: \(code) \(text)")
            if NodeEvent.isFatal(code: code) {
                guard isCurrentNodeSession(generation, sessionToken: sessionToken) else { return }
                if engineMode == "auto" {
                    // 自动模式不把节点致命错误记成全局 fatal——那会连本机一起停掉。
                    applyAutoDecision(handleAutoEvent(.reconnectAbandoned))
                } else {
                    stateQueue.sync {
                        guard runGate.isCurrent(generation), nodeSessionToken == sessionToken, session != nil, broadcastActive else { return }
                        fatalErrorOccurred = true
                        fatalErrorReason = "fatal:\(code)"
                    }
                }
            }
        case .drained:
            break
        }
    }

    /// 节点冷启动（gateway 正在拉起 worker 加载权重）：状态行显示加载中。
    private func handleNodeLoading(generation: Int, sessionToken: UUID) {
        guard isCurrentNodeSession(generation, sessionToken: sessionToken) else { return }
        applyAutoDecision(handleAutoEvent(.nodeLoading))
    }

    private func handleNodeClose(_ code: Int, generation: Int, sessionToken: UUID) {
        // 广播已经结束就别再管这个 close 了：不发 .nodeFailed（那会把本机引擎重新拉起来）、
        // 不重连、不改状态。少了这道闸，广播结束瞬间的 close 事件会让本机引擎复活，
        // 而节点根本不存在——用户看到的是「字幕还在跑，但没有节点」。
        guard isCurrentNodeRun(generation, sessionToken: sessionToken) else {
            logger.info("Node closed after its run ended: \(code)")
            return
        }

        let displayCode = code == 1013 ? "busy" : "\(code)"
        let accepted = stateQueue.sync { () -> Bool in
            guard runGate.isCurrent(generation),
                  nodeSessionToken == sessionToken,
                  session != nil,
                  broadcastActive,
                  EngineRunGate.mayTouchNode(engineMode: engineMode) else {
                return false
            }
            lastStatusCode = "closed=\(displayCode)"
            // 令牌仍是这次重连的凭据；只退休可收事件/发帧的会话身份。
            session = nil
            return true
        }
        guard accepted else { return }
        logger.info("Node closed: \(code)")

        // 显式 node 和自动模式都要重连；显式 local 不碰节点。
        guard EngineRunGate.mayTouchNode(engineMode: engineMode) else { return }

        if engineMode == "auto" {
            let resetReady = stateQueue.sync { () -> Bool in
                guard runGate.isCurrent(generation), nodeSessionToken == sessionToken, broadcastActive else { return false }
                readyReceived = false
                return true
            }
            guard resetReady else { return }
            applyAutoDecision(handleAutoEvent(.nodeFailed))
        }

        // 由 NodeSession 通过 closeReason 还原过 1005→真实码
        let scheduled = scheduleReconnect(closeCode: code, generation: generation, sessionToken: sessionToken)
        if !scheduled, engineMode == "auto", isCurrentNodeRun(generation, sessionToken: sessionToken) {
            applyAutoDecision(handleAutoEvent(.reconnectAbandoned))
        }
    }

    private func computeRMS(sampleBuffer: CMSampleBuffer, asbd: AudioStreamBasicDescription) -> Float {
        guard let dataBuffer = CMSampleBufferGetDataBuffer(sampleBuffer) else { return 0 }

        var lengthAtOffset: Int = 0
        var totalLength: Int = 0
        var dataPointer: UnsafeMutablePointer<Int8>?
        CMBlockBufferGetDataPointer(dataBuffer, atOffset: 0, lengthAtOffsetOut: &lengthAtOffset, totalLengthOut: &totalLength, dataPointerOut: &dataPointer)
        guard lengthAtOffset > 0, let ptr = dataPointer else { return 0 }

        let bitsPerChannel = Int(asbd.mBitsPerChannel)
        let bytesPerSample = bitsPerChannel / 8
        let sampleCount = lengthAtOffset / bytesPerSample
        guard sampleCount > 0 else { return 0 }

        var sumSquares: Float = 0
        let formatID = asbd.mFormatID
        let flags = asbd.mFormatFlags
        let isBigEndian = (flags & kAudioFormatFlagIsBigEndian) != 0

        if formatID == kAudioFormatLinearPCM {
            if flags & kAudioFormatFlagIsFloat != 0 {
                let floatPtr = ptr.withMemoryRebound(to: Float.self, capacity: sampleCount) { $0 }
                for i in 0..<sampleCount {
                    sumSquares += floatPtr[i] * floatPtr[i]
                }
            } else {
                let int16Ptr = ptr.withMemoryRebound(to: Int16.self, capacity: sampleCount) { $0 }
                for i in 0..<sampleCount {
                    let sample: Int16
                    if isBigEndian {
                        sample = Int16(bigEndian: int16Ptr[i])
                    } else {
                        sample = int16Ptr[i]
                    }
                    let f = Float(sample) / 32768.0
                    sumSquares += f * f
                }
            }
        }

        let meanSquare = sumSquares / Float(sampleCount)
        return sqrt(meanSquare)
    }

    /// 麦克风没有 CMSampleBuffer；按实际 PCM 布局计算等价 RMS，状态行仍能反映输入音量。
    private func computeRMS(pcmBuffer: AVAudioPCMBuffer) -> Float {
        let frames = Int(pcmBuffer.frameLength)
        let channels = Int(pcmBuffer.format.channelCount)
        guard frames > 0, channels > 0 else { return 0 }

        var sumSquares: Float = 0
        var sampleCount = 0
        if pcmBuffer.format.commonFormat == .pcmFormatFloat32 {
            guard let pointers = pcmBuffer.floatChannelData else { return 0 }
            if pcmBuffer.format.isInterleaved {
                let samples = pointers[0]
                for index in 0..<(frames * channels) {
                    sumSquares += samples[index] * samples[index]
                }
                sampleCount = frames * channels
            } else {
                for channel in 0..<channels {
                    for frame in 0..<frames {
                        let sample = pointers[channel][frame]
                        sumSquares += sample * sample
                    }
                }
                sampleCount = frames * channels
            }
        } else if pcmBuffer.format.commonFormat == .pcmFormatInt16 {
            guard let pointers = pcmBuffer.int16ChannelData else { return 0 }
            if pcmBuffer.format.isInterleaved {
                let samples = pointers[0]
                for index in 0..<(frames * channels) {
                    let sample = Float(samples[index]) / 32768.0
                    sumSquares += sample * sample
                }
                sampleCount = frames * channels
            } else {
                for channel in 0..<channels {
                    for frame in 0..<frames {
                        let sample = Float(pointers[channel][frame]) / 32768.0
                        sumSquares += sample * sample
                    }
                }
                sampleCount = frames * channels
            }
        }
        guard sampleCount > 0 else { return 0 }
        return sqrt(sumSquares / Float(sampleCount))
    }

    private func residentMemoryMB() -> Int {
        var info = task_vm_info_data_t()
        var count = mach_msg_type_number_t(MemoryLayout<task_vm_info_data_t>.size / MemoryLayout<integer_t>.size)
        let result = withUnsafeMutablePointer(to: &info) { ptr in
            ptr.withMemoryRebound(to: integer_t.self, capacity: Int(count)) { ptr in
                task_info(mach_task_self_, task_flavor_t(TASK_VM_INFO), ptr, &count)
            }
        }
        guard result == KERN_SUCCESS else { return 0 }
        return Int(info.phys_footprint) / (1024 * 1024)
    }

    private func writeStatus(_ text: String) {
        // start/finish/错误也会写状态；所有行都带来源，主 App 才不会把自己刚写的短状态误判成旧版广播。
        let sourcedText = text.contains("src=") ? text : "\(text) src=\(source)"
        let success = SharedKeychain.writeExtStatus(sourcedText)
        if !success {
            logger.error("Failed to write status to keychain")
        }
        postDarwinNotification()
    }

    private func writeLatestLines() {
        let encoder = JSONEncoder()
        encoder.outputFormatting = []
        if let data = try? encoder.encode(recentLines),
           let json = String(data: data, encoding: .utf8) {
            if json != lastLinesJSON {
                lastLinesJSON = json
                let success = SharedKeychain.writeLatestLines(json)
                if !success {
                    logger.error("Failed to write latest_lines to keychain")
                }
                postLinesNotification()
            }
        }
    }

    private func postDarwinNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.tick" as CFString
        CFNotificationCenterPostNotification(center, CFNotificationName(name), nil, nil, true)
    }

    private func postLinesNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.lines" as CFString
        CFNotificationCenterPostNotification(center, CFNotificationName(name), nil, nil, true)
    }
}
