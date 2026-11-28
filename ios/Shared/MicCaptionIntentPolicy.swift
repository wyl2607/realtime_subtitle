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

/// App Intents 与控制中心按钮共用的纯判定逻辑（不依赖 AppModel / UIKit / Audio）。
enum MicCaptionIntentPolicy {
    static let startMicScheme = "rsnode"
    static let startMicHost = "start-mic"

    /// 识别控制中心等入口打开主 App 的开始麦克风链接（`rsnode://start-mic`）。
    /// 忽略大小写、尾斜杠与附加 query 参数；拒绝其他 scheme、host 或子路径。
    static func isStartMicURL(_ url: URL) -> Bool {
        guard url.scheme?.lowercased() == startMicScheme,
              url.host?.lowercased() == startMicHost else {
            return false
        }
        return url.path.isEmpty || url.path == "/"
    }

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

/// 主 App 内 StartMicCaptionsIntent 的 Sendable 依赖注入桥接，
/// 避免 Intent 结构体直接持有非 Sendable 的主 App 状态对象。
struct MicCaptionIntentController: Sendable {
    let decideStart: @MainActor @Sendable (Date) -> MicCaptionStartDecision
    let startMic: @MainActor @Sendable () -> Void
}
