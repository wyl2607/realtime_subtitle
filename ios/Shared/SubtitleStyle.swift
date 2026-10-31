import Foundation

/// 字幕样式要存进 UserDefaults（JSON），所以这里只管数值和颜色字符串，
/// 不碰 SwiftUI。越界值和坏颜色统一在 clamped() 里收口，老数据缺字段也能读。
struct SubtitleStyle: Codable, Equatable {
    var fontSize: Double
    var sourceHex: String
    var translationHex: String
    var backgroundHex: String
    var backgroundOpacity: Double

    static let standard = SubtitleStyle()

    init(
        fontSize: Double = DesignTokens.TypeScale.subtitleDefault,
        sourceHex: String = DesignTokens.Color.subtitleSource,
        translationHex: String = DesignTokens.Color.subtitleTranslation,
        backgroundHex: String = DesignTokens.Color.background,
        backgroundOpacity: Double = DesignTokens.Opacity.backgroundDefault
    ) {
        self.fontSize = fontSize
        self.sourceHex = sourceHex
        self.translationHex = translationHex
        self.backgroundHex = backgroundHex
        self.backgroundOpacity = backgroundOpacity
    }

    private enum CodingKeys: String, CodingKey {
        case fontSize
        case sourceHex
        case translationHex
        case backgroundHex
        case backgroundOpacity
    }

    // 自己写 init(from:) 是为了 decodeIfPresent：老版本存的数据少一个键时，
    // 那一个字段回默认值，而不是整份样式解码失败退回 standard。
    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        let standard = SubtitleStyle.standard
        fontSize = try container.decodeIfPresent(Double.self, forKey: .fontSize) ?? standard.fontSize
        sourceHex = try container.decodeIfPresent(String.self, forKey: .sourceHex) ?? standard.sourceHex
        translationHex = try container.decodeIfPresent(String.self, forKey: .translationHex) ?? standard.translationHex
        backgroundHex = try container.decodeIfPresent(String.self, forKey: .backgroundHex) ?? standard.backgroundHex
        backgroundOpacity = try container.decodeIfPresent(Double.self, forKey: .backgroundOpacity) ?? standard.backgroundOpacity
    }

    func clamped() -> SubtitleStyle {
        let standard = SubtitleStyle.standard
        var result = self
        result.fontSize = Self.clamp(fontSize, lower: DesignTokens.TypeScale.subtitleMin, upper: DesignTokens.TypeScale.subtitleMax, fallback: standard.fontSize)
        result.backgroundOpacity = Self.clamp(backgroundOpacity, lower: 0.2, upper: 1.0, fallback: standard.backgroundOpacity)
        if Self.rgb(hex: sourceHex) == nil { result.sourceHex = standard.sourceHex }
        if Self.rgb(hex: translationHex) == nil { result.translationHex = standard.translationHex }
        if Self.rgb(hex: backgroundHex) == nil { result.backgroundHex = standard.backgroundHex }
        return result
    }

    static func load(from data: Data?) -> SubtitleStyle {
        guard let data, let decoded = try? JSONDecoder().decode(SubtitleStyle.self, from: data) else {
            return .standard
        }
        return decoded.clamped()
    }

    func encoded() -> Data? {
        try? JSONEncoder().encode(self)
    }

    /// "#RRGGBB"（大小写都接受）→ 0...1 的分量；不合法返回 nil。
    static func rgb(hex: String) -> (r: Double, g: Double, b: Double)? {
        guard hex.count == 7, hex.first == "#" else { return nil }
        let digits = hex.dropFirst()
        guard digits.allSatisfy({ $0.isHexDigit }), let value = UInt32(digits, radix: 16) else {
            return nil
        }
        return (
            r: Double((value >> 16) & 0xFF) / 255.0,
            g: Double((value >> 8) & 0xFF) / 255.0,
            b: Double(value & 0xFF) / 255.0
        )
    }

    private static func clamp(_ value: Double, lower: Double, upper: Double, fallback: Double) -> Double {
        guard value.isFinite else { return fallback }
        return min(max(value, lower), upper)
    }
}

enum SubtitleDisplay {
    /// 译文空的时候要不要占位，取决于这句是不是最新一句：
    /// 最新一句还在路上，显示省略号；旧句没等到译文就是「未翻译」。
    static func translation(_ dst: String, isNewest: Bool) -> (text: String, isPlaceholder: Bool) {
        let trimmed = dst.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty { return (dst, false) }
        if isNewest { return ("…", true) }
        return (DesignTokens.Text.untranslated, true)
    }

    /// 旧句对变淡，让用户一眼看出哪句在更新。列表为空时按满亮度，避免界面上出现半透明空块。
    static func pairOpacity(index: Int, count: Int) -> Double {
        guard count > 0 else { return 1.0 }
        return index == count - 1 ? 1.0 : DesignTokens.Opacity.olderPair
    }
}
