import SwiftUI

/// 诊断页：资源、扩展状态、原始句对。给排查问题用，普通使用不需要看。
struct DiagnosticsPage: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationStack {
            List {
                Section("本机资源") {
                    LabeledContent("识别", value: label(model.assets.asr))
                    LabeledContent("翻译", value: label(model.assets.translation))
                    if !model.assets.statusText.isEmpty {
                        Text(model.assets.statusText)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }

                Section("App 状态") {
                    LabeledContent("前后台", value: model.appState.isInBackground ? "后台" : "前台")
                    LabeledContent("画中画可用", value: model.pip.isPictureInPicturePossible ? "是" : "否")
                    Text(model.pip.pipStatusLine)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }

                Section("扩展状态行") {
                    Text(model.feed.status)
                        .font(.system(.body, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                }

                Section("最近句对（原始）") {
                    if model.feed.lines.isEmpty {
                        Text("暂无字幕").foregroundStyle(.secondary)
                    } else {
                        ForEach(model.feed.lines) { line in
                            VStack(alignment: .leading, spacing: DesignTokens.Space.xs) {
                                Text("[\(line.id)] \(line.src)")
                                Text(line.dst.isEmpty ? "…" : line.dst)
                                    .foregroundStyle(.secondary)
                            }
                            .font(.system(.body, design: .monospaced))
                        }
                    }
                }

                Section("跨进程通知") {
                    LabeledContent("状态", value: "\(model.feed.statusNotificationCount)")
                    LabeledContent("句对", value: "\(model.feed.linesNotificationCount)")
                }
            }
            .navigationTitle("诊断")
        }
    }

    private func label(_ readiness: AssetPreparer.Readiness) -> String {
        switch readiness {
        case .unknown: "未检查"
        case .ready: "已安装"
        case .missing: "未安装"
        }
    }
}
