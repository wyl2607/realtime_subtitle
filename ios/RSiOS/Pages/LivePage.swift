import SwiftUI

/// 字幕页：看字幕、开始/停止。
///
/// 顺序按用户要做的事排：先看有没有阻碍（资源缺失），再看字幕，最后是两种音源和画中画。
/// 画中画是显示出口不是音源，单独一组，免得被当成第三种开始方式。
struct LivePage: View {
    @Environment(AppModel.self) private var model
    @AppStorage("subtitleStyle") private var subtitleStyleData = Data()

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(spacing: DesignTokens.Space.l) {
                    if model.assets.needsAttention {
                        AssetBanner()
                    }

                    // 字幕区固定占屏幕约一半高：句子在里面往上滚，下面的按钮不会被一起推走。
                    LiveCaptionsView(
                        lines: model.captionLines,
                        style: SubtitleStyle.load(from: subtitleStyleData.isEmpty ? nil : subtitleStyleData),
                        status: model.statusSummary,
                        draft: model.feed.draft
                    )
                    .containerRelativeFrame(.vertical) { height, _ in height * 0.5 }

                    // 本机引擎失败时把原始码映射成可读提示。
                    if let message = LocalEngineStatus.userMessage(forStatusLine: model.feed.status) {
                        Label(message, systemImage: "exclamationmark.triangle.fill")
                            .font(.callout)
                            .foregroundStyle(.red)
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }

                    GroupBox("屏幕广播") {
                        HStack(spacing: DesignTokens.Space.m) {
                            BroadcastPickerView()
                                .frame(width: 60, height: 60)
                            Text("点圆形按钮 → 选「实时字幕」→ 开始直播，然后切到要看的 App")
                                .font(.footnote)
                                .foregroundStyle(.secondary)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }

                    MicSection()

                    PiPSection()
                }
                .padding()
            }
            .navigationTitle("实时字幕")
        }
    }
}

/// 资源缺失时的顶部提示：直接给按钮，不让用户去设置页里找。
private struct AssetBanner: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        GroupBox {
            VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
                Label(DesignTokens.Text.localAssetsMissing, systemImage: "arrow.down.circle")
                    .font(.callout)
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
            }
        }
        .backgroundStyle(.orange.opacity(0.15))
    }
}

private struct MicSection: View {
    @Environment(AppModel.self) private var model

    private var statusText: String {
        switch model.mic.state {
        case .idle: DesignTokens.Text.micStatusIdle
        case .running: DesignTokens.Text.micStatusRunning
        case .denied: DesignTokens.Text.micPermissionDenied
        case .blockedByBroadcast: DesignTokens.Text.micBlockedByBroadcast
        case .error(let message): message
        }
    }

    private var statusColor: Color {
        switch model.mic.state {
        case .running: .green
        case .idle: .secondary
        case .denied, .blockedByBroadcast, .error: .red
        }
    }

    var body: some View {
        GroupBox(DesignTokens.Text.micSubtitleTitle) {
            VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
                HStack {
                    Button(DesignTokens.Text.micStartButton) { model.startMic() }
                        .buttonStyle(.borderedProminent)
                        .disabled(model.mic.isRunning)
                    Button(DesignTokens.Text.micStopButton) { model.mic.stop() }
                        .buttonStyle(.bordered)
                        .disabled(!model.mic.isRunning)
                }
                Text(statusText)
                    .font(.caption)
                    .foregroundStyle(statusColor)
                if let denied = model.mic.speechDeniedHint {
                    Text(denied)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }
}

private struct PiPSection: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        GroupBox("画中画") {
            VStack(spacing: DesignTokens.Space.m) {
                PiPHostView(pipManager: model.pip)
                    .frame(height: 90)
                    .background(Color(.systemGray6), in: .rect(cornerRadius: DesignTokens.Radius.small))
                Button {
                    model.pip.isPiPActive ? model.pip.stopPiP() : model.pip.startPiP()
                } label: {
                    Text(model.pip.isPiPActive ? "关闭画中画" : "开启画中画")
                        .frame(maxWidth: .infinity)
                }
                .buttonStyle(.borderedProminent)
                .disabled(model.pip.isStarting)
                if let error = model.pip.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
        }
    }
}
