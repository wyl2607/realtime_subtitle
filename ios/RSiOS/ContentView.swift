import SwiftUI
import Combine
import ReplayKit
import AVFoundation
import AVKit
import CoreMedia
import CoreVideo
import os
import UniformTypeIdentifiers
import Speech
import Translation

struct ContentView: View {
    @StateObject private var pipManager = PiPManager()
    @StateObject private var statusMonitor = StatusMonitor()
    @StateObject private var linesMonitor = LinesMonitor()
    @StateObject private var transcriptStore = TranscriptStore()
    @ObservedObject private var appStateMonitor = AppStateMonitor.shared

    @State private var pairingLinkText = ""
    @State private var pairingStatus = ""
    @State private var pairedConfigDisplay = ""

    @State private var engineMode: String = "auto"
    @State private var prepareStatus = ""
    @State private var isPreparing = false
    /// Groq key 的输入框内容。只在用户输入时存在，保存后立刻清空。
    @State private var groqKeyInput: String = ""
    /// 已保存 key 的掩码（只显示后 4 位）。完整 key 只在 Keychain 里。
    @State private var groqKeyMasked: String = ""
    @State private var cloudEnabled: Bool = false
    @State private var cloudKeyStatus: String = ""
    /// 每次收到新句对就 +1：「今天的字幕」页面靠它触发重新读存档文件。
    @State private var transcriptRefresh = 0

    var body: some View {
        NavigationStack {
            // 内容比一屏高：必须能滚动。没有 ScrollView 时 VStack 居中溢出，顶部的 Broadcast 按钮、
            // 配对输入框被裁到状态栏后面，用户既滑不动也点不到（iPhone 15 Pro 真机复现）。
            ScrollView {
                VStack(spacing: 24) {
                    Text("RSiOS Spike")
                        .font(.largeTitle)
                        .fontWeight(.bold)

                    VStack(spacing: 12) {
                        Text("Broadcast Extension Picker")
                            .font(.headline)
                        BroadcastPickerView()
                            .frame(width: 60, height: 60)
                        Text("点上面的圆形按钮 → 选「实时字幕」→ 开始直播")
                            .font(.footnote)
                            .foregroundStyle(.secondary)
                    }

                    Divider()

                    VStack(spacing: 12) {
                        Text("节点配对")
                            .font(.headline)

                        HStack {
                            TextField("粘贴 rsnode://pair?... 链接", text: $pairingLinkText)
                                .textFieldStyle(.roundedBorder)
                                .autocapitalization(.none)
                                .disableAutocorrection(true)

                            Button("粘贴配对链接") {
                                if let pasteboard = UIPasteboard.general.string {
                                    pairingLinkText = pasteboard
                                }
                            }
                            .buttonStyle(.bordered)
                        }

                        Button("应用配对") {
                            applyPairingLink()
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(pairingLinkText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                        if !pairingStatus.isEmpty {
                            Text(pairingStatus)
                                .font(.caption)
                                .foregroundColor(pairingStatus.hasPrefix("已配对") ? .green : .red)
                        }

                        if !pairedConfigDisplay.isEmpty {
                            Text("已配对：\(pairedConfigDisplay)")
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }
                    }

                    Divider()

                    VStack(spacing: 12) {
                        Text("识别引擎")
                            .font(.headline)

                        Picker("引擎", selection: $engineMode) {
                            Text("自动").tag("auto")
                            Text("电脑节点").tag("node")
                            Text("本机").tag("local")
                        }
                        .pickerStyle(.segmented)
                        .onChange(of: engineMode) { _, newValue in
                            SharedKeychain.writeEngineMode(newValue)
                        }
                        .onAppear {
                            engineMode = SharedKeychain.readEngineMode()
                        }

                        Button(action: prepareLocalAssets) {
                            HStack {
                                if isPreparing {
                                    ProgressView().scaleEffect(0.8)
                                }
                                Text(isPreparing ? "准备中..." : "准备本机识别资源")
                            }
                            .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(isPreparing)

                        if !prepareStatus.isEmpty {
                            Text(prepareStatus)
                                .font(.caption)
                                .foregroundColor(prepareStatus.hasPrefix("错误") || prepareStatus.hasPrefix("需要") ? .red : .green)
                        }
                    }

                    Divider()

                    VStack(spacing: 12) {
                        Text("云端识别（Groq）")
                            .font(.headline)

                        if groqKeyMasked.isEmpty {
                            SecureField("Groq API key", text: $groqKeyInput)
                                .textFieldStyle(.roundedBorder)
                                .autocapitalization(.none)
                                .disableAutocorrection(true)

                            Button("保存 key") {
                                saveGroqKey()
                            }
                            .buttonStyle(.bordered)
                            .disabled(groqKeyInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                        } else {
                            HStack {
                                Text("已保存：\(groqKeyMasked)")
                                    .font(.caption)
                                    .foregroundColor(.secondary)
                                Spacer()
                                Button("清除 key") {
                                    clearGroqKey()
                                }
                                .buttonStyle(.bordered)
                            }
                        }

                        Toggle("启用云端识别", isOn: $cloudEnabled)
                            .onChange(of: cloudEnabled) { _, newValue in
                                SharedKeychain.writeCloudEnabled(newValue)
                            }

                        if !cloudKeyStatus.isEmpty {
                            Text(cloudKeyStatus)
                                .font(.caption)
                                .foregroundColor(cloudKeyStatus.hasPrefix("已保存") ? .green : .red)
                        }

                        Text("开着时音频片段会发给 Groq；关掉后只走电脑节点和本机。")
                            .font(.caption)
                            .foregroundColor(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }

                    Divider()

                    VStack(spacing: 12) {
                        Text("App State: \(appStateMonitor.isInBackground ? "后台" : "前台")")
                            .font(.headline)
                            .foregroundColor(appStateMonitor.isInBackground ? .orange : .green)

                        Text("Picture-in-Picture")
                            .font(.headline)

                        PiPHostView(pipManager: pipManager)
                            .frame(width: 320, height: 90)
                            .background(Color(.systemGray6))
                            .cornerRadius(8)

                        Button(action: {
                            if pipManager.isPiPActive {
                                pipManager.stopPiP()
                            } else {
                                pipManager.startPiP()
                            }
                        }) {
                            Text(pipManager.isPiPActive ? "Stop PiP" : "Start PiP")
                                .frame(maxWidth: .infinity)
                        }
                        .buttonStyle(.borderedProminent)
                        .disabled(pipManager.isStarting)

                        HStack {
                            Text("PiP Possible: \(pipManager.isPictureInPicturePossible ? "Yes" : "No")")
                                .font(.caption)
                                .foregroundColor(pipManager.isPictureInPicturePossible ? .green : .red)
                            Spacer()
                            Text(pipManager.pipStatusLine)
                                .font(.caption)
                                .foregroundColor(.secondary)
                        }

                        if let error = pipManager.lastError {
                            Text("Error: \(error)")
                                .font(.caption)
                                .foregroundColor(.red)
                        }
                    }

                    Divider()

                    VStack(spacing: 8) {
                        Text("Extension Status (Keychain)")
                            .font(.headline)
                        Text(statusMonitor.keychainStatus)
                            .font(.system(.body, design: .monospaced))
                            .foregroundColor(.secondary)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding()
                            .background(Color(.systemGray6))
                            .cornerRadius(8)
                    }

                    VStack(spacing: 8) {
                        Text("最近句对 (来自扩展)")
                            .font(.headline)
                        if linesMonitor.lines.isEmpty {
                            Text("暂无字幕")
                                .font(.system(.body, design: .monospaced))
                                .foregroundColor(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding()
                                .background(Color(.systemGray6))
                                .cornerRadius(8)
                        } else {
                            ForEach(linesMonitor.lines.indices, id: \.self) { idx in
                                let line = linesMonitor.lines[idx]
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("[\(line.id)] \(line.src)")
                                        .font(.system(.body, design: .monospaced))
                                        .foregroundColor(.primary)
                                    Text(line.dst.isEmpty ? "…" : line.dst)
                                        .font(.system(.body, design: .monospaced))
                                        .foregroundColor(.secondary)
                                }
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .padding()
                                .background(Color(.systemGray6))
                                .cornerRadius(8)
                            }
                        }
                    }

                    VStack(spacing: 8) {
                        Text("Darwin Notifications")
                            .font(.headline)
                        Text("Tick: \(statusMonitor.darwinNotificationCount)  Lines: \(linesMonitor.darwinNotificationCount)")
                            .font(.system(.body, design: .monospaced))
                            .foregroundColor(.secondary)
                    }

                    Divider()

                    VStack(spacing: 12) {
                        Text("今天的字幕")
                            .font(.headline)
                        NavigationLink {
                            TranscriptView(store: transcriptStore, refreshToken: transcriptRefresh)
                        } label: {
                            Text("查看今天的字幕")
                        }
                    }

                    Spacer()
                }
                .padding()
            }
            .scrollDismissesKeyboard(.interactively)
            .navigationTitle("RSiOS")
            .onAppear {
                loadPairedConfig()
                loadCloudSettings()
            }
            // 相机扫配对码 / 点 rsnode://pair 链接：只把链接填进配对框，用户点「应用配对」才写入钥匙串。
            .onOpenURL { url in
                guard let text = PairingLink.prefillText(for: url) else { return }
                pairingLinkText = text
                pairingStatus = "已填入配对链接，确认后点「应用配对」"
            }
            .onReceive(linesMonitor.$lines) { lines in
                transcriptStore.ingest(lines.map {
                    TranscriptStore.Sentence(id: $0.id, session: $0.session, src: $0.src, dst: $0.dst)
                })
                transcriptRefresh += 1
            }
        }
    }

    private func applyPairingLink() {
        let trimmed = pairingLinkText.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else {
            pairingStatus = "配对链接为空"
            return
        }

        if let config = PairingLink.parse(trimmed) {
            let jsonData = try? JSONEncoder().encode(config)
            let jsonString = jsonData.flatMap { String(data: $0, encoding: .utf8) } ?? ""
            if SharedKeychain.writeNodeConfig(jsonString) {
                pairingStatus = "已配对"
                pairedConfigDisplay = config.displayName
                pairingLinkText = ""
            } else {
                pairingStatus = "配对写入失败"
            }
        } else {
            pairingStatus = "配对链接无效"
            pairedConfigDisplay = ""
        }
    }

    private func loadPairedConfig() {
        if let json = SharedKeychain.readNodeConfig(),
           let data = json.data(using: .utf8),
           let config = try? JSONDecoder().decode(NodeConfig.self, from: data) {
            pairedConfigDisplay = config.displayName
        }
    }

    // MARK: - 云端识别（Groq）

    private func loadCloudSettings() {
        cloudEnabled = SharedKeychain.readCloudEnabled()
        groqKeyMasked = SharedKeychain.maskedGroqKey() ?? ""
    }

    /// key 只进 Keychain：写完立刻把输入框清掉，界面上以后只留后 4 位。
    private func saveGroqKey() {
        let key = groqKeyInput.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !key.isEmpty else { return }
        if SharedKeychain.writeGroqKey(key) {
            groqKeyInput = ""
            groqKeyMasked = SharedKeychain.maskedGroqKey() ?? ""
            cloudKeyStatus = "已保存到钥匙串"
        } else {
            cloudKeyStatus = "保存失败"
        }
    }

    /// 清掉 key：云端这一档立刻失效（扩展下次读到就没 key 了）。
    private func clearGroqKey() {
        _ = SharedKeychain.clearGroqKey()
        groqKeyInput = ""
        groqKeyMasked = ""
        cloudKeyStatus = "已清除"
    }

    private func prepareLocalAssets() {
        guard #available(iOS 26.0, *) else {
            prepareStatus = "需要 iOS 26"
            return
        }

        isPreparing = true
        prepareStatus = "检查资源状态..."

        Task {
            do {
                let locale = Locale(identifier: "de-DE")
                guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: locale) else {
                    await MainActor.run {
                        isPreparing = false
                        prepareStatus = "错误：不支持德语识别"
                    }
                    return
                }

                let transcriber = SpeechTranscriber(
                    locale: supported,
                    transcriptionOptions: [],
                    reportingOptions: [.volatileResults, .fastResults],
                    attributeOptions: [.audioTimeRange]
                )

                let assetStatus = await AssetInventory.status(forModules: [transcriber])
                let txStatus = await LanguageAvailability().status(from: Locale.Language(identifier: "de"), to: Locale.Language(identifier: "zh-Hans"))

                await MainActor.run {
                    isPreparing = false
                    var parts: [String] = ["ASR资源: \(assetStatus)"]
                    parts.append("翻译 de→zh-Hans: \(txStatus)")
                    prepareStatus = parts.joined(separator: " · ")
                }

                if assetStatus != .installed {
                    await MainActor.run {
                        prepareStatus += " · 点击再次准备将下载资源"
                    }
                }
            } catch {
                await MainActor.run {
                    isPreparing = false
                    prepareStatus = "错误: \(error.localizedDescription)"
                }
            }
        }
    }
}

struct BroadcastPickerView: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        // 必须给非零尺寸：系统在 init 时按这个尺寸建内部按钮，零尺寸建出来的按钮不可见也点不到。
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 60, height: 60))
        picker.preferredExtension = "com.yilinwang.realtimesubtitle.broadcast"
        picker.showsMicrophoneButton = false
        // 系统按钮默认画成黑色，深色界面上看不见（真机上只剩一块空白，用户找不到开播入口）。
        // 换成 label 色：深色模式白、浅色模式黑。
        for case let button as UIButton in picker.subviews {
            button.imageView?.tintColor = .label
        }
        return picker
    }

    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {}
}

struct PiPHostView: UIViewRepresentable {
    @ObservedObject var pipManager: PiPManager

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

struct RecentLine: Codable, Identifiable {
    let id: Int
    let src: String
    var dst: String
    /// 产生这一句的引擎会话代号：各引擎的 id 都从 1 重新计数，
    /// 存档靠「代号 + id」去重，不然后一个会话会覆盖前一个会话的句子。
    let session: String
}

final class PiPManager: NSObject, ObservableObject, AVPictureInPictureSampleBufferPlaybackDelegate {
    @Published var isPiPActive = false
    @Published var isStarting = false
    @Published var lastError: String?
    @Published var isPictureInPicturePossible = false
    @Published var backgroundFrameCount = 0

    private var pipController: AVPictureInPictureController?
    fileprivate var sampleBufferDisplayLayer: AVSampleBufferDisplayLayer?
    private var renderTimer: Timer?
    private var frameCount = 0
    private var pipPossibleObservation: NSKeyValueObservation?
    private var linesObserver: UnsafeRawPointer?
    private var pollTimer: Timer?
    private var appStateObserver: NSObjectProtocol?

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
        registerLinesNotification()
        startPolling()
        observeAppState()
        startRendering()
    }

    deinit {
        stopRendering()
        stopPolling()
        if let observer = linesObserver {
            CFNotificationCenterRemoveObserver(CFNotificationCenterGetDarwinNotifyCenter(), observer, nil, nil)
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
            self?.backgroundFrameCount = 0
        }
    }

    private func registerLinesNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.lines" as CFString
        let observer = UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque())

        CFNotificationCenterAddObserver(center, observer, { _, observer, _, _, _ in
            guard let observer = observer else { return }
            let manager = Unmanaged<PiPManager>.fromOpaque(observer).takeUnretainedValue()
            DispatchQueue.main.async {
                manager.refreshLinesFromKeychain()
            }
        }, name, nil, .deliverImmediately)

        self.linesObserver = observer
        refreshLinesFromKeychain()
    }

    private func startPolling() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.refreshLinesFromKeychain()
        }
        pollTimer?.fire()
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func refreshLinesFromKeychain() {
        if let json = SharedKeychain.readLatestLines(),
           let data = json.data(using: .utf8),
           let lines = try? JSONDecoder().decode([RecentLine].self, from: data) {
            DispatchQueue.main.async {
                self.objectWillChange.send()
            }
        }
    }

    private func extractStatusLine() -> String? {
        guard let status = SharedKeychain.readExtStatus() else { return nil }
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
        renderTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.renderFrame()
        }
        renderTimer?.fire()
    }

    private func stopRendering() {
        renderTimer?.invalidate()
        renderTimer = nil
    }

    private func renderFrame() {
        guard let layer = sampleBufferDisplayLayer else { return }

        let status = SharedKeychain.readExtStatus() ?? "无"
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

        if layer.status == .failed {
            layer.flush()
        }
        layer.enqueue(sbWithAttachment)

        if AppStateMonitor.shared.isInBackground {
            DispatchQueue.main.async {
                self.backgroundFrameCount += 1
            }
        }
    }

    private var cachedLines: [RecentLine] = []
    private var lastRenderedLinesJSON: String = ""
    private var lastRenderedStatus: String = ""

    private func renderSubtitleImage(status: String, time: String) -> UIImage? {
        var linesJSON = ""
        if let json = SharedKeychain.readLatestLines(),
           let data = json.data(using: .utf8),
           let lines = try? JSONDecoder().decode([RecentLine].self, from: data) {
            cachedLines = lines
            linesJSON = json
        }

        if linesJSON == lastRenderedLinesJSON && status == lastRenderedStatus {
            return nil
        }
        lastRenderedLinesJSON = linesJSON
        lastRenderedStatus = status

        let displayLines = Array(cachedLines.suffix(2))

        let size = CGSize(width: 1280, height: 360)
        let renderer = UIGraphicsImageRenderer(size: size)
        return renderer.image { ctx in
            UIColor.black.withAlphaComponent(0.85).setFill()
            ctx.fill(CGRect(origin: .zero, size: size))

            let paragraphStyle = NSMutableParagraphStyle()
            paragraphStyle.alignment = .left

            let srcAttrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 48, weight: .medium),
                .foregroundColor: UIColor(red: 0xF0/255, green: 0xF0/255, blue: 0xF0/255, alpha: 1.0),
                .paragraphStyle: paragraphStyle
            ]
            let dstAttrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 42, weight: .regular),
                .foregroundColor: UIColor(red: 0xFF/255, green: 0xE8/255, blue: 0x94/255, alpha: 1.0),
                .paragraphStyle: paragraphStyle
            ]
            let dstEmptyAttrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 42, weight: .regular),
                .foregroundColor: UIColor(red: 0xFF/255, green: 0xE8/255, blue: 0x94/255, alpha: 0.5),
                .paragraphStyle: paragraphStyle
            ]
            let statusAttrs: [NSAttributedString.Key: Any] = [
                .font: UIFont.systemFont(ofSize: 28, weight: .regular),
                .foregroundColor: UIColor.white.withAlphaComponent(0.7),
                .paragraphStyle: paragraphStyle
            ]

            let statusLine = extractStatusLine() ?? ""
            let statusStr = "\(time) · \(statusLine)" as NSString

            let lineHeight: CGFloat = 56
            let gap: CGFloat = 8
            let pairGap: CGFloat = 16
            let startY: CGFloat = 20

            var y = startY
            for (idx, line) in displayLines.enumerated() {
                let alpha = idx == 0 ? 0.55 : 1.0

                let srcText = "[\(line.id)] \(line.src)" as NSString
                let srcAttrsWithAlpha = idx == 0 ? srcAttrs.mapValues { value in
                    if let color = value as? UIColor {
                        return color.withAlphaComponent(0.55)
                    }
                    return value
                } : srcAttrs
                let srcHeight = srcText.boundingRect(with: CGSize(width: 1200, height: 200),
                                                      options: .usesLineFragmentOrigin,
                                                      attributes: srcAttrsWithAlpha, context: nil).height
                srcText.draw(in: CGRect(x: 40, y: y, width: 1200, height: srcHeight), withAttributes: srcAttrsWithAlpha)
                y += srcHeight + gap

                let dstText = (line.dst.isEmpty ? "…" : line.dst) as NSString
                let dstAttrsFinal = line.dst.isEmpty ? dstEmptyAttrs : dstAttrs
                let dstAttrsWithAlpha = idx == 0 ? dstAttrsFinal.mapValues { value in
                    if let color = value as? UIColor {
                        return color.withAlphaComponent(0.55)
                    }
                    return value
                } : dstAttrsFinal
                let dstHeight = dstText.boundingRect(with: CGSize(width: 1200, height: 200),
                                                      options: .usesLineFragmentOrigin,
                                                      attributes: dstAttrsWithAlpha, context: nil).height
                dstText.draw(in: CGRect(x: 40, y: y, width: 1200, height: dstHeight), withAttributes: dstAttrsWithAlpha)
                y += dstHeight + pairGap
            }

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

extension PiPManager: AVPictureInPictureControllerDelegate {
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

extension DateFormatter {
    static let timeFormatter: DateFormatter = {
        let df = DateFormatter()
        df.dateFormat = "HH:mm:ss"
        return df
    }()
}

final class StatusMonitor: ObservableObject {
    @Published var keychainStatus: String = "无"
    @Published var darwinNotificationCount: Int = 0

    private var timer: Timer?
    private var darwinObserver: UnsafeRawPointer?

    init() {
        startMonitoring()
        registerDarwinNotification()
    }

    deinit {
        stopMonitoring()
        if let observer = darwinObserver {
            CFNotificationCenterRemoveObserver(CFNotificationCenterGetDarwinNotifyCenter(), observer, nil, nil)
        }
    }

    private func startMonitoring() {
        timer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.updateKeychainStatus()
        }
        timer?.fire()
    }

    private func stopMonitoring() {
        timer?.invalidate()
        timer = nil
    }

    private func updateKeychainStatus() {
        DispatchQueue.main.async {
            self.keychainStatus = SharedKeychain.readExtStatus() ?? "无"
        }
    }

    private func registerDarwinNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.tick" as CFString
        let observer = UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque())

        CFNotificationCenterAddObserver(center, observer, { _, observer, _, _, _ in
            guard let observer = observer else { return }
            let monitor = Unmanaged<StatusMonitor>.fromOpaque(observer).takeUnretainedValue()
            DispatchQueue.main.async {
                monitor.darwinNotificationCount += 1
            }
        }, name, nil, .deliverImmediately)

        self.darwinObserver = observer
    }
}

final class LinesMonitor: ObservableObject {
    @Published var lines: [RecentLine] = []
    @Published var darwinNotificationCount: Int = 0

    private var linesObserver: UnsafeRawPointer?
    private var pollTimer: Timer?

    init() {
        registerLinesNotification()
        startPolling()
        refreshFromKeychain()
    }

    deinit {
        stopPolling()
        if let observer = linesObserver {
            CFNotificationCenterRemoveObserver(CFNotificationCenterGetDarwinNotifyCenter(), observer, nil, nil)
        }
    }

    private func registerLinesNotification() {
        let center = CFNotificationCenterGetDarwinNotifyCenter()
        let name = "com.wyl2607.rsios.spike.lines" as CFString
        let observer = UnsafeRawPointer(Unmanaged.passUnretained(self).toOpaque())

        CFNotificationCenterAddObserver(center, observer, { _, observer, _, _, _ in
            guard let observer = observer else { return }
            let monitor = Unmanaged<LinesMonitor>.fromOpaque(observer).takeUnretainedValue()
            DispatchQueue.main.async {
                monitor.darwinNotificationCount += 1
                monitor.refreshFromKeychain()
            }
        }, name, nil, .deliverImmediately)

        self.linesObserver = observer
    }

    private func startPolling() {
        pollTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.refreshFromKeychain()
        }
        pollTimer?.fire()
    }

    private func stopPolling() {
        pollTimer?.invalidate()
        pollTimer = nil
    }

    private func refreshFromKeychain() {
        if let json = SharedKeychain.readLatestLines(),
           let data = json.data(using: .utf8),
           let lines = try? JSONDecoder().decode([RecentLine].self, from: data) {
            DispatchQueue.main.async {
                self.lines = lines
            }
        }
    }
}

final class AppStateMonitor: ObservableObject {
    static let shared = AppStateMonitor()

    @Published var isInBackground = false

    private init() {
        NotificationCenter.default.addObserver(self, selector: #selector(didEnterBackground), name: UIApplication.didEnterBackgroundNotification, object: nil)
        NotificationCenter.default.addObserver(self, selector: #selector(willEnterForeground), name: UIApplication.willEnterForegroundNotification, object: nil)
    }

    @objc private func didEnterBackground() {
        DispatchQueue.main.async {
            self.isInBackground = true
        }
    }

    @objc private func willEnterForeground() {
        DispatchQueue.main.async {
            self.isInBackground = false
        }
    }
}