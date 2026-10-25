import Foundation

/// 主 App 与广播扩展共用状态行，却不能同时写入同一条字幕链路。
/// 这个判定保持纯函数，避免 Keychain 的读取时机或 UI 生命周期改变互斥结果。
enum CaptureArbiter {
    /// 扩展每 5 秒刷新状态；留 12 秒窗口可以容忍前后台调度延迟，又不会让旧状态永久占用麦克风。
    static let broadcastFreshness: TimeInterval = 12

    static func decide(
        extensionStatus: String?,
        extensionStatusUpdatedAt: Date?,
        now: Date,
        micIsRunning: Bool
    ) -> CaptureArbiterAction {
        guard isFreshBroadcast(
            extensionStatus: extensionStatus,
            extensionStatusUpdatedAt: extensionStatusUpdatedAt,
            now: now
        ) else {
            return .allowMicStart
        }
        return micIsRunning ? .stopMic : .rejectMicStart
    }

    private static func isFreshBroadcast(
        extensionStatus: String?,
        extensionStatusUpdatedAt: Date?,
        now: Date
    ) -> Bool {
        guard let extensionStatusUpdatedAt,
              now.timeIntervalSince(extensionStatusUpdatedAt) >= 0,
              now.timeIntervalSince(extensionStatusUpdatedAt) <= broadcastFreshness else {
            return false
        }

        // I8-A 之前的状态行没有 src；升级中的旧扩展宁可视作广播，不能让两种采集并发。
        guard let source = source(in: extensionStatus) else { return true }
        return source == "broadcast"
    }

    private static func source(in status: String?) -> String? {
        guard let status else { return nil }
        return status.split(whereSeparator: { $0 == " " })
            .first(where: { $0.hasPrefix("src=") })
            .map { String($0.dropFirst("src=".count)) }
    }
}

enum CaptureArbiterAction: Equatable {
    case allowMicStart
    case rejectMicStart
    case stopMic
}

/// 音频会话的目标配置不用依赖音频硬件，才能在命令行锁住 PiP 回退语义。
struct MicAudioSessionPlan: Equatable {
    let category: MicAudioSessionCategory
    let mode: MicAudioSessionMode
    let options: Set<MicAudioSessionOption>

    static func forMic(active: Bool) -> MicAudioSessionPlan {
        if active {
            return MicAudioSessionPlan(
                category: .playAndRecord,
                mode: .defaultMode,
                options: [.mixWithOthers, .defaultToSpeaker, .allowBluetoothHFP]
            )
        }
        // PiP 原本使用 playback + mixWithOthers；停止采集后必须精确还原，避免 PiP 被静音。
        return MicAudioSessionPlan(category: .playback, mode: .defaultMode, options: [.mixWithOthers])
    }
}

enum MicAudioSessionCategory: Equatable {
    case playback
    case playAndRecord
}

enum MicAudioSessionMode: Equatable {
    case defaultMode
}

enum MicAudioSessionOption: Hashable {
    case mixWithOthers
    case defaultToSpeaker
    case allowBluetoothHFP
}
