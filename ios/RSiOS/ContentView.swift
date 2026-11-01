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

/// 设置页的四个目的页。用枚举而不是字符串，`navigationDestination` 才能按类型精确分派。
enum SettingsRoute: Hashable {
    case style
    case recognition
    case node
    case diagnostics
}

struct ContentView: View {
    @StateObject private var pipManager = PiPManager()
    @StateObject private var statusMonitor = StatusMonitor()
    @StateObject private var linesMonitor = LinesMonitor()
    @StateObject private var transcriptStore = TranscriptStore()
    @StateObject private var micCapture = MicCapture()
    @ObservedObject private var appStateMonitor = AppStateMonitor.shared

    @State private var pairingLinkText = ""
    @State private var pairingStatus = ""
    @State private var pairedConfigDisplay = ""

    @State private var engineMode: String = "auto"
    @State private var engineModeSaveFailed = false
    @State private var prepareStatus = ""
    /// 语音授权被拒的提示单独存一份：prepareStatus 每完成一步都会整段重写，
    /// 拼进去的拒绝提示会被覆盖，用户就再也看不到。
    @State private var prepareSpeechDeniedHint: String?
    @State private var isPreparing = false
    /// 翻译资源下载只能借 SwiftUI 的 `.translationTask` 拿到 TranslationSession。
    /// 该类型 iOS 18 起才可用，而工程部署目标是 iOS 17。存储属性不能标 `@available`
    /// （Xcode 26 直接报错，Xcode 27 才放行），所以用 Any? 存，用的地方都在 iOS 18 分支里再转回。
    @State private var translationConfig: Any?
    /// Groq key 的输入框内容。只在用户输入时存在，保存后立刻清空。
    @State private var groqKeyInput: String = ""
    /// 已保存 key 的掩码（只显示后 4 位）。完整 key 只在 Keychain 里。
    @State private var groqKeyMasked: String = ""
    @State private var cloudEnabled: Bool = false
    @State private var cloudKeyStatus: String = ""
    /// 每次收到新句对就 +1：「今天的字幕」页面靠它触发重新读存档文件。
    @State private var transcriptRefresh = 0

    @State private var selectedTab = 0
    @State private var settingsPath: [SettingsRoute] = []
    /// 状态行只在档位信息存在时更新；解析返回 nil（如 started）时保留上一次，
    /// 否则状态词会每秒闪一下空白。
    @State private var statusSummary: StatusSummary?
    /// 字幕样式存 UserDefaults：扩展和画中画都要读同一份，不能只活在主 App 内存里。
    @AppStorage("subtitleStyle") private var subtitleStyleData: Data = Data()

    private var subtitleStyle: SubtitleStyle {
        SubtitleStyle.load(from: subtitleStyleData.isEmpty ? nil : subtitleStyleData)
    }

    private var micStatusText: String {
        switch micCapture.state {
        case .idle:
            DesignTokens.Text.micStatusIdle
        case .running:
            DesignTokens.Text.micStatusRunning
        case .denied:
            DesignTokens.Text.micPermissionDenied
        case .blockedByBroadcast:
            DesignTokens.Text.micBlockedByBroadcast
        case .error(let message):
            message
        }
    }

    private var micStatusColor: Color {
        switch micCapture.state {
        case .running:
            .green
        case .idle:
            .secondary
        case .denied, .blockedByBroadcast, .error:
            .red
        }
    }

    var body: some View {
        wrapped(
            TabView(selection: $selectedTab) {
                NavigationStack {
                    // 内容比一屏高：必须能滚动。没有 ScrollView 时 VStack 居中溢出，顶部的 Broadcast 按钮、
                    // 配对输入框被裁到状态栏后面，用户既滑不动也点不到（iPhone 15 Pro 真机复现）。
                    ScrollView {
                        VStack(spacing: DesignTokens.Space.l) {
                            SubtitleCardView(
                                lines: linesMonitor.lines,
                                style: subtitleStyle,
                                status: statusSummary,
                                draft: linesMonitor.draft
                            )

                            // 本机引擎失败时把原始码映射成可读提示（纯逻辑，不删原状态行）。
                            if let localMsg = LocalEngineStatus.userMessage(forStatusLine: statusMonitor.keychainStatus) {
                                Text(localMsg)
                                    .font(.caption)
                                    .foregroundColor(.red)
                                    .frame(maxWidth: .infinity, alignment: .leading)
                            }

                            broadcastSection
                            micSection
                            pipSection

                            NavigationLink("查看今天的字幕") {
                                TranscriptView(store: transcriptStore, refreshToken: transcriptRefresh)
                            }
                            .buttonStyle(.bordered)

                            Spacer()
                        }
                        .padding()
                    }
                    .scrollDismissesKeyboard(.interactively)
                    .navigationTitle("实时字幕")
                    .onAppear {
                        self.loadPairedConfig()
                        self.loadCloudSettings()
                    }
                    // 相机扫配对码 / 点 rsnode://pair 链接：只把链接填进配对框，用户点「应用配对」才写入钥匙串。
                    // 顺手切到设置里的配对页，省得用户在字幕页找不到输入框。
                    .onOpenURL { url in
                        guard let text = PairingLink.prefillText(for: url) else { return }
                        self.pairingLinkText = text
                        self.pairingStatus = "已填入配对链接，确认后点「应用配对」"
                        self.selectedTab = 1
                        self.settingsPath = [.node]
                    }
                    .onReceive(linesMonitor.$lines) { lines in
                        self.transcriptStore.ingest(lines.map {
                            TranscriptStore.Sentence(id: $0.id, session: $0.session, src: $0.src, dst: $0.dst)
                        })
                        self.transcriptRefresh += 1
                    }
                }
                .tabItem {
                    Label("字幕", systemImage: "captions.bubble")
                }
                .tag(0)

                NavigationStack(path: $settingsPath) {
                    List {
                        NavigationLink("字幕样式", value: SettingsRoute.style)
                        NavigationLink("识别与语言", value: SettingsRoute.recognition)
                        NavigationLink("算力节点", value: SettingsRoute.node)
                        Section {
                            NavigationLink("诊断", value: SettingsRoute.diagnostics)
                        }
                    }
                    .navigationTitle("设置")
                    .navigationDestination(for: SettingsRoute.self) { route in
                        switch route {
                        case .style:
                            self.styleSettings
                        case .recognition:
                            self.recognitionSettings
                        case .node:
                            self.nodeSettings
                        case .diagnostics:
                            self.diagnostics
                        }
                    }
                }
                .tabItem {
                    Label("设置", systemImage: "gearshape")
                }
                .tag(1)
            }
            .onReceive(statusMonitor.$lastStatusUpdate) { updatedAt in
                self.micCapture.reconcileBroadcast(
                    extensionStatus: self.statusMonitor.keychainStatus,
                    extensionStatusUpdatedAt: updatedAt
                )
            }
            .onReceive(statusMonitor.$keychainStatus) { line in
                if let summary = StatusSummaryParser.summary(forStatusLine: line) {
                    self.statusSummary = summary
                }
            }
        )
    }

    @ViewBuilder
    private var broadcastSection: some View {
        VStack(spacing: DesignTokens.Space.m) {
            Text("屏幕广播")
                .font(.headline)
            BroadcastPickerView()
                .frame(width: 60, height: 60)
            Text("点上面的圆形按钮 → 选「实时字幕」→ 开始直播")
                .font(.footnote)
                .foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    @ViewBuilder
    private var micSection: some View {
        VStack(spacing: DesignTokens.Space.m) {
            Text(DesignTokens.Text.micSubtitleTitle)
                .font(.headline)
            HStack {
                Button(DesignTokens.Text.micStartButton) {
                    self.micCapture.start(
                        extensionStatus: self.statusMonitor.keychainStatus,
                        extensionStatusUpdatedAt: self.statusMonitor.lastStatusUpdate
                    )
                }
                .buttonStyle(.borderedProminent)
                .disabled(self.micCapture.isRunning)

                Button(DesignTokens.Text.micStopButton) {
                    self.micCapture.stop()
                }
                .buttonStyle(.bordered)
                .disabled(!self.micCapture.isRunning)
            }
            Text(self.micStatusText)
                .font(.caption)
                .foregroundStyle(self.micStatusColor)
                .frame(maxWidth: .infinity, alignment: .leading)
            if let speechDenied = self.micCapture.speechDeniedHint {
                Text(speechDenied)
                    .font(.caption)
                    .foregroundColor(.red)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    @ViewBuilder
    private var pipSection: some View {
        VStack(spacing: DesignTokens.Space.m) {
            Text("画中画")
                .font(.headline)

            PiPHostView(pipManager: self.pipManager)
                .frame(width: 320, height: 90)
                .background(Color(.systemGray6))
                .cornerRadius(8)

            Button {
                if self.pipManager.isPiPActive {
                    self.pipManager.stopPiP()
                } else {
                    self.pipManager.startPiP()
                }
            } label: {
                Text(self.pipManager.isPiPActive ? "关闭画中画" : "开启画中画")
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .disabled(self.pipManager.isStarting)

            if let error = self.pipManager.lastError {
                Text("Error: \(error)")
                    .font(.caption)
                    .foregroundColor(.red)
            }
        }
    }

    @ViewBuilder
    private var styleSettings: some View {
        Form {
            Section {
                SubtitleCardView(
                    lines: [
                        RecentLine(
                            id: 0,
                            src: "Guten Abend, meine Damen und Herren.",
                            dst: "女士们先生们，晚上好。",
                            session: "preview"
                        ),
                        RecentLine(
                            id: 1,
                            src: "Willkommen bei den Echtzeit-Untertiteln.",
                            dst: "",
                            session: "preview"
                        ),
                    ],
                    style: self.subtitleStyle,
                    status: self.statusSummary,
                    maxPairs: 2
                )
                .listRowInsets(EdgeInsets())
                .listRowBackground(Color.clear)
            }

            Section("字号") {
                HStack {
                    Slider(
                        value: self.fontSizeBinding,
                        in: DesignTokens.TypeScale.subtitleMin...DesignTokens.TypeScale.subtitleMax,
                        step: 1
                    )
                    Text("\(Int(self.subtitleStyle.fontSize))")
                        .font(.system(.body, design: .monospaced))
                        .frame(width: 40, alignment: .trailing)
                }
            }

            Section("原文颜色") {
                self.colorSwatches(
                    DesignTokens.Palette.text,
                    selectedHex: self.subtitleStyle.sourceHex
                ) { hex in
                    self.updateStyle { $0.sourceHex = hex }
                }
            }

            Section("译文颜色") {
                self.colorSwatches(
                    DesignTokens.Palette.text,
                    selectedHex: self.subtitleStyle.translationHex
                ) { hex in
                    self.updateStyle { $0.translationHex = hex }
                }
            }

            Section("背景颜色") {
                self.colorSwatches(
                    DesignTokens.Palette.background,
                    selectedHex: self.subtitleStyle.backgroundHex
                ) { hex in
                    self.updateStyle { $0.backgroundHex = hex }
                }
            }

            Section("背景不透明度") {
                Slider(value: self.backgroundOpacityBinding, in: 0.2...1.0)
            }

            Section {
                Button("恢复默认") {
                    self.subtitleStyleData = SubtitleStyle.standard.clamped().encoded() ?? Data()
                }
            }
        }
        .navigationTitle("字幕样式")
    }

    @ViewBuilder
    private var recognitionSettings: some View {
        Form {
            Section("语言") {
                Text("德语 → 中文")
                    .foregroundColor(.secondary)
            }

            Section("识别引擎") {
                Picker("引擎", selection: $engineMode) {
                    Text("自动").tag("auto")
                    Text("电脑节点").tag("node")
                    Text("本机").tag("local")
                }
                .pickerStyle(.segmented)
                .onChange(of: engineMode) { _, newValue in
                    // 档位只进 Keychain；写失败时不能让界面看起来像已经生效。
                    self.engineModeSaveFailed = !SharedKeychain.writeEngineMode(newValue)
                }
                .onAppear {
                    self.engineMode = SharedKeychain.readEngineMode()
                }

                if self.engineModeSaveFailed {
                    Text("保存引擎档位失败，请重试")
                        .font(.caption)
                        .foregroundColor(.red)
                }

                // 只有启动后真的换过档位才提示：否则麦克风一开就常驻这句话，
                // 用户会以为自己刚改了什么。
                if self.micCapture.isRunning,
                   let startedMode = self.micCapture.startedEngineMode,
                   startedMode != self.engineMode {
                    Text("麦克风停止后重新开始才生效")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }

                Button {
                    self.prepareLocalAssets()
                } label: {
                    HStack {
                        if self.isPreparing {
                            ProgressView().scaleEffect(0.8)
                        }
                        Text(self.isPreparing ? "准备中..." : "准备本机识别资源")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(self.isPreparing)

                if !self.prepareStatus.isEmpty {
                    Text(self.prepareStatus)
                        .font(.caption)
                        .foregroundColor(self.prepareStatus.hasPrefix("错误") || self.prepareStatus.hasPrefix("需要") ? .red : .green)
                }

                if let speechDenied = self.prepareSpeechDeniedHint {
                    Text(speechDenied)
                        .font(.caption)
                        .foregroundColor(.red)
                }
            }

            Section("云端识别（Groq）") {
                if self.groqKeyMasked.isEmpty {
                    SecureField("Groq API key", text: $groqKeyInput)
                        .textFieldStyle(.roundedBorder)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)

                    Button("保存 key") {
                        self.saveGroqKey()
                    }
                    .buttonStyle(.bordered)
                    .disabled(self.groqKeyInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                } else {
                    HStack {
                        Text("已保存：\(self.groqKeyMasked)")
                            .font(.caption)
                            .foregroundColor(.secondary)
                        Spacer()
                        Button("清除 key") {
                            self.clearGroqKey()
                        }
                        .buttonStyle(.bordered)
                    }
                }

                Toggle("启用云端识别", isOn: $cloudEnabled)
                    .onChange(of: cloudEnabled) { _, newValue in
                        SharedKeychain.writeCloudEnabled(newValue)
                    }

                if !self.cloudKeyStatus.isEmpty {
                    Text(self.cloudKeyStatus)
                        .font(.caption)
                        .foregroundColor(self.cloudKeyStatus.hasPrefix("已保存") ? .green : .red)
                }

                Text("开着时音频片段会发给 Groq；关掉后只走电脑节点和本机。")
                    .font(.caption)
                    .foregroundColor(.secondary)
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .navigationTitle("识别与语言")
    }

    @ViewBuilder
    private var nodeSettings: some View {
        Form {
            Section("节点配对") {
                HStack {
                    TextField("粘贴 rsnode://pair?... 链接", text: $pairingLinkText)
                        .textFieldStyle(.roundedBorder)
                        .autocapitalization(.none)
                        .disableAutocorrection(true)

                    Button("粘贴配对链接") {
                        if let pasteboard = UIPasteboard.general.string {
                            self.pairingLinkText = pasteboard
                        }
                    }
                    .buttonStyle(.bordered)
                }

                Button("应用配对") {
                    self.applyPairingLink()
                }
                .buttonStyle(.borderedProminent)
                .disabled(self.pairingLinkText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                if !self.pairingStatus.isEmpty {
                    Text(self.pairingStatus)
                        .font(.caption)
                        .foregroundColor(self.pairingStatus.hasPrefix("已配对") ? .green : .red)
                }

                if !self.pairedConfigDisplay.isEmpty {
                    Text("已配对：\(self.pairedConfigDisplay)")
                        .font(.caption)
                        .foregroundColor(.secondary)
                }
            }
        }
        .navigationTitle("算力节点")
    }

    @ViewBuilder
    private var diagnostics: some View {
        List {
            Section("App 状态") {
                Text("前后台：\(self.appStateMonitor.isInBackground ? "后台" : "前台")")
                    .foregroundColor(self.appStateMonitor.isInBackground ? .orange : .green)
                Text("PiP Possible：\(self.pipManager.isPictureInPicturePossible ? "Yes" : "No")")
                    .foregroundColor(self.pipManager.isPictureInPicturePossible ? .green : .red)
                Text(self.pipManager.pipStatusLine)
                    .font(.caption)
                    .foregroundColor(.secondary)
            }

            Section("Extension Status") {
                Text(self.statusMonitor.keychainStatus)
                    .font(.system(.body, design: .monospaced))
                    .foregroundColor(.secondary)
            }

            Section("最近句对（原始）") {
                if self.linesMonitor.lines.isEmpty {
                    Text("暂无字幕")
                        .font(.system(.body, design: .monospaced))
                        .foregroundColor(.secondary)
                } else {
                    ForEach(Array(self.linesMonitor.lines.enumerated()), id: \.offset) { _, line in
                        VStack(alignment: .leading, spacing: DesignTokens.Space.xs) {
                            Text("[\(line.id)] \(line.src)")
                                .font(.system(.body, design: .monospaced))
                            Text(line.dst.isEmpty ? "…" : line.dst)
                                .font(.system(.body, design: .monospaced))
                                .foregroundColor(.secondary)
                        }
                    }
                }
            }

            Section("Darwin Notifications") {
                Text("Tick: \(self.statusMonitor.darwinNotificationCount)  Lines: \(self.linesMonitor.darwinNotificationCount)")
                    .font(.system(.body, design: .monospaced))
                    .foregroundColor(.secondary)
            }
        }
        .navigationTitle("诊断")
    }

    private var fontSizeBinding: Binding<Double> {
        Binding(
            get: { self.subtitleStyle.fontSize },
            set: { newValue in self.updateStyle { $0.fontSize = newValue } }
        )
    }

    private var backgroundOpacityBinding: Binding<Double> {
        Binding(
            get: { self.subtitleStyle.backgroundOpacity },
            set: { newValue in self.updateStyle { $0.backgroundOpacity = newValue } }
        )
    }

    /// 所有样式修改都经过这里：先夹值再编码，坏值不会写进 UserDefaults 传给扩展。
    private func updateStyle(_ mutate: (inout SubtitleStyle) -> Void) {
        var next = self.subtitleStyle
        mutate(&next)
        self.subtitleStyleData = next.clamped().encoded() ?? Data()
    }

    @ViewBuilder
    private func colorSwatches(
        _ palette: [(name: String, hex: String)],
        selectedHex: String,
        onSelect: @escaping (String) -> Void
    ) -> some View {
        HStack(spacing: DesignTokens.Space.s) {
            ForEach(Array(palette.enumerated()), id: \.offset) { _, item in
                Button {
                    onSelect(item.hex)
                } label: {
                    Circle()
                        .fill(Color(styleHex: item.hex))
                        .frame(width: 28, height: 28)
                        .overlay(
                            Circle()
                                .strokeBorder(item.hex == selectedHex ? Color.primary : Color.clear, lineWidth: 2)
                        )
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.name)
            }
        }
    }

    /// `.translationTask` 是唯一能拿到 TranslationSession 的入口（普通 Task 拿不到），
    /// 所以翻译资源下载必须挂在这里；可用性判断集中在 View 结构上，body 不整体抬高到 iOS 18。
    @ViewBuilder
    private func wrapped<V: View>(_ content: V) -> some View {
        if #available(iOS 18.0, *) {
            content.translationTask(translationConfig as? TranslationSession.Configuration) { session in
                _ = try? await session.prepareTranslation()
                // prepareTranslation 正常返回不代表资源真的装上（用户可能取消系统下载），
                // 状态必须以再次查询 LanguageAvailability 的结果为准。
                let status = await LanguageAvailability().status(
                    from: Locale.Language(identifier: "de"),
                    to: Locale.Language(identifier: "zh-Hans")
                )
                await MainActor.run {
                    isPreparing = false
                    prepareStatus = "翻译 de→zh-Hans: \(status)"
                }
            }
        } else {
            content
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
        prepareSpeechDeniedHint = nil

        // 准备本机识别资源时顺带确认语音授权（不阻止流程）；拒绝提示单独存，
        // 不写进 prepareStatus，否则会被后面每一步的整段赋值覆盖。
        let mode = SharedKeychain.readEngineMode()
        if mode == "local" || mode == "auto" {
            switch SFSpeechRecognizer.authorizationStatus() {
            case .notDetermined:
                SFSpeechRecognizer.requestAuthorization { status in
                    DispatchQueue.main.async {
                        if status == .denied || status == .restricted {
                            prepareSpeechDeniedHint = DesignTokens.Text.localSpeechDenied
                        }
                    }
                }
            case .denied, .restricted:
                prepareSpeechDeniedHint = DesignTokens.Text.localSpeechDenied
            default:
                break
            }
        }

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

                var assetStatus = await AssetInventory.status(forModules: [transcriber])
                if assetStatus != .installed {
                    // 只查状态永远不会把资源装上：本机引擎在资源缺失时直接报 asr_assets_missing，
                    // 而界面又把用户指回这个按钮，等于死循环。这里必须真的发起安装。
                    await MainActor.run { prepareStatus = "正在下载识别资源..." }
                    if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                        try await request.downloadAndInstall()
                    }
                    // 安装结束后重新查一次：用户可能中途取消，状态以查询结果为准。
                    assetStatus = await AssetInventory.status(forModules: [transcriber])
                }

                let txStatus = await LanguageAvailability().status(
                    from: Locale.Language(identifier: "de"),
                    to: Locale.Language(identifier: "zh-Hans")
                )

                await MainActor.run {
                    var parts: [String] = ["ASR资源: \(assetStatus)"]
                    parts.append("翻译 de→zh-Hans: \(txStatus)")
                    prepareStatus = parts.joined(separator: " · ")
                }

                if assetStatus != .installed {
                    await MainActor.run {
                        prepareStatus += " · 识别资源仍未安装，请重试"
                    }
                }

                if txStatus != .installed {
                    // 翻译下载要弹系统确认框，只有 translationTask 能拿到会话：
                    // 这里只设置配置，真正的 prepareTranslation 在 wrapped(_:) 里执行。
                    await MainActor.run {
                        var next = TranslationSession.Configuration(
                            source: Locale.Language(identifier: "de"),
                            target: Locale.Language(identifier: "zh-Hans")
                        )
                        // 同一对语言连续点按钮时 Configuration 相等，修饰符不会重开会话；
                        // invalidate 保证这一次点击仍然触发系统下载确认。
                        next.invalidate()
                        translationConfig = next
                        isPreparing = true
                        prepareStatus += " · 等待系统确认下载翻译资源..."
                    }
                } else {
                    await MainActor.run { isPreparing = false }
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

/// 画中画走 Core Graphics，不能直接吃 SwiftUI 的 Color；颜色仍从同一份令牌/样式解析。
fileprivate func uiColor(hex: String, alpha: CGFloat) -> UIColor {
    guard let rgb = SubtitleStyle.rgb(hex: hex) else {
        return UIColor.white.withAlphaComponent(alpha)
    }
    return UIColor(red: CGFloat(rgb.r), green: CGFloat(rgb.g), blue: CGFloat(rgb.b), alpha: alpha)
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
    private var lastRenderedDraft: String = ""
    private var lastRenderedStatus: String = ""
    private var lastRenderedStyle: SubtitleStyle?
    private var lastStatusSummary: StatusSummary?

    private func renderSubtitleImage(status: String, time: String) -> UIImage? {
        var linesJSON = ""
        if let json = SharedKeychain.readLatestLines(),
           let data = json.data(using: .utf8),
           let lines = try? JSONDecoder().decode([RecentLine].self, from: data) {
            cachedLines = lines
            linesJSON = json
        }
        let draft = SharedKeychain.readLatestDraft() ?? ""

        // 样式也要参与变化判断：用户改颜色/字号后即使句子没变也得立刻重绘。
        let style = SubtitleStyle.load(from: UserDefaults.standard.data(forKey: "subtitleStyle"))

        if linesJSON == lastRenderedLinesJSON
            && draft == lastRenderedDraft
            && status == lastRenderedStatus
            && style == lastRenderedStyle {
            return nil
        }
        lastRenderedLinesJSON = linesJSON
        lastRenderedDraft = draft
        lastRenderedStatus = status
        lastRenderedStyle = style

        let displayLines = Array(cachedLines.suffix(2))

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

            let label = self.lastStatusSummary?.label ?? DesignTokens.Text.micStatusIdle
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
    /// 只有 Darwin 通知才代表扩展刚写过状态；轮询读到旧 Keychain 值不能延长广播占用时间。
    @Published private(set) var lastStatusUpdate: Date?

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

    private func updateKeychainStatus(markingFreshUpdate: Bool = false) {
        DispatchQueue.main.async {
            self.keychainStatus = SharedKeychain.readExtStatus() ?? "无"
            if markingFreshUpdate {
                self.lastStatusUpdate = Date()
            }
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
                monitor.updateKeychainStatus(markingFreshUpdate: true)
            }
        }, name, nil, .deliverImmediately)

        self.darwinObserver = observer
    }
}

final class LinesMonitor: ObservableObject {
    @Published var lines: [RecentLine] = []
    /// 未定稿草稿原文：与定稿句对分开，只给界面显示。
    @Published var draft: String = ""
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
        let draft = SharedKeychain.readLatestDraft() ?? ""
        DispatchQueue.main.async {
            if self.draft != draft {
                self.draft = draft
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
