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
                return MicCaptionIntentPolicy.decideStart(
                    micIsRunning: mic.isRunning,
                    extensionStatus: feed.status,
                    extensionStatusUpdatedAt: feed.lastStatusUpdate,
                    now: now
                )
            },
            startMic: { [weak self] in
                guard let self else { return }
                bootstrap()
                startMic()
            }
        )
    }
}
