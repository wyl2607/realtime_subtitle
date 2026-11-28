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

    // 状态行用语义字号，跟随系统动态字体。
    @ScaledMetric(relativeTo: .caption) private var statusFontSize: CGFloat = DesignTokens.TypeScale.status
    // 草稿行字号以用户设置为基准通过 ScaledMetric 等比放大（见 scaledDraftSize）。
    @ScaledMetric(relativeTo: .body) private var scaledBodySize: CGFloat = DesignTokens.TypeScale.subtitleDefault

    private var scaledDraftSize: CGFloat {
        let ratio = scaledBodySize / DesignTokens.TypeScale.subtitleDefault
        return style.fontSize * DesignTokens.TypeScale.sourceScale * ratio
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
            Text(status?.label ?? DesignTokens.Text.micStatusIdle)
                .font(.system(size: statusFontSize))
                .foregroundStyle(Color(styleHex: statusHex))
                .accessibilityHidden(true)   // 状态已通过整体 accessibilityValue 播报

            ScrollView {
                LazyVStack(alignment: .leading, spacing: DesignTokens.Space.pairGap) {
                    if lines.isEmpty && draft.isEmpty {
                        Text("暂无字幕")
                            .font(.system(size: statusFontSize))
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
                            .font(.system(size: scaledDraftSize))
                            .foregroundStyle(Color(styleHex: DesignTokens.Color.subtitleDraft))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .accessibilityHidden(true)   // 草稿未定稿，不单独播报
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
                    // "回到最新" label 文字已明确，VoiceOver 能直接读出
                }
            }
        }
        .padding(.vertical, DesignTokens.Space.windowPadV)
        .padding(.horizontal, DesignTokens.Space.windowPadH)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.card)
                .fill(Color(styleHex: style.backgroundHex).opacity(style.backgroundOpacity))
                .accessibilityHidden(true)
        )
        .accessibilityLabel("实时字幕")
        .accessibilityValue(status?.label ?? DesignTokens.Text.micStatusIdle)
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
