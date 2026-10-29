import Foundation

/// 「自动」模式的动作。决策只在这里产生，SampleHandler 只负责执行（接线）。
enum AutoFallbackAction: Equatable {
    /// 什么都不用改：继续用当前引擎，或继续等重连。
    case keep
    /// 启动本机引擎接管音频；节点连接保持不动，等它恢复。
    case startLocal
    /// 停掉本机引擎、把音频切回已经 ready 的节点。
    case stopLocalAndSwitchToNode
    /// 云端档接管：音频改喂云端分段器，本机只留翻译（云端只给原文）。
    case startCloud
    /// 停掉云端、把音频切回节点。
    case stopCloudAndSwitchToNode
    /// 停掉云端、启本机引擎接管音频。
    case stopCloudAndStartLocal
    /// 停掉云端，但没有档位能接手（既没节点也没本机）：只停上传并报状态。
    case stopCloud
}

/// 「自动」模式的输入事件。
enum AutoFallbackEvent: Equatable {
    case nodeReady
    case nodeFailed
    /// ReconnectPolicy 返回 nil（协议错误 / 重连放弃）。
    case reconnectAbandoned
    case localAvailabilityChanged(Bool)
    /// 本机引擎收到了新音频、当前句还没定稿。节点 ready 要靠它判断“等本句结束”。
    case localSentenceStarted
    case localSentenceFinalized
    /// 节点冷启动中（gateway 正在拉起 worker 加载权重）。
    case nodeLoading
    /// 云端一段上传成功（HTTP 200）。
    case cloudSegmentSucceeded
    /// 云端一段上传失败（网络错误 / 超时）。
    case cloudSegmentFailed
    /// 云端回了 HTTP 状态码：401/403 → key 失效，429 → 限流冷却，5xx → 段失败。
    case cloudHTTPStatus(Int, retryAfter: Int?)
    /// 冷却到期：路由状态机进 probe，下一段只发一次试探。
    case cloudCooldownExpired
}

/// 云端这一档的可用性事实。
///
/// 由上层注入（读 Keychain、查系统翻译能力），AutoFallback 自己不读 Keychain、
/// 不查系统版本——它只把「事实」翻译成 `Routing.swift` 里的事件，
/// 档位规则一律由那个状态机决定。
struct CloudAvailability: Equatable {
    /// Keychain 里有 Groq key。
    var hasKey: Bool
    /// 用户打开的云端开关。
    var enabled: Bool
    /// 本机能翻译（iOS 26+ 且系统翻译已安装）。
    ///
    /// 云端只给原文，译文要用本机 TranslationSession；翻译用不了时这一档直接不参与路由。
    var translationAvailable: Bool
    /// key 的版本号：调用方在 key 内容变化时换一个新值。
    /// 这里只比版本、不碰 key 本身（key 不进结构体、不进日志、不进状态行）。
    var keyRevision: Int

    static let unavailable = CloudAvailability(
        hasKey: false,
        enabled: false,
        translationAvailable: false,
        keyRevision: 0
    )

    /// 路由意义上的「云端可用」：key + 开关 + 本机能翻译。
    var usable: Bool { hasKey && enabled && translationAvailable }
}

struct AutoFallbackDecision: Equatable {
    let action: AutoFallbackAction
    /// 要显示在状态行里的文案；nil 表示这次不改状态。
    let status: String?
}

/// 状态文案：全部取自设计令牌（spec 010 C3），代码里不写死中文。
/// 令牌是「后缀/前缀」形态，节点名拼在前面，这样同一句文案四端一致。
enum AutoFallbackStatus {
    static func connected(_ nodeName: String) -> String {
        nodeName + DesignTokens.Text.nodeConnectedSuffix
    }

    static func nodeUnavailableLocal(_ nodeName: String) -> String {
        nodeName + DesignTokens.Text.nodeUnavailableSuffix
    }

    static func nodeUnavailableNoLocal(_ nodeName: String) -> String {
        nodeName + DesignTokens.Text.nodeUnavailableReconnectingSuffix
    }

    static func waitingSentenceEnd(_ nodeName: String) -> String {
        nodeName + DesignTokens.Text.nodeRecoveredWaitingSuffix
    }

    static func switchedBack(_ nodeName: String) -> String {
        nodeName + DesignTokens.Text.nodeRecoveredSwitchedSuffix
    }

    static func localUnavailable(_ nodeName: String) -> String {
        DesignTokens.Text.localUnavailableReconnectingPrefix + nodeName
    }

    /// 节点冷启动：`<节点名> 正在加载模型…`。
    static func nodeLoading(_ nodeName: String) -> String {
        nodeName + DesignTokens.Text.nodeLoadingSuffix
    }

    /// 云端档状态行。
    static var cloud: String { DesignTokens.Text.statusCloud }

    /// key 失效（401/403）后按住不放，直到用户换 key。
    static var cloudKeyInvalid: String { DesignTokens.Text.statusCloudKeyInvalid }

    /// 限流冷却期间：当前档位标签 + 冷却后缀。
    static func cloudCooldown(_ modeLabel: String) -> String {
        modeLabel + DesignTokens.Text.statusCloudCooldownSuffix
    }

    /// 档位标签（与桌面端 `route_label` 同口径）：
    /// 云端 → 「云端·Groq」，节点 → 「混合·<节点名>」，本机 → 「本机」。
    /// 限流冷却时再拼一段冷却后缀。
    static func modeLabel(tier: ProviderTier, nodeName: String) -> String {
        switch tier {
        case .cloud:
            return DesignTokens.Text.statusCloud
        case .node:
            return DesignTokens.Text.statusHybridPrefix + nodeName
        case .local:
            return DesignTokens.Text.statusLocal
        }
    }
}

/// 「自动」模式的纯决策结构体（RFC C1/C2，本轮扩到三级）。
///
/// 规则：
/// - 档位（云端 / 节点 / 本机）一律问 `Routing.swift` 的 `CloudRouteContext`，
///   这里不另写一套「有 key 就云端」之类的判断——四端共用同一个状态机，规则才一致；
/// - 节点失败且本机可用 → 启动本机，状态 `<节点名> 不可用，已用本机`；
/// - 节点恢复 ready → 若本机当前句还在进行中则先等它定稿，避免半句被截断；
///   没有进行中的句子就立刻切回；
/// - 本机不可用（iOS < 26）→ 不启本机，只让 ReconnectPolicy 继续重连并显示状态；
/// - 广播已结束 → 之后的事件一律不动作（迟到的节点事件不能把本机引擎重新拉起来）。
///
/// 它不碰网络、不启动引擎、不读 Keychain，所以能在 macOS 上用 swiftc 跑自测。
struct AutoFallback {
    var nodeName: String
    private(set) var localAvailable: Bool
    private(set) var usingLocal = false
    /// 当前是不是云端档在跑（云端接管时本机引擎只做翻译，不做识别）。
    private(set) var usingCloud = false
    private(set) var sentenceInProgress = false
    private(set) var awaitingSentenceEnd = false
    /// 广播是否还在进行。广播结束后节点那边还会抛来迟到的 close/ready 事件，
    /// 那时既不该把本机引擎重新拉起来，也不该改状态行。
    private(set) var broadcastActive = true

    /// 路由状态机：云端 / 节点 / 本机三级的唯一真相源。
    /// 档位选择只问它（`tier()`），冷却、退避、key 失效的规则也都在它那里。
    private var route: CloudRouteContext
    /// 云端这一档的事实快照（key/开关/本机能不能翻译）。
    private var cloud: CloudAvailability
    /// 冷却到点的时间。nil = 现在不在冷却里。
    private var cooldownDeadline: Date?
    private let now: () -> Date

    init(
        nodeName: String,
        localAvailable: Bool,
        cloud: CloudAvailability = .unavailable,
        now: @escaping () -> Date = Date.init
    ) {
        self.nodeName = nodeName
        self.localAvailable = localAvailable
        self.cloud = cloud
        self.now = now
        // 翻译用不了时，对路由来说云端就是「关着的」：开关关掉的状态机分支
        // （switch_off）会走同一套规则，这里不另写判断。
        let effective = cloud.enabled && cloud.translationAvailable
        self.route = CloudRouteContext(RouteInitial(
            hasKey: cloud.hasKey,
            cloudEnabled: effective,
            cloudState: (effective && cloud.hasKey) ? .available : .off,
            nodeEnabled: false,
            nodeHealthy: false,
            nodeIsSelf: false,
            consecutiveFailures: 0,
            backoffIndex: 0,
            keyInvalid: false
        ))
    }

    /// 广播结束：之后的事件一律不再产生动作。
    mutating func broadcastEnded() {
        broadcastActive = false
    }

    private var displayName: String {
        nodeName.isEmpty ? "电脑节点" : nodeName
    }

    // MARK: - 状态查询（给 SampleHandler 用）

    /// 当前档位。只问路由状态机。
    var tier: ProviderTier { route.tier() }

    /// 云端状态机的当前状态（probe 时下一段只发一次试探）。
    var cloudState: CloudState { route.cloudState }

    /// 开关/key 变了：在飞请求要作废（判断由路由状态机给）。
    var shouldCancelInflight: Bool { route.cancelInflight }

    /// 迟到的响应要丢掉（开关关了 / key 被清了）。
    var shouldDiscardResponse: Bool { route.discardResponse }

    /// 冷却还剩几秒（0 = 不在冷却）。给状态行/日志用。
    var cooldownSecondsRemaining: Int {
        guard let deadline = cooldownDeadline else { return 0 }
        let remaining = deadline.timeIntervalSince(now())
        return remaining > 0 ? Int(remaining.rounded(.up)) : 0
    }

    // MARK: - 事件

    mutating func handle(_ event: AutoFallbackEvent) -> AutoFallbackDecision {
        // 广播已经结束：后面来的都是迟到的事件。
        // 少了这道闸，广播结束瞬间的节点 close 事件会把本机引擎重新拉起来，
        // 而节点根本连不上，用户看到的是「字幕还在跑但没有节点」。
        guard broadcastActive else {
            return AutoFallbackDecision(action: .keep, status: nil)
        }
        switch event {
        case .nodeReady:
            return handleNodeReady()
        case .nodeFailed, .reconnectAbandoned:
            return handleNodeFailed()
        case .localAvailabilityChanged(let available):
            return handleLocalAvailabilityChanged(available)
        case .localSentenceStarted:
            sentenceInProgress = true
            return AutoFallbackDecision(action: .keep, status: nil)
        case .localSentenceFinalized:
            return handleLocalSentenceFinalized()
        case .nodeLoading:
            return handleNodeLoading()
        case .cloudSegmentSucceeded:
            return handleCloudSuccess()
        case .cloudSegmentFailed:
            return handleCloudFailure()
        case .cloudHTTPStatus(let status, let retryAfter):
            return handleCloudHTTPStatus(status, retryAfter: retryAfter)
        case .cloudCooldownExpired:
            return handleCooldownExpired()
        }
    }

    /// 广播开始时的初始决策：把当前档位落到执行方上。
    ///
    /// 云端可用就直接进云端档；否则保持现状（节点/本机由节点事件驱动）。
    /// 翻译能不能用是异步查的，所以这一句多半是 `.keep`，
    /// 查完再由 `updateCloud` 决定要不要切云端。
    mutating func startDecision() -> AutoFallbackDecision {
        if route.tier() == .cloud {
            usingCloud = true
            usingLocal = false
            awaitingSentenceEnd = false
            return AutoFallbackDecision(action: .startCloud, status: AutoFallbackStatus.cloud)
        }
        return AutoFallbackDecision(action: .keep, status: nil)
    }

    /// 冷却到点：路由状态机进 probe，档位回到云端，下一段只发一次试探。
    /// 调用方（SampleHandler）按秒调 `tick(now:)`，这里只判断到没到点。
    mutating func tick(now: Date) -> AutoFallbackDecision? {
        guard broadcastActive, let deadline = cooldownDeadline, now >= deadline else { return nil }
        return handleCooldownExpired()
    }

    /// 云端的事实变了（key 写了/清了、开关拨了、本机翻译可用性变了）。
    ///
    /// 事件的翻译顺序与 mac rslite 的 `refreshSwitch` 一致：
    /// 没 key → key_cleared；开关关 → switch_off；key 换了 → key_modified；
    /// 重新打开 → switch_on。规则本身在 `Routing.swift` 里，这里只做事实 → 事件。
    mutating func updateCloud(_ availability: CloudAvailability) -> AutoFallbackDecision? {
        let previous = cloud
        cloud = availability
        let effective = availability.enabled && availability.translationAvailable
        let event: String?
        if !availability.hasKey {
            event = (route.hasKey || route.cloudState != .off) ? "key_cleared" : nil
        } else if !effective {
            event = (route.cloudEnabled && route.cloudState != .off) ? "switch_off" : nil
        } else if !route.hasKey {
            // 之前没有 key、现在有了：switch_off 之后状态是 .off，
            // 只有 key_modified 会把 hasKey 置回 true。
            event = "key_modified"
        } else if route.cloudState == .off {
            event = "switch_on"
        } else if availability.keyRevision != previous.keyRevision {
            event = "key_modified"
        } else {
            event = nil
        }
        guard let event else { return nil }
        route.apply(RouteEvent(type: event, status: nil, retryAfter: nil, usagePercent: nil))
        noteCooldownDeadline()
        // 只有换档才动状态行：开关拨一下不该把节点的状态文案顶掉。
        return reconcileTier()
    }

    /// 额度账本的百分比喂给路由状态机（90% 进冷却、80% 以下才恢复的滞回在 Routing.swift 里）。
    mutating func applyQuotaUsage(_ usagePercent: Double) -> AutoFallbackDecision? {
        route.apply(RouteEvent(type: "quota_update", status: nil, retryAfter: nil, usagePercent: usagePercent))
        noteCooldownDeadline()
        return reconcileTier()
    }

    /// 周期轮询账本的共享入口：恢复证明只允许在额度冷却中消费（G4）。
    mutating func pollQuota(
        usage: () -> Double, confirmedReset: () -> Double?
    ) -> AutoFallbackDecision? {
        if cloudState != .cooldownQuota {
            // 耗尽额度必须先锁住路由，再让 tick 处理 429 到期；低用量不发 quota_update，避免清掉 retry-after。
            let percent = usage()
            guard percent >= 90 else { return nil }
            return applyQuotaUsage(percent)
        }
        guard let percent = confirmedReset() else {
            // 换 key 丢弃证明后仍能靠完整 24h 用量自然老化恢复，保留 90/80 滞回。
            return applyQuotaUsage(usage())
        }
        return applyRouteEvent(RouteEvent(
            type: "quota_reset_confirmed", status: nil, retryAfter: nil, usagePercent: percent
        ))
    }

    /// 原样应用路由事件（保留 type，如 quota_reset_confirmed）。
    /// 由 QuotaLedger.routeEvent() 产生，SubtitleEngine 在 stateQueue 内调用。
    mutating func applyRouteEvent(_ event: RouteEvent) -> AutoFallbackDecision? {
        route.apply(event)
        noteCooldownDeadline()
        return reconcileTier()
    }

    /// 节点配没配对。配对了才可能选节点档。
    mutating func setNodeEnabled(_ enabled: Bool) {
        route.nodeEnabled = enabled
    }

    // MARK: - 节点 / 本机（沿用 C1/C2 的行为）

    private mutating func handleNodeReady() -> AutoFallbackDecision {
        route.nodeEnabled = true
        route.nodeHealthy = true
        if usingCloud {
            // 云端占着状态行：节点就绪不抢状态（云端一失败节点随时能接手）。
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
        guard usingLocal else {
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.connected(displayName))
        }
        guard sentenceInProgress else {
            usingLocal = false
            awaitingSentenceEnd = false
            return AutoFallbackDecision(
                action: .stopLocalAndSwitchToNode,
                status: AutoFallbackStatus.switchedBack(displayName)
            )
        }
        // 本机正在说一句：先别切，等这一句定稿。期间节点连接保持 ready。
        awaitingSentenceEnd = true
        return AutoFallbackDecision(
            action: .keep,
            status: AutoFallbackStatus.waitingSentenceEnd(displayName)
        )
    }

    private mutating func handleNodeFailed() -> AutoFallbackDecision {
        route.nodeHealthy = false
        if usingCloud {
            // 云端档与节点健康无关：节点掉了不影响云端出字。
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
        awaitingSentenceEnd = false
        guard localAvailable else {
            return AutoFallbackDecision(
                action: .keep,
                status: AutoFallbackStatus.nodeUnavailableNoLocal(displayName)
            )
        }
        guard !usingLocal else {
            return AutoFallbackDecision(
                action: .keep,
                status: AutoFallbackStatus.nodeUnavailableLocal(displayName)
            )
        }
        usingLocal = true
        return AutoFallbackDecision(
            action: .startLocal,
            status: AutoFallbackStatus.nodeUnavailableLocal(displayName)
        )
    }

    private mutating func handleLocalAvailabilityChanged(_ available: Bool) -> AutoFallbackDecision {
        localAvailable = available
        if usingCloud && !available {
            // 本机翻译没了 → 云端这一档出不了双语，按路由状态机的规则把云端关掉。
            route.apply(RouteEvent(type: "switch_off", status: nil, retryAfter: nil, usagePercent: nil))
            noteCooldownDeadline()
            let decision = fallbackFromCloud()
            return decision
        }
        guard !available, usingLocal else {
            return AutoFallbackDecision(action: .keep, status: nil)
        }
        usingLocal = false
        sentenceInProgress = false
        awaitingSentenceEnd = false
        return AutoFallbackDecision(
            action: .stopLocalAndSwitchToNode,
            status: AutoFallbackStatus.localUnavailable(displayName)
        )
    }

    private mutating func handleLocalSentenceFinalized() -> AutoFallbackDecision {
        sentenceInProgress = false
        guard awaitingSentenceEnd else {
            return AutoFallbackDecision(action: .keep, status: nil)
        }
        awaitingSentenceEnd = false
        usingLocal = false
        return AutoFallbackDecision(
            action: .stopLocalAndSwitchToNode,
            status: AutoFallbackStatus.switchedBack(displayName)
        )
    }

    private mutating func handleNodeLoading() -> AutoFallbackDecision {
        // 冷启动期间节点还没 ready，路由状态机里它不算健康。
        route.nodeHealthy = false
        if usingCloud {
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
        return AutoFallbackDecision(
            action: .keep,
            status: AutoFallbackStatus.nodeLoading(displayName)
        )
    }

    // MARK: - 云端

    private mutating func handleCloudSuccess() -> AutoFallbackDecision {
        guard usingCloud else {
            // 云端这一档已经不在了（开关关了 / key 被清了）：迟到的结果不上屏。
            // 状态行仍按当前状态报（key 失效是按住不放的状态，用户得看见）。
            return AutoFallbackDecision(action: .keep, status: currentStatus())
        }
        route.apply(RouteEvent(type: "segment_success", status: nil, retryAfter: nil, usagePercent: nil))
        noteCooldownDeadline()
        if route.tier() == .cloud {
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
        return fallbackFromCloud()
    }

    private mutating func handleCloudFailure() -> AutoFallbackDecision {
        guard usingCloud else {
            return AutoFallbackDecision(action: .keep, status: currentStatus())
        }
        route.apply(RouteEvent(type: "segment_failure", status: nil, retryAfter: nil, usagePercent: nil))
        noteCooldownDeadline()
        if route.tier() == .cloud {
            // 连续失败还没到退避门槛：留在云端继续试（Routing.swift 的 keep_local）。
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
        return fallbackFromCloud()
    }

    private mutating func handleCloudHTTPStatus(_ status: Int, retryAfter: Int?) -> AutoFallbackDecision {
        guard usingCloud else {
            return AutoFallbackDecision(action: .keep, status: currentStatus())
        }
        route.apply(RouteEvent(type: "http_status", status: status, retryAfter: retryAfter, usagePercent: nil))
        noteCooldownDeadline()
        if route.tier() == .cloud {
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
        return fallbackFromCloud()
    }

    private mutating func handleCooldownExpired() -> AutoFallbackDecision {
        cooldownDeadline = nil
        route.apply(RouteEvent(type: "cooldown_expired", status: nil, retryAfter: nil, usagePercent: nil))
        noteCooldownDeadline()
        if route.tier() == .cloud {
            // probe：档位回到云端，下一段只发一次试探（成败由 segment_success/failure 决定）。
            usingCloud = true
            usingLocal = false
            awaitingSentenceEnd = false
            return AutoFallbackDecision(action: .startCloud, status: AutoFallbackStatus.cloud)
        }
        return AutoFallbackDecision(action: .keep, status: currentStatus())
    }

    /// 云端这一档用不了了：按路由状态机给的档位退到节点或本机。
    private mutating func fallbackFromCloud() -> AutoFallbackDecision {
        usingCloud = false
        let status = currentStatus()
        switch route.tier() {
        case .node:
            return AutoFallbackDecision(action: .stopCloudAndSwitchToNode, status: status)
        case .local:
            guard localAvailable else {
                // 既没节点也没本机：没有能接手的档位，只把云端停掉。
                // 节点重连成功后会由 nodeReady 事件接管。
                let status = route.cloudState == .disabledInvalidKey
                    ? AutoFallbackStatus.cloudKeyInvalid
                    : AutoFallbackStatus.nodeUnavailableNoLocal(displayName)
                return AutoFallbackDecision(action: .stopCloud, status: status)
            }
            return AutoFallbackDecision(action: .stopCloudAndStartLocal, status: status)
        case .cloud:
            return AutoFallbackDecision(action: .keep, status: AutoFallbackStatus.cloud)
        }
    }

    /// 路由状态机给的档位变了没有：变了才换执行方。
    private mutating func reconcileTier() -> AutoFallbackDecision? {
        if route.tier() == .cloud {
            guard !usingCloud else { return nil }
            usingCloud = true
            usingLocal = false
            awaitingSentenceEnd = false
            return AutoFallbackDecision(action: .startCloud, status: AutoFallbackStatus.cloud)
        }
        guard usingCloud else { return nil }
        return fallbackFromCloud()
    }

    /// 当前状态行文案：key 失效优先，其次档位标签（限流冷却时带后缀）。
    private func currentStatus() -> String {
        if route.cloudState == .disabledInvalidKey {
            return AutoFallbackStatus.cloudKeyInvalid
        }
        let label = AutoFallbackStatus.modeLabel(tier: route.tier(), nodeName: displayName)
        if route.cloudState == .cooldownRateLimited {
            return AutoFallbackStatus.cloudCooldown(label)
        }
        return label
    }

    /// 冷却时长记在状态机里（apply 会把它清掉，所以每次事件后都要重新记一次）。
    private mutating func noteCooldownDeadline() {
        if let seconds = route.cooldownSeconds {
            cooldownDeadline = now().addingTimeInterval(TimeInterval(seconds))
        } else {
            cooldownDeadline = nil
        }
    }
}

/// 上传决策与请求提交共用的闸门；引擎在 applyDecision 内执行停云端等副作用。
enum CloudUploadGate {
    static func submit(
        applyDecision: () -> Void, cloudIsActive: () -> Bool, send: () -> Void
    ) {
        applyDecision()
        // 停云端会推进 generation；必须在副作用执行之后重查，才能阻止被错误标成新代次的请求。
        guard cloudIsActive() else { return }
        send()
    }
}
