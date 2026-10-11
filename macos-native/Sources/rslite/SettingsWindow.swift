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
        window.setContentSize(NSSize(width: 720, height: 680))
        window.contentMinSize = NSSize(width: 560, height: 480)
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
    @Published var mode: String
    @Published var languageMessage: String?
    @Published var nodeLoad: SettingsNodeLoad = .missing

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
        mode = loadedLanguage.mode
        savedLanguage = loadedLanguage
        keyConfigured = CloudConfigStore.readKey() != nil
        cloudEnabled = CloudConfigStore.readEnabled()
        nodeLoad = SettingsNodeList.load()
    }

    var languageDirty: Bool {
        source != savedLanguage.source
            || src != savedLanguage.src
            || dst != savedLanguage.dst
            || mode != savedLanguage.mode
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
        let next = AppSettings.normalized(AppSettings(source: source, src: src, dst: dst, mode: mode))
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

    private func applyLanguage(_ settings: AppSettings) {
        source = settings.source
        src = settings.src
        dst = settings.dst
        mode = settings.mode
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
                Picker("模式", selection: $model.mode) {
                    Text("自动").tag("auto")
                    Text("仅本机").tag("local")
                    Text("混合").tag("hybrid")
                }
                .pickerStyle(.radioGroup)
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
                    Text("还没有节点。")
                case .unreadable:
                    Text("节点列表无法读取。")
                case .loaded(let nodes):
                    ForEach(nodes) { node in
                        HStack(alignment: .firstTextBaseline) {
                            Text(node.id)
                                .font(.body.monospaced())
                            Spacer()
                            Text(node.host)
                                .foregroundStyle(.secondary)
                                .textSelection(.enabled)
                        }
                    }
                }
                Text("如何添加节点：bash scripts/node/install_node.sh <ssh 别名>")
                    .textSelection(.enabled)
            }
            .padding(20)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
        .onAppear { model.reloadNodes() }
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
            nodes.append(SettingsNodeSummary(id: id, host: hostName(from: urlString)))
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

private func settingsDescribe(_ error: Error) -> String {
    if let cloud = error as? CloudConfigError {
        return cloud.description
    }
    let text = error.localizedDescription.trimmingCharacters(in: .whitespacesAndNewlines)
    return text.isEmpty ? "保存失败" : text
}
