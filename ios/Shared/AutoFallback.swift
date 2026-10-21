import Foundation

/// 「自动」模式的动作。决策只在这里产生，SampleHandler 只负责执行（接线）。
enum AutoFallbackAction: Equatable {
    /// 什么都不用改：继续用当前引擎，或继续等重连。
    case keep
    /// 启动本机引擎接管音频；节点连接保持不动，等它恢复。
    case startLocal
    /// 停掉本机引擎、把音频切回已经 ready 的节点。
    case stopLocalAndSwitchToNode
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
}

/// 「自动」模式的纯决策结构体（RFC C1/C2）。
///
/// 规则：
/// - 节点失败且本机可用 → 启动本机，状态 `<节点名> 不可用，已用本机`；
/// - 节点恢复 ready → 若本机当前句还在进行中则先等它定稿，避免半句被截断；
///   没有进行中的句子就立刻切回；
/// - 本机不可用（iOS < 26）→ 不启本机，只让 ReconnectPolicy 继续重连并显示状态。
/// - 广播已结束 → 之后的事件一律不动作（迟到的节点事件不能把本机引擎重新拉起来）。
///
/// 它不碰网络、不启动引擎、不读 Keychain，所以能在 macOS 上用 swiftc 跑自测。
struct AutoFallback {
    var nodeName: String
    private(set) var localAvailable: Bool
    private(set) var usingLocal = false
    private(set) var sentenceInProgress = false
    private(set) var awaitingSentenceEnd = false
    /// 广播是否还在进行。广播结束后节点那边还会抛来迟到的 close/ready 事件，
    /// 那时既不该把本机引擎重新拉起来，也不该改状态行。
    private(set) var broadcastActive = true

    init(nodeName: String, localAvailable: Bool) {
        self.nodeName = nodeName
        self.localAvailable = localAvailable
    }

    /// 广播结束：之后的事件一律不再产生动作。
    mutating func broadcastEnded() {
        broadcastActive = false
    }

    private var displayName: String {
        nodeName.isEmpty ? "电脑节点" : nodeName
    }

    mutating func handle(_ event: AutoFallbackEvent) -> AutoFallbackDecision {
        // 广播已经结束：后面来的都是迟到的事件。
        // 少了这道闸，广播结束瞬间的节点 close 事件会把本机引擎重新拉起来，
        // 而节点根本连不上，用户看到的是「字幕还在跑但没有节点」。
        guard broadcastActive else {
            return AutoFallbackDecision(action: .keep, status: nil)
        }
        switch event {
        case .nodeReady:
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

        case .nodeFailed, .reconnectAbandoned:
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

        case .localAvailabilityChanged(let available):
            localAvailable = available
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

        case .localSentenceStarted:
            sentenceInProgress = true
            return AutoFallbackDecision(action: .keep, status: nil)

        case .localSentenceFinalized:
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
    }
}
