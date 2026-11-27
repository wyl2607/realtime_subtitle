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
        // 保守规则与时钟回拨防护：
        // 1. 读到旧格式无时间戳（extensionStatusUpdatedAt == nil）视为未知新鲜度，不阻塞麦克风，
        //    避免升级前遗留的旧状态永久锁死麦克风（同一安装包升级会结束旧扩展）。
        // 2. 时钟回拨产生未来时间戳（age < 0）直接视作无效，防止一条未来时间旧状态永久阻塞麦克风。
        guard let extensionStatusUpdatedAt else { return false }
        let age = now.timeIntervalSince(extensionStatusUpdatedAt)
        guard age >= 0, age <= broadcastFreshness else {
            return false
        }

        // 只认明确写着 src=broadcast 的状态行。扩展和主 App 同一个包发布，所有状态行都带 src；
        // 读不到 / 读到 "无"（锁屏时 Keychain 拒绝访问）绝不能当成广播——以前这么判，
        // 麦克风自己写状态失败后发的通知就把自己停了（2026-11-21 真机：锁屏 3 秒后录音停止）。
        // 广播结束时扩展最后写一行 "finished src=broadcast"：它是新鲜的，但广播已经停了，
        // 不能让用户停完广播后 12 秒内都开不了麦克风。
        guard extensionStatus?.hasPrefix("finished") != true else { return false }
        return source(in: extensionStatus) == "broadcast"
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
