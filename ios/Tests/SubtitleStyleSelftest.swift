import Foundation

/// 字幕样式自测：持久化数据可能来自老版本或被手改坏，
/// 所以默认值、夹值、坏 hex、缺字段四条路径都要能反复跑。
enum SubtitleStyleSelftest {
    static func run() -> Bool {
        var failed = false
        func check(_ condition: Bool, _ message: String) {
            if condition {
                print("PASS: \(message)")
            } else {
                print("FAIL: \(message)")
                failed = true
            }
        }

        // 默认样式直接取自设计令牌。
        let standard = SubtitleStyle.standard
        check(standard.fontSize == DesignTokens.TypeScale.subtitleDefault, "默认字号 = subtitleDefault")
        check(standard.sourceHex == DesignTokens.Color.subtitleSource, "默认原文字色 = subtitleSource")
        check(standard.translationHex == DesignTokens.Color.subtitleTranslation, "默认译文字色 = subtitleTranslation")
        check(standard.backgroundHex == DesignTokens.Color.background, "默认背景色 = background")
        check(standard.backgroundOpacity == DesignTokens.Opacity.backgroundDefault, "默认背景透明度 = backgroundDefault")

        // 越界数值夹回边界。
        var low = SubtitleStyle.standard
        low.fontSize = 1
        low.backgroundOpacity = 0
        let lowClamped = low.clamped()
        check(lowClamped.fontSize == DesignTokens.TypeScale.subtitleMin, "字号下界夹到 subtitleMin")
        check(lowClamped.backgroundOpacity == 0.2, "透明度下界夹到 0.2")

        var high = SubtitleStyle.standard
        high.fontSize = 1000
        high.backgroundOpacity = 9
        let highClamped = high.clamped()
        check(highClamped.fontSize == DesignTokens.TypeScale.subtitleMax, "字号上界夹到 subtitleMax")
        check(highClamped.backgroundOpacity == 1.0, "透明度上界夹到 1.0")

        // 坏 hex 按字段逐个回默认值，不牵连其它字段。
        var badHex = SubtitleStyle.standard
        badHex.sourceHex = "red"
        badHex.translationHex = "#12345"
        badHex.backgroundHex = "#GGGGGG"
        let fixedHex = badHex.clamped()
        check(fixedHex.sourceHex == DesignTokens.Color.subtitleSource, "坏的原文字色回默认")
        check(fixedHex.translationHex == DesignTokens.Color.subtitleTranslation, "坏的译文字色回默认")
        check(fixedHex.backgroundHex == DesignTokens.Color.background, "坏的背景色回默认")

        // rgb 解析：大小写都收，分量按 0...1。
        let upper = SubtitleStyle.rgb(hex: DesignTokens.Color.subtitleTranslation)
        let lower = SubtitleStyle.rgb(hex: DesignTokens.Color.subtitleTranslation.lowercased())
        check(upper != nil && lower != nil, "大小写 hex 都能解析")
        if let upper, let lower {
            check(upper.r == lower.r && upper.g == lower.g && upper.b == lower.b, "大小写解析结果一致")
        }
        if let black = SubtitleStyle.rgb(hex: "#000000") {
            check(black.r == 0 && black.g == 0 && black.b == 0, "#000000 → 全 0")
        } else {
            check(false, "#000000 应能解析")
        }
        if let white = SubtitleStyle.rgb(hex: "#FFFFFF") {
            check(white.r == 1 && white.g == 1 && white.b == 1, "#FFFFFF → 全 1")
        } else {
            check(false, "#FFFFFF 应能解析")
        }
        check(SubtitleStyle.rgb(hex: "FFE894") == nil, "缺 # 不合法")
        check(SubtitleStyle.rgb(hex: "#FFF") == nil, "三位缩写不合法")
        check(SubtitleStyle.rgb(hex: "#FFE89400") == nil, "带 alpha 不合法")
        check(SubtitleStyle.rgb(hex: "#GGGGGG") == nil, "非十六进制字符不合法")
        check(SubtitleStyle.rgb(hex: "") == nil, "空串不合法")

        // 解码容错：nil、坏 JSON、缺字段。
        check(SubtitleStyle.load(from: nil) == .standard, "nil 数据 → standard")
        check(SubtitleStyle.load(from: Data("not json".utf8)) == .standard, "坏 JSON → standard")
        check(SubtitleStyle.load(from: Data("{}".utf8)) == .standard, "缺所有字段 → standard")

        let partial = Data(#"{"fontSize": 40}"#.utf8)
        let partialStyle = SubtitleStyle.load(from: partial)
        check(partialStyle.fontSize == 40, "缺字段时保留已存字号")
        check(partialStyle.sourceHex == DesignTokens.Color.subtitleSource, "缺字段的其它项回默认")

        // 解码成功也要夹值。
        let extreme = Data(#"{"fontSize": 999, "backgroundOpacity": 9}"#.utf8)
        let extremeStyle = SubtitleStyle.load(from: extreme)
        check(extremeStyle.fontSize == DesignTokens.TypeScale.subtitleMax, "load 也会夹字号")
        check(extremeStyle.backgroundOpacity == 1.0, "load 也会夹透明度")

        // 编码再读回来应当等价。
        var custom = SubtitleStyle.standard
        custom.fontSize = 30
        custom.backgroundOpacity = 0.5
        if let encoded = custom.encoded() {
            check(SubtitleStyle.load(from: encoded) == custom, "编码/解码往返一致")
        } else {
            check(false, "编码不应失败")
        }

        // 译文显示三种情况。
        let filled = SubtitleDisplay.translation("text", isNewest: false)
        check(filled.text == "text" && !filled.isPlaceholder, "非空译文原样显示")
        let padded = SubtitleDisplay.translation("  text  ", isNewest: false)
        check(padded.text == "  text  " && !padded.isPlaceholder, "只有空白差异时保留原文本")
        let newestEmpty = SubtitleDisplay.translation("   ", isNewest: true)
        check(newestEmpty.text == "…" && newestEmpty.isPlaceholder, "最新一句空译文 → 省略号占位")
        let oldEmpty = SubtitleDisplay.translation("", isNewest: false)
        check(oldEmpty.text == DesignTokens.Text.untranslated && oldEmpty.isPlaceholder, "旧句空译文 → 未翻译")

        // 旧句变淡，最新一句满亮度。
        check(SubtitleDisplay.pairOpacity(index: 0, count: 0) == 1.0, "空列表按满亮度")
        check(SubtitleDisplay.pairOpacity(index: 2, count: 3) == 1.0, "最新一条满亮度")
        check(SubtitleDisplay.pairOpacity(index: 0, count: 3) == DesignTokens.Opacity.olderPair, "旧句用 olderPair")
        check(SubtitleDisplay.pairOpacity(index: 1, count: 3) == DesignTokens.Opacity.olderPair, "中间旧句同样变淡")

        return !failed
    }
}
