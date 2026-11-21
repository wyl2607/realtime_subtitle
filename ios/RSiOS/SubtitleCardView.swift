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

    var body: some View {
        VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
            Text(self.status?.label ?? DesignTokens.Text.micStatusIdle)
                .font(.system(size: DesignTokens.TypeScale.status, weight: .regular))
                .foregroundColor(self.statusColor)
                .frame(maxWidth: .infinity, alignment: .leading)

            if self.shownLines.isEmpty {
                Text("暂无字幕")
                    .font(.system(size: DesignTokens.TypeScale.status, weight: .regular))
                    .foregroundColor(Color(styleHex: DesignTokens.Color.subtitleSecondary))
                    .frame(maxWidth: .infinity, alignment: .leading)
            } else {
                self.pairs
            }

            if !self.draft.isEmpty {
                Text(self.draft)
                    .font(.system(
                        size: self.style.fontSize * DesignTokens.TypeScale.sourceScale,
                        weight: .regular
                    ))
                    .foregroundColor(Color(styleHex: DesignTokens.Color.subtitleDraft))
                    .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, DesignTokens.Space.windowPadV)
        .padding(.horizontal, DesignTokens.Space.windowPadH)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.card)
                .fill(Color(styleHex: self.style.backgroundHex).opacity(self.style.backgroundOpacity))
        )
        .animation(
            .easeOut(duration: DesignTokens.Motion.fadeInMs / 1000),
            value: self.lines.map(\.key)
        )
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
    }
}

/// 一组原文 + 译文。字幕卡片和字幕页的滚动字幕共用，保证两处排版一致。
struct SubtitlePairRow: View {
    let line: RecentLine
    let style: SubtitleStyle
    let isNewest: Bool
    let opacity: Double

    var body: some View {
        let translation = SubtitleDisplay.translation(line.dst, isNewest: isNewest)
        VStack(alignment: .leading, spacing: DesignTokens.Space.s) {
            Text(line.src)
                .font(.system(size: style.fontSize * DesignTokens.TypeScale.sourceScale, weight: .medium))
                .foregroundColor(Color(styleHex: style.sourceHex))
            Text(translation.text)
                .font(.system(size: style.fontSize, weight: .regular))
                .foregroundColor(
                    translation.isPlaceholder
                        ? Color(styleHex: DesignTokens.Color.untranslated)
                        : Color(styleHex: style.translationHex)
                )
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .opacity(opacity)
        .transition(.move(edge: .bottom).combined(with: .opacity))
    }
}
