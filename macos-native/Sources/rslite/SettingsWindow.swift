import AppKit
import SwiftUI

/// 再次打开设置窗口时换成最新回调，样式页不会抓住第一次的闭包。
@MainActor
final class StyleChangeForwarder {
    var handler: ((OverlayStyle) -> Void)?
}

/// 标准设置窗口。单例：第一次 `open` 构造并注入 `onStyleChange`，之后只把同一扇窗口前置。
@MainActor
final class SettingsWindow {
    static let shared = SettingsWindow()

    private let forwarder = StyleChangeForwarder()
    private var window: NSWindow?
    private var model: SettingsViewModel?

    private init() {}

    static func open(onStyleChange: @escaping (OverlayStyle) -> Void) {
        shared.open(onStyleChange: onStyleChange)
    }

    func open(onStyleChange: @escaping (OverlayStyle) -> Void) {
        forwarder.handler = onStyleChange
        if let window {
            if !window.isVisible {
                model?.reloadFromDisk()
            }
            window.makeKeyAndOrderFront(nil)
            NSApp.activate()
            return
        }
        let model = SettingsViewModel { [forwarder] style in
            forwarder.handler?(style)
        }
        self.model = model
        let host = NSHostingController(rootView: SettingsRootView(model: model))
        let window = NSWindow(contentViewController: host)
        window.title = "rslite 设置"
        window.styleMask = [.titled, .closable, .miniaturizable, .resizable]
        window.setContentSize(NSSize(width: 960, height: 720))
        window.contentMinSize = NSSize(width: 720, height: 520)
        window.isReleasedWhenClosed = false
        window.tabbingMode = .disallowed
        window.center()
        self.window = window
        window.makeKeyAndOrderFront(nil)
        NSApp.activate()
    }
}

@MainActor
final class SettingsViewModel: ObservableObject {
    @Published var style: OverlayStyle
    @Published var styleError: String?
    @Published var keyDraft = ""
    @Published var keyConfigured = false
    @Published var keyError: String?
    @Published var cloudEnabled = false
    @Published var cloudError: String?
    @Published var quotaText = "打开本页时读取。"
    @Published var source: String
    @Published var src: String
    @Published var dst: String
    /// local / auto / pinned。指定节点对应 mode hybrid + pinnedNode。
    @Published var runChoice: String
    @Published var pinnedDraft: String
    @Published var languageMessage: String?
    @Published var nodeLoad: SettingsNodeLoad = .missing
    @Published var compareRows: [NodeCompareRow] = []
    @Published var probing = false

    private let onStyleChange: (OverlayStyle) -> Void
    private var savedLanguage: AppSettings

    init(onStyleChange: @escaping (OverlayStyle) -> Void) {
        let loadedStyle = OverlayStyleStore.load()
        let loadedLanguage = AppSettingsStore.load()
        self.onStyleChange = onStyleChange
        style = loadedStyle
        source = loadedLanguage.source
        src = loadedLanguage.src
        dst = loadedLanguage.dst
        runChoice = Self.runChoice(for: loadedLanguage)
        pinnedDraft = loadedLanguage.pinnedNode ?? ""
        savedLanguage = loadedLanguage
        keyConfigured = CloudConfigStore.readKey() != nil
        cloudEnabled = CloudConfigStore.readEnabled()
        nodeLoad = SettingsNodeList.load()
        compareRows = NodeCompareBoard.makeRows(nodes: Self.loadedNodes(nodeLoad), probes: [:], probing: false)
    }

    private var probeGeneration = 0

    var languageDirty: Bool {
        editedLanguage() != savedLanguage
    }

    var nodeChoices: [String] {
        var ids: [String] = []
        if case .loaded(let nodes) = nodeLoad {
            ids = nodes.map(\.id)
        }
        if !pinnedDraft.isEmpty, !ids.contains(pinnedDraft) {
            ids.append(pinnedDraft)
        }
        return ids
    }

    var cloudEnabledBinding: Binding<Bool> {
        Binding(get: { self.cloudEnabled }, set: { self.writeCloudEnabled($0) })
    }

    func reloadFromDisk() {
        style = OverlayStyleStore.load()
        styleError = nil
        let loadedLanguage = AppSettingsStore.load()
        applyLanguage(loadedLanguage)
        languageMessage = nil
        keyDraft = ""
        keyError = nil
        keyConfigured = CloudConfigStore.readKey() != nil
        cloudEnabled = CloudConfigStore.readEnabled()
        cloudError = nil
        nodeLoad = SettingsNodeList.load()
        reloadQuota()
    }

    func updateStyle(_ mutate: (inout OverlayStyle) -> Void) {
        var next = style
        mutate(&next)
        var current = style
        current.originX = nil
        current.originY = nil
        next.originX = nil
        next.originY = nil
        guard next != current else { return }
        style = next
        persistStyle(next)
    }

    func resetStyle() {
        var next = OverlayStyle.default
        var current = style
        current.originX = nil
        current.originY = nil
        next.originX = nil
        next.originY = nil
        guard next != current else { return }
        style = next
        persistStyle(next)
    }

    func saveKey() {
        do {
            try CloudConfigStore.writeKey(keyDraft)
            keyDraft = ""
            keyError = nil
            refreshCloudFlags()
        } catch {
            keyError = settingsDescribe(error)
            refreshCloudFlags()
        }
    }

    func clearKey() {
        do {
            try CloudConfigStore.clearKey()
            keyDraft = ""
            keyError = nil
            refreshCloudFlags()
        } catch {
            keyError = settingsDescribe(error)
            refreshCloudFlags()
        }
    }

    func reloadQuota() {
        let usage = QuotaLedger().usage()
        let limits = QuotaLedgerLimits()
        quotaText = [
            "用量 \(Self.percent(usage.usagePercent))",
            "1 分钟请求 \(usage.minuteRequests) / \(limits.requestsPerMinute)",
            "1 小时音频 \(Self.seconds(usage.hourAudioSeconds)) 秒 / \(Self.seconds(limits.audioSecondsPerHour)) 秒",
            "24 小时请求 \(usage.dayRequests) / \(limits.requestsPerDay)",
            "24 小时音频 \(Self.seconds(usage.dayAudioSeconds)) 秒 / \(Self.seconds(limits.audioSecondsPerDay)) 秒",
        ].joined(separator: "\n")
    }

    func saveLanguage() {
        let next = editedLanguage()
        do {
            try AppSettingsStore.save(next)
            applyLanguage(next)
            languageMessage = "已写入 settings.json。重启 rslite 后生效。"
        } catch {
            languageMessage = "保存失败：\(settingsDescribe(error))"
        }
    }

    func reloadNodes() {
        nodeLoad = SettingsNodeList.load()
    }

    /// 打开「算力节点」时调用。并发测每个节点的 /v1/info，每台 3 次取 RTT 中位数。
    func probeNodes() {
        reloadNodes()
        probeGeneration += 1
        let generation = probeGeneration
        let nodes = Self.loadedNodes(nodeLoad)
        probing = true
        compareRows = NodeCompareBoard.makeRows(nodes: nodes, probes: [:], probing: true)
        let configs = nodes.compactMap(\.config)
        guard !configs.isEmpty else {
            probing = false
            compareRows = NodeCompareBoard.makeRows(nodes: nodes, probes: [:], probing: false)
            return
        }
        guard #available(macOS 27.0, *) else {
            probing = false
            compareRows = NodeCompareBoard.makeRows(nodes: nodes, probes: [:], probing: false)
            return
        }
        Task { [weak self] in
            let measured = await NodeProber.measure(configs)
            await MainActor.run {
                guard let self, self.probeGeneration == generation else { return }
                self.probing = false
                self.compareRows = NodeCompareBoard.makeRows(
                    nodes: nodes,
                    probes: Dictionary(uniqueKeysWithValues: measured),
                    probing: false
                )
            }
        }
    }

    private func editedLanguage() -> AppSettings {
        let mode: String
        let pin: String?
        switch runChoice {
        case "local":
            mode = "local"
            pin = nil
        case "auto":
            mode = "auto"
            pin = nil
        default:
            mode = "hybrid"
            pin = pinnedDraft
        }
        return AppSettings.normalized(AppSettings(source: source, src: src, dst: dst, mode: mode, pinnedNode: pin))
    }

    private static func runChoice(for settings: AppSettings) -> String {
        if settings.mode == "local" {
            return "local"
        }
        if settings.mode == "hybrid" {
            return "pinned"
        }
        return "auto"
    }

    private static func loadedNodes(_ load: SettingsNodeLoad) -> [SettingsNodeSummary] {
        if case .loaded(let nodes) = load {
            return nodes
        }
        return []
    }

    private func applyLanguage(_ settings: AppSettings) {
        source = settings.source
        src = settings.src
        dst = settings.dst
        runChoice = Self.runChoice(for: settings)
        pinnedDraft = settings.pinnedNode ?? ""
        savedLanguage = settings
    }

    private func refreshCloudFlags() {
        keyConfigured = CloudConfigStore.readKey() != nil
        cloudEnabled = CloudConfigStore.readEnabled()
    }

    private func writeCloudEnabled(_ enabled: Bool) {
        cloudEnabled = enabled
        do {
            try CloudConfigStore.writeEnabled(enabled)
            cloudError = nil
        } catch {
            cloudError = settingsDescribe(error)
            cloudEnabled = CloudConfigStore.readEnabled()
        }
    }

    /// 窗口位置沿用磁盘上的 origin，避免改字号时把悬浮窗拽回默认位置。
    private func persistStyle(_ next: OverlayStyle) {
        var writing = next
        let disk = OverlayStyleStore.load()
        writing.originX = disk.originX
        writing.originY = disk.originY
        do {
            try OverlayStyleStore.save(writing)
            styleError = nil
            onStyleChange(writing)
        } catch {
            styleError = "无法写入样式：\(settingsDescribe(error))"
            style = disk
        }
    }

    private static func percent(_ value: Double) -> String {
        String(format: "%.0f%%", value.isFinite ? value : 0)
    }

    private static func seconds(_ value: Double) -> String {
        guard value.isFinite else { return "0" }
        if abs(value - value.rounded()) < 0.05 {
            return String(Int(value.rounded()))
        }
        return String(format: "%.1f", value)
    }
}

struct SettingsRootView: View {
    @ObservedObject var model: SettingsViewModel

    var body: some View {
        TabView {
            StyleSettingsTab(model: model)
                .tabItem { Text("字幕样式") }
            CloudSettingsTab(model: model)
                .tabItem { Text("云端识别") }
            LanguageSettingsTab(model: model)
                .tabItem { Text("识别与语言") }
            NodesSettingsTab(model: model)
                .tabItem { Text("算力节点") }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct StyleSettingsTab: View {
    @ObservedObject var model: SettingsViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 18) {
                SubtitleSamplePreview(style: model.style)
                    .padding(12)
                    .frame(maxWidth: .infinity)
                    .background(
                        RoundedRectangle(cornerRadius: 16)
                            .fill(Color.gray.opacity(0.18))
                    )
                Text("悬浮窗保留最近 \(model.style.lines) 对。预览是一对示例。")
                    .font(.callout)
                    .foregroundStyle(.secondary)

                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("字号")
                        Spacer()
                        Text("\(Int(model.style.fontSize.rounded()))")
                            .monospacedDigit()
                    }
                    Slider(
                        value: Binding(
                            get: { model.style.fontSize },
                            set: { value in
                                model.updateStyle { $0.fontSize = min(72, max(16, value.rounded())) }
                            }
                        ),
                        in: 16...72,
                        step: 1
                    )
                }

                colorRow("原文颜色", hex: model.style.sourceColor, presets: OverlayStyle.textColorPresets) { hex in
                    model.updateStyle { $0.sourceColor = hex }
                }
                colorRow("译文颜色", hex: model.style.translationColor, presets: OverlayStyle.textColorPresets) { hex in
                    model.updateStyle { $0.translationColor = hex }
                }
                colorRow("背景色", hex: model.style.backgroundColor, presets: OverlayStyle.backgroundColorPresets) { hex in
                    model.updateStyle { $0.backgroundColor = hex }
                }

                VStack(alignment: .leading, spacing: 6) {
                    HStack {
                        Text("透明度")
                        Spacer()
                        Text("\(Int((model.style.backgroundOpacity * 100).rounded()))%")
                            .monospacedDigit()
                    }
                    Slider(
                        value: Binding(
                            get: { model.style.backgroundOpacity },
                            set: { value in
                                let clamped = min(1, max(0, value))
                                model.updateStyle { $0.backgroundOpacity = (clamped * 100).rounded() / 100 }
                            }
                        ),
                        in: 0...1,
                        step: 0.01
                    )
                }

                rawPicker("文字边缘", selection: model.style.textEdge.rawValue, choices: OverlayStyle.textEdgeChoices.map { ($0.title, $0.value.rawValue) }) { raw in
                    guard let edge = TextEdge(rawValue: raw) else { return }
                    model.updateStyle { $0.textEdge = edge }
                }
                rawPicker("形状", selection: model.style.shape.rawValue, choices: OverlayStyle.shapeChoices.map { ($0.title, $0.value.rawValue) }) { raw in
                    guard let shape = OverlayShape(rawValue: raw) else { return }
                    model.updateStyle { $0.shape = shape }
                }
                rawPicker("行数", selection: String(model.style.lines), choices: (1...4).map { ("\($0)", String($0)) }) { raw in
                    guard let lines = Int(raw), (1...4).contains(lines) else { return }
                    model.updateStyle { $0.lines = lines }
                }
                rawPicker("显示模式", selection: model.style.displayMode.rawValue, choices: OverlayStyle.displayModeChoices.map { ($0.title, $0.value.rawValue) }) { raw in
                    guard let mode = DisplayMode(rawValue: raw) else { return }
                    model.updateStyle { $0.displayMode = mode }
                }

                Button("恢复默认") {
                    model.resetStyle()
                }
                if let styleError = model.styleError {
                    Text(styleError)
                        .foregroundStyle(.red)
                }
                Text("改动立即写入 style.json。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func colorRow(
        _ title: String,
        hex: String,
        presets: [(title: String, hex: String)],
        set: @escaping (String) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            ColorPicker(
                title,
                selection: Binding(
                    get: { SettingsColor.color(hex: hex) },
                    set: { set(SettingsColor.hex(from: $0)) }
                ),
                supportsOpacity: false
            )
            HStack(spacing: 6) {
                ForEach(presets, id: \.hex) { preset in
                    Button(preset.title) {
                        set(preset.hex)
                    }
                    .buttonStyle(.bordered)
                    .tint(OverlayStyle.canonicalHex(hex) == preset.hex ? Color.accentColor : nil)
                }
            }
        }
    }

    private func rawPicker(
        _ title: String,
        selection: String,
        choices: [(title: String, value: String)],
        set: @escaping (String) -> Void
    ) -> some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(title)
            Picker(title, selection: Binding(get: { selection }, set: { set($0) })) {
                ForEach(choices, id: \.value) { choice in
                    Text(choice.title).tag(choice.value)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
        }
    }
}

struct SubtitleSamplePreview: View {
    let style: OverlayStyle

    private static let sourceSample = "Guten Abend, meine Damen und Herren."
    private static let translationSample = "女士们先生们，晚上好。"

    var body: some View {
        let rgb = OverlayStyle.parseHex(style.backgroundColor) ?? (r: 0, g: 0, b: 0)
        VStack(alignment: .leading, spacing: 2) {
            if style.displayMode != .translationOnly {
                sampleLine(Self.sourceSample, hex: style.sourceColor)
            }
            if style.displayMode != .sourceOnly {
                sampleLine(Self.translationSample, hex: style.translationColor)
            }
        }
        .padding(.horizontal, style.shape == .strip ? 8 : 18)
        .padding(.vertical, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            Color(red: rgb.r, green: rgb.g, blue: rgb.b).opacity(style.backgroundOpacity)
        )
        .clipShape(RoundedRectangle(cornerRadius: style.cornerRadius, style: .continuous))
    }

    private func sampleLine(_ text: String, hex: String) -> some View {
        Text(text)
            .font(.system(size: style.fontSize, weight: .semibold))
            .foregroundStyle(SettingsColor.color(hex: hex))
            .modifier(SampleTextEdge(edge: style.textEdge))
            .fixedSize(horizontal: false, vertical: true)
    }
}

private struct SampleTextEdge: ViewModifier {
    let edge: TextEdge

    func body(content: Content) -> some View {
        switch edge {
        case .none:
            content
        case .shadow:
            content.shadow(color: .black.opacity(0.9), radius: 3, x: 0, y: 0)
        case .outline:
            content
                .shadow(color: .black, radius: 0, x: 0.8, y: 0)
                .shadow(color: .black, radius: 0, x: -0.8, y: 0)
                .shadow(color: .black, radius: 0, x: 0, y: 0.8)
                .shadow(color: .black, radius: 0, x: 0, y: -0.8)
        }
    }
}

enum SettingsColor {
    static func color(hex: String) -> Color {
        guard let rgb = OverlayStyle.parseHex(hex) else { return Color.white }
        return Color(.sRGB, red: rgb.r, green: rgb.g, blue: rgb.b, opacity: 1)
    }

    static func hex(from color: Color) -> String {
        let resolved = NSColor(color).usingColorSpace(.sRGB)
        let red = min(1, max(0, resolved?.redComponent ?? 1))
        let green = min(1, max(0, resolved?.greenComponent ?? 1))
        let blue = min(1, max(0, resolved?.blueComponent ?? 1))
        let formatted = String(
            format: "#%02X%02X%02X",
            Int((red * 255).rounded()),
            Int((green * 255).rounded()),
            Int((blue * 255).rounded())
        )
        return OverlayStyle.canonicalHex(formatted) ?? formatted
    }
}

struct CloudSettingsTab: View {
    @ObservedObject var model: SettingsViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text(model.keyConfigured ? "已配置" : "未配置")
                    .font(.headline)
                SecureField("输入 Groq key", text: $model.keyDraft)
                    .textFieldStyle(.roundedBorder)
                HStack(spacing: 8) {
                    Button("保存") { model.saveKey() }
                    Button("清除") { model.clearKey() }
                }
                if let keyError = model.keyError {
                    Text(keyError)
                        .foregroundStyle(.red)
                }
                Toggle("启用云端识别", isOn: model.cloudEnabledBinding)
                if let cloudError = model.cloudError {
                    Text(cloudError)
                        .foregroundStyle(.red)
                }
                VStack(alignment: .leading, spacing: 8) {
                    Text("只有在程序配置里填了 Groq key 才会把音频发送到云端识别服务。不读环境变量（避免因系统变量意外开启上传）；可在设置面板或命令行一键关掉云端开关。")
                    Text("若网络经由代理，经代理时代理可见上传的原始音频片段（WAV）。")
                    Text("关于第三方数据保留：[Groq 官方数据政策](https://console.groq.com/docs/your-data) 默认不保留识别请求；排查故障或滥用时可能临时保存最多 30 天。介意的话可在 Groq 控制台 Data Controls 打开 Zero Data Retention（ZDR）。")
                }
                .font(.callout)
                .foregroundStyle(.secondary)
                Text("本地额度（只读）")
                    .font(.headline)
                Text(model.quotaText)
                    .font(.body.monospacedDigit())
                    .fixedSize(horizontal: false, vertical: true)
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear { model.reloadQuota() }
    }
}

struct LanguageSettingsTab: View {
    @ObservedObject var model: SettingsViewModel

    private static let pairs: [(title: String, src: String, dst: String)] = [
        ("de-DE → zh-Hans", "de-DE", "zh-Hans"),
        ("en-US → zh-Hans", "en-US", "zh-Hans"),
        ("zh-CN → de-DE", "zh-CN", "de-DE"),
    ]

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                Text("重启 rslite 后生效")
                    .font(.headline)
                Picker("音频来源", selection: $model.source) {
                    Text("系统声音").tag("tap")
                    Text("麦克风").tag("mic")
                    if model.source != "tap" && model.source != "mic" {
                        Text("自定义").tag(model.source)
                    }
                }
                .pickerStyle(.radioGroup)
                if model.source != "tap" && model.source != "mic" {
                    Text(model.source)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                        .textSelection(.enabled)
                }

                Text("常用语言对")
                HStack(spacing: 8) {
                    ForEach(Self.pairs, id: \.title) { pair in
                        Button(pair.title) {
                            model.src = pair.src
                            model.dst = pair.dst
                        }
                        .buttonStyle(.bordered)
                        .tint(model.src == pair.src && model.dst == pair.dst ? Color.accentColor : nil)
                    }
                }
                Text("源语言")
                TextField("例如 de-DE", text: $model.src)
                    .textFieldStyle(.roundedBorder)
                Text("目标语言")
                TextField("例如 zh-Hans", text: $model.dst)
                    .textFieldStyle(.roundedBorder)
                Text("运行模式")
                Picker("运行模式", selection: $model.runChoice) {
                    Text("仅本机（默认）").tag("local")
                    Text("自动选最快").tag("auto")
                    Text("指定节点").tag("pinned")
                }
                .pickerStyle(.radioGroup)
                .labelsHidden()
                if model.runChoice == "pinned" {
                    if model.nodeChoices.isEmpty {
                        Text("nodes.json 里还没有节点。指定节点需要先添加。")
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    } else {
                        Picker("节点", selection: $model.pinnedDraft) {
                            Text("请选择").tag("")
                            ForEach(model.nodeChoices, id: \.self) { id in
                                Text(id).tag(id)
                            }
                        }
                    }
                    Text("选定后重启才会钉住这一台。不选具体节点则仍按混合路由。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Text("重启 rslite 后生效")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                HStack(spacing: 8) {
                    Button("保存") { model.saveLanguage() }
                    if model.languageDirty {
                        Text("尚未保存")
                            .foregroundStyle(.secondary)
                    }
                }
                if let languageMessage = model.languageMessage {
                    Text(languageMessage)
                        .foregroundStyle(languageMessage.hasPrefix("保存失败") ? Color.red : Color.secondary)
                }
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

struct NodesSettingsTab: View {
    @ObservedObject var model: SettingsViewModel

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 12) {
                switch model.nodeLoad {
                case .missing, .empty:
                    Text("还没有节点。下面只列出本机。")
                case .unreadable:
                    Text("节点列表无法读取。")
                case .loaded:
                    EmptyView()
                }
                HStack {
                    Button(model.probing ? "测速中…" : "测速") {
                        model.probeNodes()
                    }
                    .disabled(model.probing)
                    Text("打开这一页时会自动测一次。")
                        .font(.callout)
                        .foregroundStyle(.secondary)
                }
                Table(model.compareRows) {
                    TableColumn("名称") { row in
                        Text(row.isFastest ? "\(row.name) 最快" : row.name)
                    }
                    TableColumn("识别模型") { row in
                        Text(row.model)
                    }
                    TableColumn("翻译") { row in
                        Text(row.translation)
                    }
                    TableColumn("延迟") { row in
                        Text(row.latency).monospacedDigit()
                    }
                    TableColumn("rtf") { row in
                        Text(row.rtf).monospacedDigit()
                    }
                    TableColumn("状态") { row in
                        Text(row.status)
                    }
                    TableColumn("预计") { row in
                        Text(row.estimate).monospacedDigit()
                    }
                }
                .frame(minHeight: 220)
                Text("预计每句耗时 ≈ RTT + rtf×4s（RTT 用秒，表里的延迟是毫秒）。这是估算，不是实测整句耗时。本机没有网络延迟，延迟显示为 —。")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                Text("如何添加节点：bash scripts/node/install_node.sh <ssh 别名>")
                    .textSelection(.enabled)
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear { model.probeNodes() }
    }
}

enum SettingsNodeLoad: Equatable {
    case missing
    case empty
    case unreadable
    case loaded([SettingsNodeSummary])
}

struct SettingsNodeSummary: Identifiable, Equatable {
    var id: String
    var host: String
    /// 测速用。界面不展示 token 文件路径，也不读文件内容。
    var config: NodeConfig?
}

enum SettingsNodeList {
    static func load(from url: URL = CloudConfigStore.defaultDirectory.appendingPathComponent("nodes.json")) -> SettingsNodeLoad {
        guard FileManager.default.fileExists(atPath: url.path) else { return .missing }
        guard let data = try? Data(contentsOf: url),
              let rows = try? JSONSerialization.jsonObject(with: data) as? [Any] else {
            return .unreadable
        }
        var seen = Set<String>()
        var nodes: [SettingsNodeSummary] = []
        for row in rows {
            guard let object = row as? [String: Any] else { continue }
            let alias = (object["id"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let nodeID = (object["node_id"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let id = alias.isEmpty ? nodeID : alias
            guard !id.isEmpty, seen.insert(id).inserted else { continue }
            let urlString = object["url"] as? String ?? ""
            let tokenFile = (object["token_file"] as? String)?.trimmingCharacters(in: .whitespacesAndNewlines) ?? ""
            let config: NodeConfig?
            if !nodeID.isEmpty, !urlString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !tokenFile.isEmpty {
                config = NodeConfig(id: id, nodeID: nodeID, url: urlString, tokenFile: tokenFile)
            } else {
                config = nil
            }
            nodes.append(SettingsNodeSummary(id: id, host: hostName(from: urlString), config: config))
        }
        return nodes.isEmpty ? .empty : .loaded(nodes)
    }

    static func hostName(from urlString: String) -> String {
        let trimmed = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let host = URL(string: trimmed)?.host, !host.isEmpty else {
            return "（无主机名）"
        }
        return host
    }
}

struct NodeCompareRow: Identifiable, Equatable {
    var id: String
    var name: String
    var model: String
    var translation: String
    var latency: String
    var rtf: String
    var status: String
    var estimate: String
    var estimateSeconds: Double?
    var isFastest: Bool
}

enum NodeCompareBoard {
    static let localID = "__rslite_local__"
    /// 估算用的一句时长。界面写明这不是实测整句耗时。
    static let sentenceSeconds = 4.0

    /// 预计每句耗时（秒）≈ RTT（秒）+ rtf × 4。缺 RTT 或 rtf 时不估算。
    static func estimateSeconds(rttMS: Double?, rtf: Double?) -> Double? {
        guard let rttMS, let rtf, rttMS.isFinite, rtf.isFinite, rttMS >= 0, rtf >= 0 else {
            return nil
        }
        return rttMS / 1000 + rtf * sentenceSeconds
    }

    static func modelText(model: String?, local: Bool) -> String {
        if local { return "Apple 语音识别" }
        guard let model, !model.isEmpty else { return "—" }
        return model
    }

    static func translationText(translator: String?, translatorOK: Bool?, local: Bool) -> String {
        if local { return "Apple 翻译" }
        guard let translator, !translator.isEmpty else { return "—" }
        let state = translatorOK == false ? "离线" : "在线"
        return "\(translator) · \(state)"
    }

    static func latencyText(_ rttMS: Double?, local: Bool) -> String {
        if local { return "—" }
        guard let rttMS, rttMS.isFinite else { return "—" }
        return "\(Int(rttMS.rounded())) ms"
    }

    static func rtfText(_ rtf: Double?) -> String {
        guard let rtf, rtf.isFinite else { return "—" }
        return String(format: "%.2f", rtf)
    }

    static func estimateText(_ seconds: Double?) -> String {
        guard let seconds, seconds.isFinite else { return "—" }
        return String(format: "预计 %.2fs", seconds)
    }

    static func makeRows(
        nodes: [SettingsNodeSummary],
        probes: [String: ProbeResult],
        probing: Bool
    ) -> [NodeCompareRow] {
        var rows = [localRow()]
        for node in nodes {
            rows.append(nodeRow(node, probe: probes[node.id], probing: probing && probes[node.id] == nil))
        }
        return ordered(rows)
    }

    static func localRow() -> NodeCompareRow {
        NodeCompareRow(
            id: localID,
            name: DesignTokens.Text.statusLocal,
            model: modelText(model: nil, local: true),
            translation: translationText(translator: nil, translatorOK: nil, local: true),
            latency: "—",
            rtf: "—",
            status: DesignTokens.Text.statusLocal,
            estimate: "—",
            estimateSeconds: nil,
            isFastest: false
        )
    }

    static func nodeRow(_ node: SettingsNodeSummary, probe: ProbeResult?, probing: Bool) -> NodeCompareRow {
        let info = probe?.info
        let seconds = estimateSeconds(rttMS: probe?.rttMS, rtf: info?.asr.rtf)
        let status: String
        if let probe {
            status = probe.availability.title
        } else if node.config == nil {
            status = ProbeResult.Availability.offline.title
        } else if probing {
            status = "测速中"
        } else {
            status = "—"
        }
        return NodeCompareRow(
            id: node.id,
            name: node.id,
            model: modelText(model: info?.asr.model, local: false),
            translation: translationText(translator: info?.translator, translatorOK: info?.translatorOK, local: false),
            latency: latencyText(probe?.rttMS, local: false),
            rtf: rtfText(info?.asr.rtf),
            status: status,
            estimate: estimateText(seconds),
            estimateSeconds: seconds,
            isFastest: false
        )
    }

    /// 有估算的按耗时升序，相同耗时按 id。没有估算的排在后面，本机行靠前。只标一台最快。
    static func ordered(_ rows: [NodeCompareRow]) -> [NodeCompareRow] {
        let fastest = fastestID(rows)
        let marked = rows.map { row in
            var copy = row
            copy.isFastest = row.id == fastest
            return copy
        }
        return marked.sorted { lhs, rhs in
            switch (lhs.estimateSeconds, rhs.estimateSeconds) {
            case let (left?, right?) where left != right:
                return left < right
            case let (left?, right?) where left == right:
                return lhs.id < rhs.id
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            default:
                if lhs.id == localID { return true }
                if rhs.id == localID { return false }
                return lhs.id < rhs.id
            }
        }
    }

    static func fastestID(_ rows: [NodeCompareRow]) -> String? {
        let ranked = rows.compactMap { row -> (String, Double)? in
            guard let seconds = row.estimateSeconds else { return nil }
            return (row.id, seconds)
        }
        return ranked.min { lhs, rhs in
            if lhs.1 != rhs.1 { return lhs.1 < rhs.1 }
            return lhs.0 < rhs.0
        }?.0
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        check(estimateSeconds(rttMS: 1000, rtf: 1) == 5, "预计每句耗时 = RTT秒 + rtf×4")
        check(estimateSeconds(rttMS: 125, rtf: 0.25) == 1.125, "125ms + rtf 0.25×4s = 1.125s")
        check(estimateSeconds(rttMS: nil, rtf: 1) == nil, "没有 RTT 不估算")
        check(estimateSeconds(rttMS: 10, rtf: nil) == nil, "没有 rtf 不估算")
        check(estimateSeconds(rttMS: -1, rtf: 0.1) == nil, "负 RTT 不估算")

        let secret = "/tmp/secret-token-value"
        let info = NodeInfo(
            v: 2, nodeID: "n2", hwHash: "h",
            asr: .init(model: "whisper-large-v3-turbo", backend: "mlx", rtf: 0.1),
            translator: "apple", busy: false, onAC: true, inUse: false, worker: "warm",
            translatorOK: true
        )
        let slow = NodeInfo(
            v: 2, nodeID: "n3", hwHash: "h",
            asr: .init(model: "whisper-large-v3", backend: "mlx", rtf: 0.5),
            translator: "ollama:qwen", busy: true, onAC: true, inUse: false, worker: "warm",
            translatorOK: false
        )
        let mini = SettingsNodeSummary(
            id: "mini2",
            host: "mini2",
            config: NodeConfig(id: "mini2", nodeID: "n2", url: "ws://mini2:8791", tokenFile: secret)
        )
        let win = SettingsNodeSummary(
            id: "win",
            host: "win",
            config: NodeConfig(id: "win", nodeID: "n3", url: "ws://win:8791", tokenFile: secret)
        )
        let rows = makeRows(
            nodes: [win, mini],
            probes: [
                "mini2": ProbeResult(info: info, rttMS: 50, availability: .idle),
                "win": ProbeResult(info: slow, rttMS: 100, availability: .busy),
            ],
            probing: false
        )
        check(rows.first?.id == "mini2" && rows.first?.isFastest == true, "最快只标一台，且按预计耗时排在最前")
        check(rows.filter(\.isFastest).count == 1, "相同比较里只有一台标最快")
        let local = rows.first { $0.id == localID }
        check(local?.name == DesignTokens.Text.statusLocal && local?.model == "Apple 语音识别" && local?.translation == "Apple 翻译", "本机行写 Apple 语音识别和 Apple 翻译")
        check(local?.latency == "—" && local?.estimateSeconds == nil && local?.isFastest == false, "本机没有 RTT 时不参与最快")
        let miniRow = rows.first { $0.id == "mini2" }
        check(miniRow?.model == "whisper-large-v3-turbo", "节点识别模型取 asr.model")
        check(miniRow?.translation == "apple · 在线", "translator_ok 为真时翻译显示在线")
        check(miniRow?.latency == "50 ms" && miniRow?.rtf == "0.10" && miniRow?.status == "空闲", "节点行显示延迟、rtf 和空闲")
        let winRow = rows.first { $0.id == "win" }
        check(winRow?.translation == "ollama:qwen · 离线" && winRow?.status == "忙", "translator_ok 为假时翻译显示离线")
        let blob = rows.map { "\($0.name) \($0.model) \($0.translation) \($0.latency) \($0.status)" }.joined(separator: "\n")
        check(!blob.contains(secret) && !blob.contains("token"), "对比行不显示 token 或 token 文件")
        let tieA = NodeCompareRow(
            id: "b", name: "b", model: "m", translation: "t", latency: "1 ms", rtf: "0.10",
            status: "空闲", estimate: "预计 1.00s", estimateSeconds: 1, isFastest: false
        )
        let tieB = NodeCompareRow(
            id: "a", name: "a", model: "m", translation: "t", latency: "1 ms", rtf: "0.10",
            status: "空闲", estimate: "预计 1.00s", estimateSeconds: 1, isFastest: false
        )
        let tied = ordered([tieA, tieB])
        check(tied.first?.id == "a" && tied.first?.isFastest == true && tied.filter(\.isFastest).count == 1, "耗时相同按 id 只标一台")
        let missingRTF = translationText(translator: "apple", translatorOK: nil, local: false)
        check(missingRTF == "apple · 在线", "translator_ok 缺失按在线")
        return failures
    }
}

@available(macOS 27.0, *)
enum NodeProber {
    static func measure(_ configs: [NodeConfig]) async -> [(String, ProbeResult)] {
        await withTaskGroup(of: (String, ProbeResult).self) { group in
            for config in configs {
                group.addTask {
                    let result = await sample(config)
                    return (config.id, result)
                }
            }
            var measured: [(String, ProbeResult)] = []
            for await item in group {
                measured.append(item)
            }
            return measured
        }
    }

    static func sample(_ config: NodeConfig) async -> ProbeResult {
        await withTaskGroup(of: ProbeResult.self) { group in
            for _ in 0..<ProbeResult.samplesPerNode {
                group.addTask {
                    await NodeClient.probeInfo(config: config, timeout: ProbeResult.timeout)
                }
            }
            var results: [ProbeResult] = []
            for await result in group {
                results.append(result)
            }
            return ProbeResult.combined(results)
        }
    }
}

private func settingsDescribe(_ error: Error) -> String {
    if let cloud = error as? CloudConfigError {
        return cloud.description
    }
    let text = error.localizedDescription.trimmingCharacters(in: .whitespacesAndNewlines)
    return text.isEmpty ? "保存失败" : text
}
