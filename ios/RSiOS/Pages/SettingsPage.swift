import SwiftUI

/// 设置页：字幕样式 / 识别与语言 / 算力节点。诊断已经单独成一个标签页。
struct SettingsPage: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        NavigationStack(path: $model.settingsPath) {
            List {
                NavigationLink(value: SettingsRoute.style) {
                    Label("字幕样式", systemImage: "textformat.size")
                }
                NavigationLink(value: SettingsRoute.recognition) {
                    Label("识别与语言", systemImage: "waveform")
                }
                NavigationLink(value: SettingsRoute.node) {
                    Label("算力节点", systemImage: "desktopcomputer")
                }
            }
            .navigationTitle("设置")
            .navigationDestination(for: SettingsRoute.self) { route in
                switch route {
                case .style: StyleSettingsView()
                case .recognition: RecognitionSettingsView()
                case .node: NodeSettingsView()
                }
            }
        }
    }
}

private struct StyleSettingsView: View {
    @Environment(AppModel.self) private var model
    /// 样式存 UserDefaults：画中画也读同一份，不能只活在内存里。
    @AppStorage("subtitleStyle") private var subtitleStyleData = Data()

    private var style: SubtitleStyle {
        SubtitleStyle.load(from: subtitleStyleData.isEmpty ? nil : subtitleStyleData)
    }

    var body: some View {
        Form {
            Section {
                SubtitleCardView(
                    lines: [
                        RecentLine(id: 0, src: "Guten Abend, meine Damen und Herren.", dst: "女士们先生们，晚上好。", session: "preview"),
                        RecentLine(id: 1, src: "Willkommen bei den Echtzeit-Untertiteln.", dst: "", session: "preview"),
                    ],
                    style: style,
                    status: model.statusSummary,
                    maxPairs: 2
                )
                .listRowInsets(EdgeInsets())
                .listRowBackground(Color.clear)
            }

            Section("字号") {
                HStack {
                    Slider(
                        value: binding(\.fontSize),
                        in: DesignTokens.TypeScale.subtitleMin...DesignTokens.TypeScale.subtitleMax,
                        step: 1
                    )
                    Text("\(Int(style.fontSize))")
                        .monospacedDigit()
                        .frame(width: 40, alignment: .trailing)
                }
            }

            Section("原文颜色") {
                swatches(DesignTokens.Palette.text, selected: style.sourceHex) { hex in update { $0.sourceHex = hex } }
            }
            Section("译文颜色") {
                swatches(DesignTokens.Palette.text, selected: style.translationHex) { hex in update { $0.translationHex = hex } }
            }
            Section("背景颜色") {
                swatches(DesignTokens.Palette.background, selected: style.backgroundHex) { hex in update { $0.backgroundHex = hex } }
            }
            Section("背景不透明度") {
                Slider(value: binding(\.backgroundOpacity), in: 0.2...1.0)
            }
            Section {
                Button("恢复默认") {
                    subtitleStyleData = SubtitleStyle.standard.clamped().encoded() ?? Data()
                }
            }
        }
        .navigationTitle("字幕样式")
    }

    private func binding(_ keyPath: WritableKeyPath<SubtitleStyle, Double>) -> Binding<Double> {
        Binding(get: { style[keyPath: keyPath] }, set: { value in update { $0[keyPath: keyPath] = value } })
    }

    /// 所有修改都先夹值再编码：坏值不会写进 UserDefaults 传给画中画。
    private func update(_ mutate: (inout SubtitleStyle) -> Void) {
        var next = style
        mutate(&next)
        subtitleStyleData = next.clamped().encoded() ?? Data()
    }

    private func swatches(
        _ palette: [(name: String, hex: String)],
        selected: String,
        onSelect: @escaping (String) -> Void
    ) -> some View {
        HStack(spacing: DesignTokens.Space.s) {
            ForEach(palette, id: \.hex) { item in
                Button { onSelect(item.hex) } label: {
                    Circle()
                        .fill(Color(styleHex: item.hex))
                        .frame(width: 28, height: 28)
                        .overlay(Circle().strokeBorder(item.hex == selected ? Color.primary : .clear, lineWidth: 2))
                }
                .buttonStyle(.plain)
                .accessibilityLabel(item.name)
            }
        }
    }
}

private struct RecognitionSettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Form {
            Section("语言") {
                LabeledContent("识别 → 翻译", value: "德语 → 中文")
            }

            Section("识别引擎") {
                Picker("引擎", selection: $model.engineMode) {
                    Text("自动").tag("auto")
                    Text("电脑节点").tag("node")
                    Text(DesignTokens.Text.statusLocal).tag("local")
                }
                .pickerStyle(.segmented)
                .onChange(of: model.engineMode) { model.saveEngineMode() }

                if model.engineModeSaveFailed {
                    Text("保存引擎档位失败，请重试").font(.caption).foregroundStyle(.red)
                }
                if model.engineModeNeedsRestart {
                    Text("麦克风停止后重新开始才生效").font(.caption).foregroundStyle(.secondary)
                }

                Button {
                    model.assets.prepare()
                } label: {
                    HStack {
                        if model.assets.isPreparing { ProgressView() }
                        Text(model.assets.isPreparing ? "准备中..." : "准备本机识别资源")
                    }
                    .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.assets.isPreparing)

                if !model.assets.statusText.isEmpty {
                    Text(model.assets.statusText).font(.caption).foregroundStyle(.secondary)
                }
                if let denied = model.assets.speechDeniedHint {
                    Text(denied).font(.caption).foregroundStyle(.red)
                }
            }

            Section {
                if model.groqKeyMasked.isEmpty {
                    SecureField("Groq API key", text: $model.groqKeyInput)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("保存 key") { model.saveGroqKey() }
                        .disabled(model.groqKeyInput.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                } else {
                    LabeledContent("已保存", value: model.groqKeyMasked)
                    Button("清除 key", role: .destructive) { model.clearGroqKey() }
                }
                Toggle("启用云端识别", isOn: $model.cloudEnabled)
                    .onChange(of: model.cloudEnabled) { model.saveCloudEnabled() }
                if !model.cloudKeyStatus.isEmpty {
                    Text(model.cloudKeyStatus)
                        .font(.caption)
                        .foregroundStyle(model.cloudKeyStatus.hasPrefix("已保存") ? .green : .red)
                }
            } header: {
                Text("云端识别（Groq）")
            } footer: {
                Text("开着时音频片段会发给 Groq；关掉后只走电脑节点和本机。")
            }
        }
        .navigationTitle("识别与语言")
    }
}

private struct NodeSettingsView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        Form {
            Section("节点配对") {
                TextField("粘贴 rsnode://pair?... 链接", text: $model.pairingLinkText)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                HStack {
                    Button("粘贴配对链接") {
                        if let text = UIPasteboard.general.string { model.pairingLinkText = text }
                    }
                    .buttonStyle(.bordered)
                    Spacer()
                    Button("应用配对") { model.applyPairingLink() }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.pairingLinkText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
                if !model.pairingStatus.isEmpty {
                    Text(model.pairingStatus)
                        .font(.caption)
                        .foregroundStyle(model.pairingStatus.hasPrefix("已配对") ? .green : .red)
                }
                if !model.pairedConfigDisplay.isEmpty {
                    LabeledContent("已配对", value: model.pairedConfigDisplay)
                }
            }
        }
        .navigationTitle("算力节点")
    }
}
