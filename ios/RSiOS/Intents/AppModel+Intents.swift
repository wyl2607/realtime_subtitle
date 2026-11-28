import Foundation

extension AppModel {
    /// AppModel 本体只有 `mic.stop()`；这里补一层封装并顺带同步锁屏实时活动，
    /// 确保在后台通过快捷指令/Siri 停止时实时活动也能立刻结束。
    func stopMic() {
        mic.stop()
        refreshLiveActivity()
    }

    func makeIntentController() -> MicCaptionIntentController {
        MicCaptionIntentController(
            decideStart: { [weak self] now in
                guard let self else { return .ignoreAlreadyRunning }
                return decideStartMic(now: now)
            },
            startMic: { [weak self] in
                guard let self else { return }
                bootstrapAndStartMic()
            }
        )
    }

    /// 响应控制中心等入口发来的 `rsnode://start-mic` 深链接；未命中返回 false 以便继续走普通 URL 处理。
    func handleStartMicOpenURL(_ url: URL, now: Date = Date()) -> Bool {
        guard MicCaptionIntentPolicy.isStartMicURL(url) else {
            return false
        }
        if decideStartMic(now: now) == .start {
            bootstrapAndStartMic()
        }
        return true
    }

    private func decideStartMic(now: Date) -> MicCaptionStartDecision {
        MicCaptionIntentPolicy.decideStart(
            micIsRunning: mic.isRunning,
            extensionStatus: feed.status,
            extensionStatusUpdatedAt: feed.lastStatusUpdate,
            now: now
        )
    }

    private func bootstrapAndStartMic() {
        bootstrap()
        startMic()
    }
}
