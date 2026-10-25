import Foundation

/// I8-B：互斥和音频会话目标都必须脱离 Keychain / AVAudioSession 测，
/// 这样旧状态行与 PiP 回退配置不会只靠真机才发现回归。
@main
struct CaptureArbiterSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        let now = Date(timeIntervalSince1970: 1_000)
        let fresh = now.addingTimeInterval(-11)
        let stale = now.addingTimeInterval(-13)

        check(
            CaptureArbiter.decide(
                extensionStatus: "blocks=1 src=broadcast",
                extensionStatusUpdatedAt: fresh,
                now: now,
                micIsRunning: false
            ) == .rejectMicStart,
            "新鲜 broadcast 状态行应拒绝启动麦克风"
        )
        check(
            CaptureArbiter.decide(
                extensionStatus: "blocks=1 src=broadcast",
                extensionStatusUpdatedAt: stale,
                now: now,
                micIsRunning: false
            ) == .allowMicStart,
            "过期 broadcast 状态行不应阻止麦克风"
        )
        check(
            CaptureArbiter.decide(
                extensionStatus: "blocks=1 src=mic",
                extensionStatusUpdatedAt: fresh,
                now: now,
                micIsRunning: false
            ) == .allowMicStart,
            "src=mic 不是广播占用"
        )
        check(
            CaptureArbiter.decide(
                extensionStatus: "blocks=1 src=broadcast",
                extensionStatusUpdatedAt: fresh,
                now: now,
                micIsRunning: true
            ) == .stopMic,
            "麦克风运行时收到新鲜 broadcast 状态应停止麦克风"
        )
        check(
            CaptureArbiter.decide(
                extensionStatus: "blocks=1 rate=48000",
                extensionStatusUpdatedAt: fresh,
                now: now,
                micIsRunning: false
            ) == .rejectMicStart,
            "旧状态行解析不到 src 时按 broadcast 处理"
        )

        let activePlan = MicAudioSessionPlan.forMic(active: true)
        check(activePlan.category == .playAndRecord, "麦克风运行时必须是 playAndRecord")
        check(activePlan.mode == .defaultMode, "麦克风运行时保持默认音频模式")
        check(activePlan.options == [.mixWithOthers, .defaultToSpeaker, .allowBluetoothHFP], "麦克风运行时的音频会话选项不应打断 PiP")

        let inactivePlan = MicAudioSessionPlan.forMic(active: false)
        check(inactivePlan.category == .playback, "麦克风停止后必须还原 PiP 的 playback 类别")
        check(inactivePlan.mode == .defaultMode, "麦克风停止后保持默认音频模式")
        check(inactivePlan.options == [.mixWithOthers], "麦克风停止后只保留 PiP 所需的混音选项")

        if failures.isEmpty {
            print("SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
