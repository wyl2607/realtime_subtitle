import ActivityKit
import SwiftUI
import WidgetKit

@main
struct SubtitleLiveActivityBundle: WidgetBundle {
    var body: some Widget {
        SubtitleLiveActivity()
        StartMicCaptionsControl()
    }
}

/// 锁屏横幅显示一整句；灵动岛展开时显示原文+译文，收起时只留状态。
struct SubtitleLiveActivity: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: SubtitleActivityAttributes.self) { context in
            LockScreenView(state: context.state, sourceName: context.attributes.sourceName)
                .activityBackgroundTint(.black.opacity(0.72))
                .activitySystemActionForegroundColor(.white)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.bottom) {
                    VStack(alignment: .leading, spacing: 2) {
                        Text(context.state.source)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                        Text(context.state.translation)
                            .font(.callout)
                            .lineLimit(2)
                    }
                }
            } compactLeading: {
                Image(systemName: "captions.bubble")
            } compactTrailing: {
                Text(context.state.status)
                    .font(.caption2)
                    .lineLimit(1)
            } minimal: {
                Image(systemName: "captions.bubble")
            }
        }
    }
}

private struct LockScreenView: View {
    let state: SubtitleActivityAttributes.ContentState
    let sourceName: String

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Label(sourceName, systemImage: "captions.bubble")
                Spacer()
                Text(state.status)
            }
            .font(.caption2)
            .foregroundStyle(.white.opacity(0.7))
            Text(state.source)
                .font(.footnote)
                .foregroundStyle(.white.opacity(0.85))
                .lineLimit(2)
            Text(state.translation)
                .font(.body.weight(.medium))
                .foregroundStyle(.yellow)
                .lineLimit(2)
        }
        .padding()
    }
}
