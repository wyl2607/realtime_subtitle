import SwiftUI

/// 字幕页的滚动字幕：新句从底部进来，整体自动往上滚。
///
/// 为什么不直接用字幕卡片：卡片只显示最近几句、固定位置原地换字，看起来不会「滚」，
/// 句子一快就来不及读。这里按本次运行累积的句子显示，停在底部时跟着最新一句走；
/// 用户往上翻看旧句时不抢回去，右下角给一个「回到最新」。
struct LiveCaptionsView: View {
    let lines: [RecentLine]
    let style: SubtitleStyle
    let status: StatusSummary?
    let draft: String

    @State private var position = ScrollPosition(edge: .bottom)
    /// 用户是不是停在底部：只有在底部时新句才自动滚过去。
    @State private var followsLatest = true

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
            Text(status?.label ?? DesignTokens.Text.micStatusIdle)
                .font(.system(size: DesignTokens.TypeScale.status))
                .foregroundStyle(Color(styleHex: statusHex))

            ScrollView {
                LazyVStack(alignment: .leading, spacing: DesignTokens.Space.pairGap) {
                    if lines.isEmpty && draft.isEmpty {
                        Text("暂无字幕")
                            .font(.system(size: DesignTokens.TypeScale.status))
                            .foregroundStyle(Color(styleHex: DesignTokens.Color.subtitleSecondary))
                    }
                    ForEach(lines, id: \.key) { line in
                        SubtitlePairRow(
                            line: line,
                            style: style,
                            isNewest: line.key == lines.last?.key,
                            opacity: line.key == lines.last?.key ? 1 : DesignTokens.Opacity.olderPair
                        )
                    }
                    if !draft.isEmpty {
                        Text(draft)
                            .font(.system(size: style.fontSize * DesignTokens.TypeScale.sourceScale))
                            .foregroundStyle(Color(styleHex: DesignTokens.Color.subtitleDraft))
                            .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
                .scrollTargetLayout()
            }
            .scrollPosition($position)
            .defaultScrollAnchor(.bottom)
            .onScrollGeometryChange(for: Bool.self) { geometry in
                // 离底部 40pt 以内都算「在底部」，草稿行一伸一缩不会把跟随状态弄掉。
                geometry.contentOffset.y + geometry.containerSize.height >= geometry.contentSize.height - 40
            } action: { _, atBottom in
                followsLatest = atBottom
            }
            .onChange(of: lines.last) { scrollToLatestIfFollowing() }
            .onChange(of: draft) { scrollToLatestIfFollowing() }
            .overlay(alignment: .bottomTrailing) {
                if !followsLatest {
                    Button {
                        withAnimation { position.scrollTo(edge: .bottom) }
                    } label: {
                        Label("回到最新", systemImage: "arrow.down")
                            .font(.footnote)
                    }
                    .buttonStyle(.borderedProminent)
                    .padding(DesignTokens.Space.s)
                }
            }
        }
        .padding(.vertical, DesignTokens.Space.windowPadV)
        .padding(.horizontal, DesignTokens.Space.windowPadH)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.card)
                .fill(Color(styleHex: style.backgroundHex).opacity(style.backgroundOpacity))
        )
    }

    private func scrollToLatestIfFollowing() {
        guard followsLatest else { return }
        withAnimation(.easeOut(duration: DesignTokens.Motion.fadeInMs / 1000)) {
            position.scrollTo(edge: .bottom)
        }
    }

    private var statusHex: String {
        switch status?.level {
        case .normal: DesignTokens.Color.statusNormal
        case .warning: DesignTokens.Color.statusWarning
        case .error: DesignTokens.Color.statusError
        case .idle, nil: DesignTokens.Color.subtitleSecondary
        }
    }
}
