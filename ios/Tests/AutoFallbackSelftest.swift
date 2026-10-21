import Foundation

/// 自动回退决策自测（C1/C2）。只测纯结构体，不碰 Keychain / 网络 / 引擎。
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
}
