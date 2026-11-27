import Foundation

/// 快捷指令 / 控制中心启动与停止的纯状态判定自测：脱离 AppModel、AVAudioSession 与 AppIntents 框架，
/// 锁住重复触发忽略与广播互斥逻辑。
@main
struct MicCaptionIntentPolicySelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        let now = Date(timeIntervalSince1970: 2_000)
        let fresh = now.addingTimeInterval(-5)
        let stale = now.addingTimeInterval(-20)

        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: false,
                extensionStatus: nil,
                extensionStatusUpdatedAt: nil,
                now: now
            ) == .start,
            "空闲状态应允许启动麦克风字幕"
        )
        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: true,
                extensionStatus: nil,
                extensionStatusUpdatedAt: nil,
                now: now
            ) == .ignoreAlreadyRunning,
            "麦克风已在运行时重复触发开始应忽略"
        )
        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: false,
                extensionStatus: "blocks=3 src=broadcast",
                extensionStatusUpdatedAt: fresh,
                now: now
            ) == .blockedByBroadcast,
            "广播活跃窗口内应阻止启动麦克风字幕"
        )
        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: false,
                extensionStatus: "blocks=3 src=broadcast",
                extensionStatusUpdatedAt: stale,
                now: now
            ) == .start,
            "过期广播状态不应阻止启动麦克风字幕"
        )
        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: false,
                extensionStatus: "finished src=broadcast",
                extensionStatusUpdatedAt: fresh,
                now: now
            ) == .start,
            "广播刚结束（finished）应允许启动麦克风字幕"
        )
        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: false,
                extensionStatus: "blocks=3 src=mic",
                extensionStatusUpdatedAt: fresh,
                now: now
            ) == .start,
            "自身 src=mic 状态不应拦截启动"
        )
        check(
            MicCaptionIntentPolicy.decideStart(
                micIsRunning: true,
                extensionStatus: "blocks=3 src=broadcast",
                extensionStatusUpdatedAt: fresh,
                now: now
            ) == .ignoreAlreadyRunning,
            "麦克风已在运行时优先按重复触发忽略"
        )

        check(
            MicCaptionIntentPolicy.decideStop(micIsRunning: true) == .stop,
            "麦克风运行中应允许停止"
        )
        check(
            MicCaptionIntentPolicy.decideStop(micIsRunning: false) == .ignoreAlreadyStopped,
            "麦克风未运行时重复停止应忽略"
        )

        if failures.isEmpty {
            print("MIC CAPTION INTENT POLICY SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
