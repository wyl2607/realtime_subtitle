import SwiftUI

extension Color {
    /// 设计令牌和用户样式都用 #RRGGBB 存颜色；解析失败回退白色，
    /// 让坏数据在界面上可见，而不是整块透明消失。
    init(styleHex: String) {
        if let rgb = SubtitleStyle.rgb(hex: styleHex) {
            self.init(red: rgb.r, green: rgb.g, blue: rgb.b)
        } else {
            self = .white
        }
    }
}

/// 字幕卡片的唯一渲染入口：主界面和样式预览共用，避免两处规则走散。
/// 旧的在上、新的在下；顶部渐隐让滚动中的旧句自然淡出。
struct SubtitleCardView: View {
    let lines: [RecentLine]
    let style: SubtitleStyle
    let status: StatusSummary?
    /// 未定稿的实时草稿原文：显示在最新句对下面，不翻译、不进存档。
    var draft: String = ""
    var maxPairs: Int = 4

    // 状态行用语义字号，跟随系统动态字体。
    @ScaledMetric(relativeTo: .caption) private var statusFontSize: CGFloat = DesignTokens.TypeScale.status

    private var shownLines: [RecentLine] {
        guard maxPairs > 0 else { return [] }
        return Array(lines.suffix(maxPairs))
    }

    private var statusColor: Color {
        guard let status else {
            return Color(styleHex: DesignTokens.Color.subtitleSecondary)
        }
        switch status.level {
        case .normal:
            return Color(styleHex: DesignTokens.Color.statusNormal)
        case .warning:
            return Color(styleHex: DesignTokens.Color.statusWarning)
        case .error:
            return Color(styleHex: DesignTokens.Color.statusError)
        case .idle:
            return Color(styleHex: DesignTokens.Color.subtitleSecondary)
        }
    }

    private var statusLabel: String {
        status?.label ?? DesignTokens.Text.micStatusIdle
    }

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
            Text(statusLabel)
                .font(.system(size: statusFontSize, weight: .regular))
                .foregroundColor(self.statusColor)
                .frame(maxWidth: .infinity, alignment: .leading)
                .accessibilityHidden(true)   // 状态信息已合并到整卡的 accessibilityValue

            if self.shownLines.isEmpty {
                Text("暂无字幕")
                    .font(.system(size: statusFontSize, weight: .regular))
                    .foregroundColor(Color(styleHex: DesignTokens.Color.subtitleSecondary))
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                self.pairs
            }

            if !self.draft.isEmpty {
                // 草稿行：用户设置的字号通过 @ScaledMetric 等比放大（见 SubtitlePairRow）。
                // 这里只用于外层卡片的直接草稿显示，子视图负责自己的 ScaledMetric。
                ScaledDraftText(draft: self.draft, style: self.style)
            }
        }
        .padding(.vertical, DesignTokens.Space.windowPadV)
        .padding(.horizontal, DesignTokens.Space.windowPadH)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.card)
                .fill(Color(styleHex: self.style.backgroundHex).opacity(self.style.backgroundOpacity))
                .accessibilityHidden(true)
        )
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityCardLabel)
        .accessibilityValue(statusLabel)
        .animation(
            .easeOut(duration: DesignTokens.Motion.fadeInMs / 1000),
            value: self.lines.map(\.key)
        )
    }

    private var accessibilityCardLabel: String {
        if shownLines.isEmpty && draft.isEmpty {
            return "字幕卡片，暂无字幕"
        }
        var parts: [String] = []
        for line in shownLines {
            parts.append(line.src)
            if !line.dst.isEmpty { parts.append(line.dst) }
        }
        if !draft.isEmpty { parts.append(draft) }
        return parts.joined(separator: "，")
    }

    private var pairs: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Space.pairGap) {
            // 按句子身份而不是位置做 id：新句进来时旧句整体往上移，而不是每一格原地换字。
            ForEach(Array(self.shownLines.enumerated()), id: \.element.key) { index, line in
                SubtitlePairRow(
                    line: line,
                    style: self.style,
                    isNewest: index == self.shownLines.count - 1,
                    opacity: SubtitleDisplay.pairOpacity(index: index, count: self.shownLines.count)
                )
            }
        }
        // 只渐隐最上面约 20%：再多会把最新一句也压暗，用户分不清哪句在更新。
        .mask(
            LinearGradient(
                gradient: Gradient(stops: [
                    .init(color: .clear, location: 0),
                    .init(color: .black, location: 0.2),
                    .init(color: .black, location: 1),
                ]),
                startPoint: .top,
                endPoint: .bottom
            )
        )
        // pairs 内的元素已被外层 .accessibilityElement(children: .ignore) 合并，不重复朗读。
        .accessibilityHidden(true)
    }
}

/// 草稿文本：用户设定字号为基准，ScaledMetric 等比跟随动态字体。
private struct ScaledDraftText: View {
    let draft: String
    let style: SubtitleStyle

    @ScaledMetric(relativeTo: .body) private var scaledSize: CGFloat = DesignTokens.TypeScale.subtitleDefault

    private var resolvedSize: CGFloat {
        // 用 scaledSize 的缩放比例乘以用户实际选择的字号。
        let ratio = scaledSize / DesignTokens.TypeScale.subtitleDefault
        return style.fontSize * DesignTokens.TypeScale.sourceScale * ratio
    }

    var body: some View {
        Text(draft)
            .font(.system(size: resolvedSize, weight: .regular))
            .foregroundColor(Color(styleHex: DesignTokens.Color.subtitleDraft))
            .frame(maxWidth: .infinity, alignment: .leading)
    }
}

/// 一组原文 + 译文。字幕卡片和字幕页的滚动字幕共用，保证两处排版一致。
struct SubtitlePairRow: View {
    let line: RecentLine
    let style: SubtitleStyle
    let isNewest: Bool
    let opacity: Double

    // 以 .body 为基准缩放参考，取默认值作为比例基准。
    @ScaledMetric(relativeTo: .body) private var scaledSize: CGFloat = DesignTokens.TypeScale.subtitleDefault

    private var scaledSourceSize: CGFloat {
        let ratio = scaledSize / DesignTokens.TypeScale.subtitleDefault
        return style.fontSize * DesignTokens.TypeScale.sourceScale * ratio
    }

    private var scaledTranslationSize: CGFloat {
        let ratio = scaledSize / DesignTokens.TypeScale.subtitleDefault
        return style.fontSize * ratio
    }

    var body: some View {
        let translation = SubtitleDisplay.translation(line.dst, isNewest: isNewest)
        VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
            Text(line.src)
                .font(.system(size: scaledSourceSize, weight: .medium))
                .foregroundColor(Color(styleHex: style.sourceHex))
            Text(translation.text)
                .font(.system(size: scaledTranslationSize, weight: .regular))
                .foregroundColor(
                    translation.isPlaceholder
                        ? Color(styleHex: DesignTokens.Color.untranslated)
                        : Color(styleHex: style.translationHex)
                )
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity(opacity)
        .transition(.move(edge: .bottom).combined(with: .opacity))
        // 原文+译文合并为单个无障碍元素，VoiceOver 一次读完一对。
        .accessibilityElement(children: .combine)
    }
}
