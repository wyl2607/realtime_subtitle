import Foundation

/// 自动回退决策自测（C1/C2，本轮扩到三级：云端 → 节点 → 本机）。
/// 只测纯结构体，不碰 Keychain / 网络 / 引擎。
///
/// 状态文案的期望值全部由设计令牌拼出来（spec 010 C3）：文案改了这里不用改，
/// 但令牌漏了会让这里红——正是要钉住的那件事。
func runAutoFallbackSelftest(_ check: @escaping (Bool, String) -> Void) {
    // 节点失败 + 本机可用 → 启动本机，状态文案按卡片要求。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        let decision = fallback.handle(.nodeFailed)
        check(decision.action == .startLocal, "节点失败应启动本机")
        check(decision.status == "mini2" + DesignTokens.Text.nodeUnavailableSuffix, "节点失败状态文案（实际 \(decision.status ?? "nil")）")
        check(fallback.usingLocal, "节点失败后应标记正在用本机")

        // 本机已经在跑时节点再失败，不应该重复启动。
        let again = fallback.handle(.nodeFailed)
        check(again.action == .keep, "本机已在跑时节点再失败应保持")
    }

    // 节点就绪（没在用本机）→ 状态取「已连接」令牌。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        let decision = fallback.handle(.nodeReady)
        check(decision.action == .keep, "节点就绪且没在用本机时不应切换")
        check(decision.status == "mini2" + DesignTokens.Text.nodeConnectedSuffix, "节点就绪状态文案（实际 \(decision.status ?? "nil")）")
    }

    // 节点恢复 → 本机当前句还没定稿，先等；定稿后才切。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        _ = fallback.handle(.nodeFailed)
        _ = fallback.handle(.localSentenceStarted)
        let waiting = fallback.handle(.nodeReady)
        check(waiting.action == .keep, "本机句未定稿时节点 ready 应继续等（实际 \(waiting.action)）")
        check(waiting.status == "mini2" + DesignTokens.Text.nodeRecoveredWaitingSuffix, "等待切回状态文案（实际 \(waiting.status ?? "nil")）")
        check(fallback.usingLocal, "等待期间应继续用本机")
        check(fallback.awaitingSentenceEnd, "等待期间应记住待切回")

        let switched = fallback.handle(.localSentenceFinalized)
        check(switched.action == .stopLocalAndSwitchToNode, "本机句定稿后应切回节点")
        check(switched.status == "mini2" + DesignTokens.Text.nodeRecoveredSwitchedSuffix, "切回节点状态文案（实际 \(switched.status ?? "nil")）")
        check(!fallback.usingLocal, "切回后应标记不再用本机")
        check(!fallback.awaitingSentenceEnd, "切回后应清掉等待标记")
    }

    // 节点恢复时本机没有进行中的句子 → 立刻切回。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        _ = fallback.handle(.nodeFailed)
        let switched = fallback.handle(.nodeReady)
        check(switched.action == .stopLocalAndSwitchToNode, "没有进行中的句子应立刻切回节点")
        check(!fallback.usingLocal, "立刻切回后应标记不再用本机")
    }

    // 定稿事件在非等待状态下不产生动作。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        _ = fallback.handle(.nodeFailed)
        let decision = fallback.handle(.localSentenceFinalized)
        check(decision.action == .keep, "未在等待切回时定稿应保持")
    }

    // iOS < 26：本机引擎缺席，只保持重连并显示状态。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: false)
        let failed = fallback.handle(.nodeFailed)
        check(failed.action == .keep, "本机引擎缺席时节点失败不应启动本机")
        check(failed.status == "mini2" + DesignTokens.Text.nodeUnavailableReconnectingSuffix, "本机引擎缺席状态文案（实际 \(failed.status ?? "nil")）")
        check(!fallback.usingLocal, "本机引擎缺席时不能标记正在用本机")

        let abandoned = fallback.handle(.reconnectAbandoned)
        check(abandoned.action == .keep, "重连放弃且本机引擎缺席应保持")
        check(!fallback.usingLocal, "重连放弃且本机引擎缺席时仍不能启动本机")

        let ready = fallback.handle(.nodeReady)
        check(ready.action == .keep, "本机引擎缺席时节点 ready 不需要切换")
    }

    // 重连放弃但本机可用 → 仍然落到本机（与节点失败同一条出路）。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        let decision = fallback.handle(.reconnectAbandoned)
        check(decision.action == .startLocal, "重连放弃且本机可用应启动本机")
    }

    // 本机引擎中途失效 → 停本机、切回节点（节点是否 ready 由 SampleHandler 的重连负责）。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        _ = fallback.handle(.nodeFailed)
        let decision = fallback.handle(.localAvailabilityChanged(false))
        check(decision.action == .stopLocalAndSwitchToNode, "本机失效应停本机")
        check(!fallback.usingLocal, "本机失效后应清掉正在用本机")
        check(!fallback.localAvailable, "本机失效后应记录为失效")
        check(decision.status == DesignTokens.Text.localUnavailableReconnectingPrefix + "mini2", "本机失效状态文案（实际 \(decision.status ?? "nil")）")
    }

    // 广播结束后：迟到的节点事件既不该启动本机，也不该改状态行。
    // 少了这道闸，广播结束瞬间的 close 事件会把本机引擎重新拉起来。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        fallback.broadcastEnded()

        let failed = fallback.handle(.nodeFailed)
        check(failed.action == .keep, "广播结束后节点失败不应启动本机")
        check(failed.status == nil, "广播结束后节点失败不应改状态（实际 \(failed.status ?? "nil")）")
        check(!fallback.usingLocal, "广播结束后不应标记正在用本机")

        let abandoned = fallback.handle(.reconnectAbandoned)
        check(abandoned.action == .keep, "广播结束后重连放弃应保持")
        check(abandoned.status == nil, "广播结束后重连放弃不应改状态（实际 \(abandoned.status ?? "nil")）")

        let ready = fallback.handle(.nodeReady)
        check(ready.action == .keep, "广播结束后节点 ready 不应切换")
        check(ready.status == nil, "广播结束后节点 ready 不应改状态（实际 \(ready.status ?? "nil")）")
    }

    // 显式 node / local 模式的行为由 SharedKeychain 原样保留；
    // 这里只钉住模式解析：缺省和不认识的值才是 auto。
    check(EngineMode.normalized(nil) == .auto, "engine_mode 缺省应为 auto")
    check(EngineMode.normalized("auto") == .auto, "engine_mode=auto")
    check(EngineMode.normalized("node") == .node, "engine_mode=node 保持不变")
    check(EngineMode.normalized("local") == .local, "engine_mode=local 保持不变")
    check(EngineMode.normalized("garbage") == .auto, "不认识的 engine_mode 应退回 auto")

    // MARK: - 三级：云端 → 节点 → 本机

    // 有 key + 开关开 + 本机能翻译 → 云端优先。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        check(fallback.tier == .cloud, "有 key 且开关开、翻译可用时应选云端档（实际 \(fallback.tier)）")
        let started = fallback.startDecision()
        check(started.action == .startCloud, "云端档应接管音频（实际 \(started.action)）")
        check(started.status == DesignTokens.Text.statusCloud, "云端档状态文案（实际 \(started.status ?? "nil")）")
    }

    // 401 → key 无效（按住不放），并落到节点。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        _ = fallback.handle(.nodeReady)
        let decision = fallback.handle(.cloudHTTPStatus(401, retryAfter: nil))
        check(decision.status == DesignTokens.Text.statusCloudKeyInvalid, "401 状态文案应是「云端 key 无效」（实际 \(decision.status ?? "nil")）")
        check(decision.action == .stopCloudAndSwitchToNode, "401 后应落到节点（实际 \(decision.action)）")
        check(fallback.tier == .node, "401 后档位应退回节点（实际 \(fallback.tier)）")
        // key 失效后不能再被 429 带回云端（Routing.swift 的规则）。
        let late = fallback.handle(.cloudHTTPStatus(429, retryAfter: 30))
        check(fallback.tier == .node, "key 失效后迟到的 429 不该把档位带回云端（实际 \(fallback.tier)）")
        check(late.status == DesignTokens.Text.statusCloudKeyInvalid, "key 失效后状态仍是「云端 key 无效」（实际 \(late.status ?? "nil")）")
    }

    // 401 且节点不可用 → 再落本机。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        let decision = fallback.handle(.cloudHTTPStatus(401, retryAfter: nil))
        check(decision.action == .stopCloudAndStartLocal, "401 且节点不可用应落本机（实际 \(decision.action)）")
        check(decision.status == DesignTokens.Text.statusCloudKeyInvalid, "401 且节点不可用时状态仍是「云端 key 无效」（实际 \(decision.status ?? "nil")）")
    }

    // 401 且既没节点也没本机 → 停掉云端，没有档位能接手。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: false,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        let decision = fallback.handle(.cloudHTTPStatus(401, retryAfter: nil))
        check(decision.action == .stopCloud, "没有档位能接手时只停云端（实际 \(decision.action)）")
    }

    // 429 → 当前档位 + 冷却后缀；到期试探一次。
    do {
        var clock = Date()
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1),
            now: { clock }
        )
        _ = fallback.startDecision()
        _ = fallback.handle(.nodeReady)
        let limited = fallback.handle(.cloudHTTPStatus(429, retryAfter: 30))
        let expected = DesignTokens.Text.statusHybridPrefix + "mini2" + DesignTokens.Text.statusCloudCooldownSuffix
        check(limited.status == expected, "429 状态文案应是当前档位 + 冷却后缀（实际 \(limited.status ?? "nil")）")
        check(limited.action == .stopCloudAndSwitchToNode, "429 后应退到节点（实际 \(limited.action)）")
        check(fallback.tier == .node, "429 冷却期间档位应是节点（实际 \(fallback.tier)）")

        // 冷却没到点不动作。
        check(fallback.tick(now: clock.addingTimeInterval(10)) == nil, "冷却没到点不该动作")
        // 到点 → probe，档位回到云端。
        clock = clock.addingTimeInterval(31)
        let expired = fallback.tick(now: clock)
        check(expired?.action == .startCloud, "冷却到期应回云端试探（实际 \(String(describing: expired?.action))）")
        check(fallback.cloudState == .probe, "冷却到期后云端状态应是 probe（实际 \(fallback.cloudState)）")
        check(fallback.tier == .cloud, "冷却到期后档位应回到云端（实际 \(fallback.tier)）")
    }

    // 开关关 / 没有 key / 本机不能翻译 → 跳过云端。
    do {
        let off = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: false, translationAvailable: true, keyRevision: 1)
        )
        check(off.tier != .cloud, "开关关时不该选云端（实际 \(off.tier)）")

        let noKey = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: false, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        check(noKey.tier != .cloud, "没有 key 时不该选云端（实际 \(noKey.tier)）")

        let noTx = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: false, keyRevision: 1)
        )
        check(noTx.tier != .cloud, "本机不能翻译时不该选云端（实际 \(noTx.tier)）")
    }

    // 本机翻译后来可用了 → 云端档接管（switch_on 走 Routing.swift 的规则）。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: false, keyRevision: 1)
        )
        check(fallback.tier != .cloud, "翻译还不可用时先不选云端（实际 \(fallback.tier)）")
        let decision = fallback.updateCloud(
            CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        check(decision?.action == .startCloud, "翻译可用后应切到云端档（实际 \(String(describing: decision?.action))）")
        check(fallback.tier == .cloud, "翻译可用后档位应是云端（实际 \(fallback.tier)）")
    }

    // 用户清了 key → 云端停掉，退回节点。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        _ = fallback.handle(.nodeReady)
        let decision = fallback.updateCloud(
            CloudAvailability(hasKey: false, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        check(decision?.action == .stopCloudAndSwitchToNode, "清掉 key 后应退到节点（实际 \(String(describing: decision?.action))）")
        check(fallback.tier == .node, "清掉 key 后档位应是节点（实际 \(fallback.tier)）")
    }

    // 用户关了开关 → 云端停掉；在飞结果要作废。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        _ = fallback.handle(.nodeReady)
        let decision = fallback.updateCloud(
            CloudAvailability(hasKey: true, enabled: false, translationAvailable: true, keyRevision: 1)
        )
        check(decision?.action == .stopCloudAndSwitchToNode, "关开关后应退到节点（实际 \(String(describing: decision?.action))）")
        check(fallback.shouldCancelInflight, "关开关后在飞请求要作废")
        check(fallback.shouldDiscardResponse, "关开关后迟到的响应要丢掉")
    }

    // 节点冷启动 → 状态行显示「<节点> 正在加载模型…」。
    do {
        var fallback = AutoFallback(nodeName: "mini2", localAvailable: true)
        let decision = fallback.handle(.nodeLoading)
        check(decision.action == .keep, "节点加载中不该换档（实际 \(decision.action)）")
        check(decision.status == "mini2" + DesignTokens.Text.nodeLoadingSuffix, "节点加载中状态文案（实际 \(decision.status ?? "nil")）")
    }

    // 云端档占着状态行时，节点事件不抢状态行。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        let ready = fallback.handle(.nodeReady)
        check(ready.status == DesignTokens.Text.statusCloud, "云端档时节点就绪不抢状态行（实际 \(ready.status ?? "nil")）")
        let failed = fallback.handle(.nodeFailed)
        check(failed.status == DesignTokens.Text.statusCloud, "云端档时节点失败不抢状态行（实际 \(failed.status ?? "nil")）")
        check(fallback.tier == .cloud, "节点失败不影响云端档（实际 \(fallback.tier)）")
    }

    // 连续失败三次才退（Routing.swift 的退避门槛），前两次留在云端。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        let first = fallback.handle(.cloudSegmentFailed)
        check(first.action == .keep, "第一次失败还留在云端（实际 \(first.action)）")
        let second = fallback.handle(.cloudSegmentFailed)
        check(second.action == .keep, "第二次失败还留在云端（实际 \(second.action)）")
        _ = fallback.handle(.nodeReady)
        let third = fallback.handle(.cloudSegmentFailed)
        check(third.action == .stopCloudAndSwitchToNode, "第三次失败应退到节点（实际 \(third.action)）")
    }

    // G4: applyRouteEvent 保留 event type（quota_reset_confirmed），
    // 且只有在 cooldownQuota 时才消费恢复记录。
    do {
        var fallback = AutoFallback(
            nodeName: "mini2",
            localAvailable: true,
            cloud: CloudAvailability(hasKey: true, enabled: true, translationAvailable: true, keyRevision: 1)
        )
        _ = fallback.startDecision()
        _ = fallback.handle(.nodeReady)
        // 先用 quota_update 打入配额冷却
        let hotEvent = RouteEvent(type: "quota_update", status: nil, retryAfter: nil, usagePercent: 95)
        let hotDecision = fallback.applyRouteEvent(hotEvent)
        check(hotDecision != nil, "G4: quota_update ≥90% 应进入配额冷却")
        check(fallback.cloudState == .cooldownQuota, "G4: 状态机应进入 cooldownQuota")

        // 在 cooldownQuota 时，applyRouteEvent(quota_reset_confirmed) 应放行云端
        let resetEvent = RouteEvent(type: "quota_reset_confirmed", status: nil, retryAfter: nil, usagePercent: 50)
        let resetDecision = fallback.applyRouteEvent(resetEvent)
        check(resetDecision != nil, "G4: cooldownQuota 时 quota_reset_confirmed 应产生决策")
        check(fallback.cloudState == .available, "G4: quota_reset_confirmed <80% 应放行回云端")
        check(resetDecision?.action == .startCloud, "G4: 放行后动作应为 startCloud")

        // 不在 cooldownQuota 时，applyRouteEvent(quota_reset_confirmed) 不应改变状态
        let resetEvent2 = RouteEvent(type: "quota_reset_confirmed", status: nil, retryAfter: nil, usagePercent: 50)
        let resetDecision2 = fallback.applyRouteEvent(resetEvent2)
        check(resetDecision2 == nil, "G4: 非 cooldownQuota 时 quota_reset_confirmed 不应产生决策")
        check(fallback.cloudState == .available, "G4: 非冷却时状态不应改变")
    }
}
