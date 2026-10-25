import ReplayKit

/// ReplayKit 只负责把广播生命周期和 App 音频交给共享引擎。
/// 路由逻辑留在 SubtitleEngine，主 App 的麦克风模式才能复用而不链接 ReplayKit。
final class SampleHandler: RPBroadcastSampleHandler {
    private let engine = SubtitleEngine(source: "broadcast")

    override init() {
        super.init()
        // 终止广播是扩展专属 API；引擎通过回调表达故障，避免反向依赖 ReplayKit。
        engine.onFatalError = { [weak self] error in
            self?.finishBroadcastWithError(error)
        }
    }

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        engine.start()
    }

    override func broadcastFinished() {
        engine.finish()
    }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        // 只让广播 App 的声音进入引擎；麦克风入口由主 App 未来直接调用。
        guard sampleBufferType == .audioApp else { return }
        engine.ingest(sampleBuffer: sampleBuffer)
    }
}
