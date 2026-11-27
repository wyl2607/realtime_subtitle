import Foundation

/// 快捷指令 / 控制中心触发「开始麦克风字幕」时的纯判定结果。
enum MicCaptionStartDecision: Equatable, Sendable {
    /// 可以启动麦克风字幕。
    case start
    /// 麦克风已在采集，忽略重复触发。
    case ignoreAlreadyRunning
    /// 屏幕广播仍在活跃窗口内，拒绝并发启动麦克风。
    case blockedByBroadcast
}

/// 快捷指令触发「停止麦克风字幕」时的纯判定结果。
enum MicCaptionStopDecision: Equatable, Sendable {
    /// 麦克风正在运行，应该停止。
    case stop
    /// 麦克风未在运行，忽略重复触发。
    case ignoreAlreadyStopped
}

/// App Intents 与控制中心按钮共用的状态判定纯函数（不依赖 AppModel / UIKit / Audio）。
enum MicCaptionIntentPolicy {
    static func decideStart(
        micIsRunning: Bool,
        extensionStatus: String?,
        extensionStatusUpdatedAt: Date?,
        now: Date
    ) -> MicCaptionStartDecision {
        if micIsRunning {
            return .ignoreAlreadyRunning
        }
        let arbiter = CaptureArbiter.decide(
            extensionStatus: extensionStatus,
            extensionStatusUpdatedAt: extensionStatusUpdatedAt,
            now: now,
            micIsRunning: false
        )
        if arbiter == .rejectMicStart {
            return .blockedByBroadcast
        }
        return .start
    }

    static func decideStop(micIsRunning: Bool) -> MicCaptionStopDecision {
        micIsRunning ? .stop : .ignoreAlreadyStopped
    }
}

/// 跨 target（RSiOS + RSLiveActivity）共享的依赖注入桥接：
/// StartMicCaptionsIntent 只依赖这个 Sendable 类型，不直接引用主 App 才有的 AppModel。
struct MicCaptionIntentController: Sendable {
    let decideStart: @MainActor @Sendable (Date) -> MicCaptionStartDecision
    let startMic: @MainActor @Sendable () -> Void
}
