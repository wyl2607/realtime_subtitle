import AVFoundation
import Combine
import Foundation

/// 主 App 麦克风采集的可观察状态；错误详情只留给诊断，界面只显示设计令牌里的稳定文案。
enum MicCaptureState: Equatable {
    case idle
    case running
    case denied
    case blockedByBroadcast
    case error(String)
}

final class MicCapture: ObservableObject {
    @Published private(set) var state: MicCaptureState = .idle

    private let audioEngine = AVAudioEngine()
    private let subtitleEngine = SubtitleEngine(source: "mic")
    /// tap 在 Core Audio 的实时线程调用；转换和路由只能在这条队列，避免占用实时线程或与 stop 竞争。
    private let ingestQueue = DispatchQueue(label: "com.wyl2607.rsios.mic-ingest")
    private var notificationObservers: [NSObjectProtocol] = []
    private var isRequestingPermission = false
    private var shouldResumeAfterInterruption = false
    private var latestExtensionStatus: String?
    private var latestExtensionStatusUpdatedAt: Date?

    init() {
        let center = NotificationCenter.default
        notificationObservers = [
            center.addObserver(
                forName: AVAudioSession.interruptionNotification,
                object: AVAudioSession.sharedInstance(),
                queue: .main
            ) { [weak self] notification in
                self?.handleInterruption(notification)
            },
            center.addObserver(
                forName: AVAudioSession.routeChangeNotification,
                object: AVAudioSession.sharedInstance(),
                queue: .main
            ) { [weak self] notification in
                self?.handleRouteChange(notification)
            },
        ]
    }

    deinit {
        notificationObservers.forEach(NotificationCenter.default.removeObserver)
    }

    var isRunning: Bool {
        state == .running
    }

    func start(extensionStatus: String?, extensionStatusUpdatedAt: Date?) {
        guard !isRequestingPermission, !isRunning else { return }
        rememberExtensionStatus(extensionStatus, updatedAt: extensionStatusUpdatedAt)
        switch CaptureArbiter.decide(
            extensionStatus: extensionStatus,
            extensionStatusUpdatedAt: extensionStatusUpdatedAt,
            now: Date(),
            micIsRunning: false
        ) {
        case .rejectMicStart:
            publish(.blockedByBroadcast)
            return
        case .allowMicStart, .stopMic:
            break
        }

        isRequestingPermission = true
        // iOS 17 起由 AVAudioApplication 统一申请录音权限；不把系统权限 API 散在 View 里。
        AVAudioApplication.requestRecordPermission { [weak self] granted in
            DispatchQueue.main.async {
                guard let self else { return }
                self.isRequestingPermission = false
                guard granted else {
                    self.publish(.denied)
                    return
                }
                self.beginCapture()
            }
        }
    }

    func stop() {
        shouldResumeAfterInterruption = false
        stopAudioPipeline()
        restorePiPAudioSession()
        publish(.idle)
    }

    /// 状态通知来自广播扩展时，主 App 必须主动让出同一组 Keychain/字幕通道。
    func reconcileBroadcast(extensionStatus: String?, extensionStatusUpdatedAt: Date?) {
        rememberExtensionStatus(extensionStatus, updatedAt: extensionStatusUpdatedAt)
        guard isRunning else { return }
        if CaptureArbiter.decide(
            extensionStatus: extensionStatus,
            extensionStatusUpdatedAt: extensionStatusUpdatedAt,
            now: Date(),
            micIsRunning: true
        ) == .stopMic {
            stop(with: .error(DesignTokens.Text.micStoppedByBroadcast))
        }
    }

    private func beginCapture() {
        // 权限弹窗或来电期间广播可能刚启动；重启前用最新通知再判一次，不能只信点击当时的状态。
        if CaptureArbiter.decide(
            extensionStatus: latestExtensionStatus,
            extensionStatusUpdatedAt: latestExtensionStatusUpdatedAt,
            now: Date(),
            micIsRunning: false
        ) == .rejectMicStart {
            publish(.blockedByBroadcast)
            return
        }
        do {
            try applyAudioSession(MicAudioSessionPlan.forMic(active: true))
            try startAudioPipeline(restartingSubtitleEngine: false)
            publish(.running)
        } catch {
            stopAudioPipeline()
            restorePiPAudioSession()
            publish(.error(DesignTokens.Text.micError))
        }
    }

    private func stop(with nextState: MicCaptureState) {
        stopAudioPipeline()
        restorePiPAudioSession()
        publish(nextState)
    }

    private func startAudioPipeline(restartingSubtitleEngine: Bool) throws {
        let inputNode = audioEngine.inputNode
        inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        audioEngine.reset()

        if restartingSubtitleEngine {
            ingestQueue.sync { subtitleEngine.finish() }
        }

        let format = inputNode.outputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            throw MicCaptureError.invalidInputFormat
        }
        let queue = ingestQueue
        let subtitleEngine = subtitleEngine
        inputNode.installTap(onBus: 0, bufferSize: 4096, format: format) { buffer, _ in
            // tap 返回后原 buffer 可被 Core Audio 复用；先复制再投递，实时线程只做这两件事。
            guard let copiedBuffer = copy(buffer) else { return }
            queue.async {
                subtitleEngine.ingest(pcmBuffer: copiedBuffer)
            }
        }

        // 先装 tap 再启动路由：audioEngine.start() 之后的第一帧不会落在尚未初始化的引擎前面。
        ingestQueue.sync { subtitleEngine.start() }
        audioEngine.prepare()
        try audioEngine.start()
    }

    private func stopAudioPipeline() {
        audioEngine.inputNode.removeTap(onBus: 0)
        audioEngine.stop()
        audioEngine.reset()
        // sync 会排在已投递的 buffer 之后：不丢 tap 退出前已经复制的音频，也不会让 finish 与 ingest 并发。
        ingestQueue.sync { subtitleEngine.finish() }
    }

    private func applyAudioSession(_ plan: MicAudioSessionPlan) throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(plan.avCategory, mode: plan.avMode, options: plan.avOptions)
        try session.setActive(true)
    }

    private func restorePiPAudioSession() {
        do {
            // 不 deactivate：PiP 仍需要 active playback session 才能持续显示和发声。
            try applyAudioSession(MicAudioSessionPlan.forMic(active: false))
        } catch {
            publish(.error(DesignTokens.Text.micError))
        }
    }

    private func handleInterruption(_ notification: Notification) {
        guard let rawValue = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt,
              let type = AVAudioSession.InterruptionType(rawValue: rawValue) else { return }

        switch type {
        case .began:
            shouldResumeAfterInterruption = isRunning
            guard shouldResumeAfterInterruption else { return }
            stopAudioPipeline()
            restorePiPAudioSession()
            publish(.error(DesignTokens.Text.micInterrupted))
        case .ended:
            guard shouldResumeAfterInterruption else { return }
            shouldResumeAfterInterruption = false
            let optionsRaw = notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0
            let options = AVAudioSession.InterruptionOptions(rawValue: optionsRaw)
            guard options.contains(.shouldResume) else {
                publish(.error(DesignTokens.Text.micInterrupted))
                return
            }
            beginCapture()
        @unknown default:
            return
        }
    }

    private func handleRouteChange(_ notification: Notification) {
        guard isRunning else { return }
        let rawValue = notification.userInfo?[AVAudioSessionRouteChangeReasonKey] as? UInt
        let reason = rawValue.flatMap(AVAudioSession.RouteChangeReason.init(rawValue:))
        // setCategory 本身也会发 categoryChange；只为真实设备/路由配置变化重建，避免自己触发自己重启。
        guard reason == .newDeviceAvailable || reason == .oldDeviceUnavailable || reason == .routeConfigurationChange else {
            return
        }
        do {
            // 换耳机/蓝牙时 input format 可能已变；必须把 tap 与转换器一起重建，不能沿用旧格式。
            try applyAudioSession(MicAudioSessionPlan.forMic(active: true))
            try startAudioPipeline(restartingSubtitleEngine: true)
            publish(.running)
        } catch {
            stop(with: .error(DesignTokens.Text.micError))
        }
    }

    private func publish(_ state: MicCaptureState) {
        if Thread.isMainThread {
            self.state = state
        } else {
            DispatchQueue.main.async { self.state = state }
        }
    }

    private func rememberExtensionStatus(_ status: String?, updatedAt: Date?) {
        latestExtensionStatus = status
        latestExtensionStatusUpdatedAt = updatedAt
    }
}

/// `AVAudioPCMBuffer(copying:)` 是 iOS 27 才新增的 API；部署目标 iOS 17 时手动拷贝各 AudioBuffer。
/// 此函数只在 tap 内调用，不能做格式转换、同步等待或访问共享状态。
private func copy(_ buffer: AVAudioPCMBuffer) -> AVAudioPCMBuffer? {
    guard buffer.frameLength > 0,
          let copiedBuffer = AVAudioPCMBuffer(pcmFormat: buffer.format, frameCapacity: buffer.frameLength) else {
        return nil
    }
    copiedBuffer.frameLength = buffer.frameLength
    let sourceBuffers = UnsafeMutableAudioBufferListPointer(buffer.mutableAudioBufferList)
    let destinationBuffers = UnsafeMutableAudioBufferListPointer(copiedBuffer.mutableAudioBufferList)
    guard sourceBuffers.count == destinationBuffers.count else { return nil }
    for index in sourceBuffers.indices {
        guard let source = sourceBuffers[index].mData,
              let destination = destinationBuffers[index].mData else { return nil }
        memcpy(destination, source, Int(sourceBuffers[index].mDataByteSize))
    }
    return copiedBuffer
}

private enum MicCaptureError: Error {
    case invalidInputFormat
}

private extension MicAudioSessionPlan {
    var avCategory: AVAudioSession.Category {
        switch category {
        case .playback: .playback
        case .playAndRecord: .playAndRecord
        }
    }

    var avMode: AVAudioSession.Mode {
        switch mode {
        case .defaultMode: .default
        }
    }

    var avOptions: AVAudioSession.CategoryOptions {
        options.reduce(into: []) { result, option in
            switch option {
            case .mixWithOthers:
                result.insert(.mixWithOthers)
            case .defaultToSpeaker:
                result.insert(.defaultToSpeaker)
            case .allowBluetoothHFP:
                // .allowBluetooth 自 iOS 15 弃用；部署目标 iOS 17，改用覆盖 HFP 输入设备的新名称。
                result.insert(.allowBluetoothHFP)
            }
        }
    }
}
