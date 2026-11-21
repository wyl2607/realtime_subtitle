import AVFoundation
import AVKit
import CoreMedia
import CoreVideo
import Observation
import SwiftUI
import UIKit

struct PiPHostView: UIViewRepresentable {
    let pipManager: PiPManager

    func makeUIView(context: Context) -> PiPHostUIView {
        let view = PiPHostUIView()
        view.pipManager = pipManager
        return view
    }

    func updateUIView(_ uiView: PiPHostUIView, context: Context) {
        uiView.pipManager = pipManager
        uiView.updateLayerFrame()
    }
}

final class PiPHostUIView: UIView {
    weak var pipManager: PiPManager?

    override func layoutSubviews() {
        super.layoutSubviews()
        updateLayerFrame()
    }

    func updateLayerFrame() {
        guard let layer = pipManager?.sampleBufferDisplayLayer else { return }
        if layer.superlayer != self.layer {
            self.layer.addSublayer(layer)
        }
        layer.frame = bounds
    }
}

/// 画中画走 Core Graphics，不能直接吃 SwiftUI 的 Color；颜色仍从同一份令牌/样式解析。
fileprivate func uiColor(hex: String, alpha: CGFloat) -> UIColor {
    guard let rgb = SubtitleStyle.rgb(hex: hex) else {
        return UIColor.white.withAlphaComponent(alpha)
    }
    return UIColor(red: CGFloat(rgb.r), green: CGFloat(rgb.g), blue: CGFloat(rgb.b), alpha: alpha)
}

@MainActor
@Observable
final class PiPManager: NSObject, @preconcurrency AVPictureInPictureSampleBufferPlaybackDelegate {
    var isPiPActive = false
    var isStarting = false
    var lastError: String?
    var isPictureInPicturePossible = false
    var backgroundFrameCount = 0

    @ObservationIgnored private var pipController: AVPictureInPictureController?
    @ObservationIgnored var sampleBufferDisplayLayer: AVSampleBufferDisplayLayer?
    @ObservationIgnored private var renderTimer: Timer?
    @ObservationIgnored private var frameCount = 0
    @ObservationIgnored private var pipPossibleObservation: NSKeyValueObservation?
    @ObservationIgnored private var feedObserver: NSObjectProtocol?
    @ObservationIgnored private var appStateObserver: NSObjectProtocol?

    var pipStatusLine: String {
        let timeString = DateFormatter.timeFormatter.string(from: Date())
        var parts: [String] = [timeString]
        if backgroundFrameCount > 0 {
            parts.append("后台刷新 \(backgroundFrameCount) 帧")
        }
        if let statusLine = extractStatusLine() {
            parts.append(statusLine)
        }
        return parts.joined(separator: " · ")
    }

    override init() {
        super.init()
        setupAudioSession()
        setupPiPLayerAndController()
        observePiPPossible()
        observeLiveFeed()
        observeAppState()
        startRendering()
    }

    isolated deinit {
        stopRendering()
        if let observer = feedObserver {
            NotificationCenter.default.removeObserver(observer)
        }
        if let observer = appStateObserver {
            NotificationCenter.default.removeObserver(observer)
        }
    }

    private func setupAudioSession() {
        do {
            let session = AVAudioSession.sharedInstance()
            try session.setCategory(.playback, mode: .default, options: [.mixWithOthers])
            try session.setActive(true)
        } catch {
            print("Audio session setup failed: \(error)")
        }
    }

    private func setupPiPLayerAndController() {
        let layer = AVSampleBufferDisplayLayer()
        layer.videoGravity = .resizeAspect
        layer.backgroundColor = UIColor.clear.cgColor
        self.sampleBufferDisplayLayer = layer

        let contentSource = AVPictureInPictureController.ContentSource(
            sampleBufferDisplayLayer: layer,
            playbackDelegate: self
        )

        guard AVPictureInPictureController.isPictureInPictureSupported() else {
            lastError = "PiP not supported on this device"
            return
        }

        let controller = AVPictureInPictureController(contentSource: contentSource)
        controller.canStartPictureInPictureAutomaticallyFromInline = true
        controller.delegate = self
        self.pipController = controller
    }

    private func observePiPPossible() {
        guard let controller = pipController else { return }
        pipPossibleObservation = controller.observe(\.isPictureInPicturePossible, options: [.initial, .new]) { [weak self] _, change in
            DispatchQueue.main.async {
                self?.isPictureInPicturePossible = change.newValue ?? false
            }
        }
    }

    private func observeAppState() {
        appStateObserver = NotificationCenter.default.addObserver(
            forName: UIApplication.willEnterForegroundNotification,
            object: nil,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.backgroundFrameCount = 0 }
        }
    }

    /// 画中画不再自己读 Keychain：内容变化统一由 LiveFeed 通知。
    /// 未激活且在前台时定时器不再整帧重绘，这一帧负责让内嵌预览跟上。
    private func observeLiveFeed() {
        feedObserver = NotificationCenter.default.addObserver(
            forName: LiveFeed.didChangeNotification,
            object: LiveFeed.shared,
            queue: .main
        ) { [weak self] _ in
            MainActor.assumeIsolated { self?.renderFrame() }
        }
    }

    private func extractStatusLine() -> String? {
        let status = LiveFeed.shared.status
        guard status != "无" else { return nil }
        let parts = status.split(separator: " ")
        var nodeInfo: [String] = []
        for part in parts {
            if part.hasPrefix("node=") || part.hasPrefix("ready=") || part.hasPrefix("sent=") || part.hasPrefix("finals=") || part.hasPrefix("tx=") || part.hasPrefix("last=") {
                nodeInfo.append(String(part))
            }
        }
        return nodeInfo.isEmpty ? nil : nodeInfo.joined(separator: " ")
    }

    func startPiP() {
        guard !isStarting, !isPiPActive else { return }

        guard isPictureInPicturePossible else {
            lastError = "PiP 尚未就绪（isPictureInPicturePossible=false）"
            return
        }

        isStarting = true
        lastError = nil
        pipController?.startPictureInPicture()
    }

    func stopPiP() {
        pipController?.stopPictureInPicture()
    }

    private func startRendering() {
        stopRendering()
        frameCount = 0
        backgroundFrameCount = 0
        // 主线程 RunLoop 上的定时器，回调一定在主线程。
        renderTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tickRender() }
        }
        // 内嵌预览要先有一帧，「未激活 + 前台」的定时器跳过规则不适用于首帧。
        renderFrame()
    }

    private func stopRendering() {
        renderTimer?.invalidate()
        renderTimer = nil
    }

    /// 定时器节奏：画中画未激活、App 又在前台时这块画面没人看，不必每 0.5 秒
    /// 整帧重绘；激活时保持原来的 0.5 秒（系统按帧保活，停更会被判成卡住），
    /// 后台同理。前台未激活时的内容变化由 LiveFeed 的通知单独补帧。
    private func tickRender() {
        guard isPiPActive || AppStateMonitor.shared.isInBackground else { return }
        renderFrame()
    }

    private func renderFrame() {
        guard let layer = sampleBufferDisplayLayer else { return }

        let status = LiveFeed.shared.status
        // 解析不到档位（如刚 started）时保留上一次，避免状态词闪空。
        if let summary = StatusSummaryParser.summary(forStatusLine: status) {
            lastStatusSummary = summary
        }
        let timeString = DateFormatter.timeFormatter.string(from: Date())

        guard let image = renderSubtitleImage(status: status, time: timeString),
              let pixelBuffer = pixelBuffer(from: image) else { return }

        let hostTime = CMClockGetTime(CMClockGetHostTimeClock())
        var timingInfo = CMSampleTimingInfo(
            duration: CMTime(value: 1, timescale: 2),
            presentationTimeStamp: hostTime,
            decodeTimeStamp: .invalid
        )
        frameCount += 1

        var formatDescription: CMVideoFormatDescription?
        let fmtStatus = CMVideoFormatDescriptionCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pixelBuffer,
            formatDescriptionOut: &formatDescription
        )
        guard fmtStatus == noErr, let fmtDesc = formatDescription else { return }

        var sampleBuffer: CMSampleBuffer?
        let sbStatus = CMSampleBufferCreateForImageBuffer(
            allocator: kCFAllocatorDefault,
            imageBuffer: pixelBuffer,
            dataReady: true,
            makeDataReadyCallback: nil,
            refcon: nil,
            formatDescription: fmtDesc,
            sampleTiming: &timingInfo,
            sampleBufferOut: &sampleBuffer
        )
        guard sbStatus == noErr, let sb = sampleBuffer else { return }

        let sbWithAttachment = setDisplayImmediately(sb)

        // iOS 18 起队列/状态在 sampleBufferRenderer 上，图层上的同名 API 已弃用。
        let renderer = layer.sampleBufferRenderer
        if renderer.status == .failed {
            renderer.flush()
        }
        renderer.enqueue(sbWithAttachment)

        if AppStateMonitor.shared.isInBackground {
            DispatchQueue.main.async {
                self.backgroundFrameCount += 1
            }
        }
    }

    @ObservationIgnored private var lastRenderedLines: [RecentLine] = []
    @ObservationIgnored private var lastRenderedDraft: String = ""
    @ObservationIgnored private var lastRenderedStatus: String = ""
    @ObservationIgnored private var lastRenderedStyle: SubtitleStyle?
    @ObservationIgnored private var lastStatusSummary: StatusSummary?

    private func renderSubtitleImage(status: String, time: String) -> UIImage? {
        let lines = LiveFeed.shared.lines
        let draft = LiveFeed.shared.draft

        // 样式也要参与变化判断：用户改颜色/字号后即使句子没变也得立刻重绘。
        let style = SubtitleStyle.load(from: UserDefaults.standard.data(forKey: "subtitleStyle"))

        if lines == lastRenderedLines
            && draft == lastRenderedDraft
            && status == lastRenderedStatus
            && style == lastRenderedStyle {
            return nil
        }
        lastRenderedLines = lines
        lastRenderedDraft = draft
        lastRenderedStatus = status
        lastRenderedStyle = style

        let displayLines = Array(lines.suffix(2))

        // 画中画画面按样式字号等比缩放：译文基准 48，原文再乘 sourceScale。
        let scale = style.fontSize / DesignTokens.TypeScale.subtitleDefault
        let translationSize = CGFloat(48.0 * scale)
        let sourceSize = translationSize * CGFloat(DesignTokens.TypeScale.sourceScale)

        let size = CGSize(width: 1280, height: 360)
        let renderer = UIGraphicsImageRenderer(size: size)
        return renderer.image { ctx in
            uiColor(hex: style.backgroundHex, alpha: CGFloat(style.backgroundOpacity)).setFill()
            ctx.fill(CGRect(origin: .zero, size: size))

            let paragraphStyle = NSMutableParagraphStyle()
            paragraphStyle.alignment = .left

            let gap: CGFloat = 8
            let pairGap: CGFloat = 16
            let startY: CGFloat = 20

            var y = startY
            // 本机引擎失败（如识别资源没下载）时，画中画常常是用户唯一看着的地方：
            // 把原因画出来，别留一块空白让人以为还在等字幕。
            // 翻译不可用时原文照常在出，那种情况原因只放底部状态行，不能把字幕盖掉。
            let localMessage = LocalEngineStatus.userMessage(forStatusLine: status)
            if let localMessage, displayLines.isEmpty, draft.isEmpty {
                let messageAttrs: [NSAttributedString.Key: Any] = [
                    .font: UIFont.systemFont(ofSize: translationSize * 0.75, weight: .medium),
                    .foregroundColor: uiColor(hex: DesignTokens.Color.statusError, alpha: 1),
                    .paragraphStyle: paragraphStyle
                ]
                let messageText = localMessage as NSString
                let messageHeight = messageText.boundingRect(with: CGSize(width: 1200, height: 240),
                                                             options: .usesLineFragmentOrigin,
                                                             attributes: messageAttrs, context: nil).height
                messageText.draw(in: CGRect(x: 40, y: y, width: 1200, height: messageHeight), withAttributes: messageAttrs)
            } else {
                for (idx, line) in displayLines.enumerated() {
                    let isNewest = idx == displayLines.count - 1
                    let alpha = CGFloat(SubtitleDisplay.pairOpacity(index: idx, count: displayLines.count))
                    let translation = SubtitleDisplay.translation(line.dst, isNewest: isNewest)

                    let srcAttrs: [NSAttributedString.Key: Any] = [
                        .font: UIFont.systemFont(ofSize: sourceSize, weight: .medium),
                        .foregroundColor: uiColor(hex: style.sourceHex, alpha: alpha),
                        .paragraphStyle: paragraphStyle
                    ]
                    let dstAttrs: [NSAttributedString.Key: Any] = [
                        .font: UIFont.systemFont(ofSize: translationSize, weight: .regular),
                        .foregroundColor: uiColor(
                            hex: translation.isPlaceholder ? DesignTokens.Color.untranslated : style.translationHex,
                            alpha: alpha
                        ),
                        .paragraphStyle: paragraphStyle
                    ]

                    let srcText = line.src as NSString
                    let srcHeight = srcText.boundingRect(with: CGSize(width: 1200, height: 200),
                                                          options: .usesLineFragmentOrigin,
                                                          attributes: srcAttrs, context: nil).height
                    srcText.draw(in: CGRect(x: 40, y: y, width: 1200, height: srcHeight), withAttributes: srcAttrs)
                    y += srcHeight + gap

                    let dstText = translation.text as NSString
                    let dstHeight = dstText.boundingRect(with: CGSize(width: 1200, height: 200),
                                                          options: .usesLineFragmentOrigin,
                                                          attributes: dstAttrs, context: nil).height
                    dstText.draw(in: CGRect(x: 40, y: y, width: 1200, height: dstHeight), withAttributes: dstAttrs)
                    y += dstHeight + pairGap
                }

                // 未定稿草稿：只显示原文，放在最新句对下面，用草稿色区分。
                if !draft.isEmpty {
                    let draftAttrs: [NSAttributedString.Key: Any] = [
                        .font: UIFont.systemFont(ofSize: sourceSize * 0.95, weight: .regular),
                        .foregroundColor: uiColor(hex: DesignTokens.Color.subtitleDraft, alpha: 0.9),
                        .paragraphStyle: paragraphStyle
                    ]
                    let draftText = draft as NSString
                    let draftHeight = draftText.boundingRect(with: CGSize(width: 1200, height: 200),
                                                              options: .usesLineFragmentOrigin,
                                                              attributes: draftAttrs, context: nil).height
                    draftText.draw(in: CGRect(x: 40, y: y, width: 1200, height: draftHeight), withAttributes: draftAttrs)
                }
            }

            let summaryLevel = self.lastStatusSummary?.level ?? .idle
            let statusHex: String
            switch summaryLevel {
            case .normal:
                statusHex = DesignTokens.Color.statusNormal
            case .warning:
                statusHex = DesignTokens.Color.statusWarning
            case .error:
                statusHex = DesignTokens.Color.statusError
            case .idle:
                statusHex = DesignTokens.Color.subtitleSecondary
            }
            let statusAttrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 28, weight: .regular),
                .foregroundColor: uiColor(hex: statusHex, alpha: 0.9),
                .paragraphStyle: paragraphStyle
            ]

            let label = localMessage ?? self.lastStatusSummary?.label ?? DesignTokens.Text.micStatusIdle
            let statusStr = "\(time) · \(label)" as NSString
            let statusHeight = statusStr.boundingRect(with: CGSize(width: 1200, height: 100),
                                                       options: .usesLineFragmentOrigin,
                                                       attributes: statusAttrs, context: nil).height
            let statusY = size.height - statusHeight - 20
            statusStr.draw(in: CGRect(x: 40, y: statusY, width: 1200, height: statusHeight), withAttributes: statusAttrs)
        }
    }

    private func pixelBuffer(from image: UIImage) -> CVPixelBuffer? {
        let attrs: [String: Any] = [
            kCVPixelBufferCGImageCompatibilityKey as String: true,
            kCVPixelBufferCGBitmapContextCompatibilityKey as String: true,
            kCVPixelBufferWidthKey as String: 1280,
            kCVPixelBufferHeightKey as String: 360,
            kCVPixelBufferPixelFormatTypeKey as String: kCVPixelFormatType_32BGRA
        ]

        var pixelBuffer: CVPixelBuffer?
        let status = CVPixelBufferCreate(kCFAllocatorDefault, 1280, 360,
                                          kCVPixelFormatType_32BGRA, attrs as CFDictionary,
                                          &pixelBuffer)
        guard status == kCVReturnSuccess, let pb = pixelBuffer else { return nil }

        CVPixelBufferLockBaseAddress(pb, [])
        defer { CVPixelBufferUnlockBaseAddress(pb, []) }

        guard let context = CGContext(data: CVPixelBufferGetBaseAddress(pb),
                                       width: 1280, height: 360,
                                       bitsPerComponent: 8, bytesPerRow: CVPixelBufferGetBytesPerRow(pb),
                                       space: CGColorSpaceCreateDeviceRGB(),
                                       bitmapInfo: CGImageAlphaInfo.premultipliedFirst.rawValue | CGBitmapInfo.byteOrder32Little.rawValue),
              let cgImage = image.cgImage else { return nil }

        context.draw(cgImage, in: CGRect(x: 0, y: 0, width: 1280, height: 360))
        return pb
    }

    private func setDisplayImmediately(_ sampleBuffer: CMSampleBuffer) -> CMSampleBuffer {
        CMSetAttachment(sampleBuffer, key: kCMSampleAttachmentKey_DisplayImmediately, value: kCFBooleanTrue, attachmentMode: kCMAttachmentMode_ShouldNotPropagate)
        return sampleBuffer
    }

    // MARK: - AVPictureInPictureSampleBufferPlaybackDelegate

    func pictureInPictureControllerTimeRangeForPlayback(_ pictureInPictureController: AVPictureInPictureController) -> CMTimeRange {
        return CMTimeRange(start: .negativeInfinity, duration: .positiveInfinity)
    }

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, isPlaybackPausedForItemAt time: CMTime) -> Bool {
        return false
    }

    func pictureInPictureControllerIsPlaybackPaused(_ pictureInPictureController: AVPictureInPictureController) -> Bool {
        return false
    }

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, didChangePlaybackRate rate: Float) {}

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, didChangeSeekPosition seekPosition: CMTime) {}

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, setPlaying playing: Bool) {}

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, skipByInterval skipInterval: CMTime, completion: @escaping @Sendable () -> Void) {
        completion()
    }

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, didTransitionToRenderSize newRenderSize: CMVideoDimensions) {}
}

extension PiPManager: @preconcurrency AVPictureInPictureControllerDelegate {
    func pictureInPictureControllerDidStartPictureInPicture(_ pictureInPictureController: AVPictureInPictureController) {
        DispatchQueue.main.async {
            self.isPiPActive = true
            self.isStarting = false
        }
    }

    func pictureInPictureControllerDidStopPictureInPicture(_ pictureInPictureController: AVPictureInPictureController) {
        DispatchQueue.main.async {
            self.isPiPActive = false
            self.isStarting = false
        }
    }

    func pictureInPictureController(_ pictureInPictureController: AVPictureInPictureController, failedToStartPictureInPictureWithError error: Error) {
        DispatchQueue.main.async {
            self.lastError = error.localizedDescription
            self.isStarting = false
        }
    }
}
