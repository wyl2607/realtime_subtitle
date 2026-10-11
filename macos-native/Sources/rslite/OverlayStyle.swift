import CoreGraphics
import Foundation

enum TextEdge: String, Codable, Equatable {
    case none
    case shadow
    case outline
}

enum OverlayShape: String, Codable, Equatable {
    case rounded
    case square
    case strip
}

enum DisplayMode: String, Codable, Equatable {
    case bilingual
    case translationOnly
    case sourceOnly
}

/// 字幕外观。缺字段、坏 JSON、认不出的枚举都回默认；多出来的字段忽略。
struct OverlayStyle: Codable, Equatable {
    var fontSize: Double
    var sourceColor: String
    var translationColor: String
    var backgroundColor: String
    var backgroundOpacity: Double
    var textEdge: TextEdge
    var shape: OverlayShape
    var lines: Int
    var displayMode: DisplayMode
    /// 窗口左下角。空表示还没拖动过，用默认位置。
    var originX: Double?
    var originY: Double?
    /// 用户手动调整的窗口尺寸。空表示沿用当前形状的默认宽度/内容高度。
    var width: Double?
    var height: Double?

    static let defaultFontSize = DesignTokens.TypeScale.subtitleDefault
    static let defaultSourceColor = DesignTokens.Color.subtitleSource
    static let defaultTranslationColor = DesignTokens.Color.subtitleTranslation
    static let defaultBackgroundColor = DesignTokens.Color.background
    static let defaultBackgroundOpacity = DesignTokens.Opacity.backgroundDefault
    static let minimumFontSize = DesignTokens.TypeScale.subtitleMin
    static let maximumFontSize = DesignTokens.TypeScale.subtitleMax
    static let defaultLines = 2
    static let minimumWindowSize = CGSize(width: 360, height: 80)
    static let windowPadV = CGFloat(DesignTokens.Space.windowPadV)
    static let windowPadH = CGFloat(DesignTokens.Space.windowPadH)
    static let pairGap = CGFloat(DesignTokens.Space.pairGap)
    static let statusFontSize = CGFloat(DesignTokens.TypeScale.status)
    static let fontSizePresets: [Double] = [24, 28, 32, 40, 48]
    static let textColorPresets: [(title: String, hex: String)] = DesignTokens.Palette.text.map {
        (title: $0.name, hex: $0.hex)
    }
    static let backgroundColorPresets: [(title: String, hex: String)] = DesignTokens.Palette.background.map {
        (title: $0.name, hex: $0.hex)
    }
    static let opacityPresets: [(title: String, value: Double)] = [
        ("50%", 0.50),
        ("72%", 0.72),
        ("90%", 0.90),
    ]
    static let textEdgeChoices: [(title: String, value: TextEdge)] = [
        ("无", .none),
        ("阴影", .shadow),
        ("描边", .outline),
    ]
    static let shapeChoices: [(title: String, value: OverlayShape)] = [
        ("圆角", .rounded),
        ("直角", .square),
        ("贴底横条", .strip),
    ]
    static let displayModeChoices: [(title: String, value: DisplayMode)] = [
        ("双语", .bilingual),
        ("仅译文", .translationOnly),
        ("仅原文", .sourceOnly),
    ]

    static let `default` = OverlayStyle(
        fontSize: defaultFontSize,
        sourceColor: defaultSourceColor,
        translationColor: defaultTranslationColor,
        backgroundColor: defaultBackgroundColor,
        backgroundOpacity: defaultBackgroundOpacity,
        textEdge: .shadow,
        shape: .rounded,
        lines: defaultLines,
        displayMode: .bilingual,
        originX: nil,
        originY: nil,
        width: nil,
        height: nil
    )

    /// 圆角卡片 16；直角和贴底横条用直角令牌。
    var cornerRadius: CGFloat {
        switch shape {
        case .rounded:
            CGFloat(DesignTokens.Radius.card)
        case .square, .strip:
            CGFloat(DesignTokens.Radius.none)
        }
    }

    /// 贴底横条宽 = 可见宽 − 40；圆角/直角沿用原来的卡片宽（不超过 1100，两侧留 80）。
    static func windowWidth(for shape: OverlayShape, visibleWidth: CGFloat) -> CGFloat {
        let raw: CGFloat
        switch shape {
        case .strip:
            raw = visibleWidth - 40
        case .rounded, .square:
            raw = min(1100, visibleWidth - 80)
        }
        return max(raw, 1)
    }

    static func stepFontSize(_ current: Double, by delta: Int) -> Double {
        let presets = fontSizePresets
        if let index = presets.firstIndex(where: { abs($0 - current) < 0.01 }) {
            let next = min(max(index + delta, 0), presets.count - 1)
            return presets[next]
        }
        if delta >= 0 {
            return presets.first { $0 > current + 0.01 } ?? presets[presets.count - 1]
        }
        return presets.last { $0 < current - 0.01 } ?? presets[0]
    }

    static func parseHex(_ text: String) -> (r: Double, g: Double, b: Double)? {
        var raw = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if raw.hasPrefix("#") {
            raw.removeFirst()
        }
        guard raw.count == 3 || raw.count == 6 else { return nil }
        guard raw.allSatisfy(\.isHexDigit) else { return nil }
        if raw.count == 3 {
            raw = raw.map { "\($0)\($0)" }.joined()
        }
        guard let value = Int(raw, radix: 16) else { return nil }
        return (
            r: Double((value >> 16) & 0xFF) / 255,
            g: Double((value >> 8) & 0xFF) / 255,
            b: Double(value & 0xFF) / 255
        )
    }

    static func canonicalHex(_ text: String) -> String? {
        guard let rgb = parseHex(text) else { return nil }
        let r = Int((rgb.r * 255).rounded())
        let g = Int((rgb.g * 255).rounded())
        let b = Int((rgb.b * 255).rounded())
        return String(format: "#%02X%02X%02X", r, g, b)
    }

    /// 读失败、类型不对、空数据都回默认。单个认不出的枚举只让那一项回默认。
    static func fromJSON(_ data: Data) -> OverlayStyle {
        guard let partial = try? JSONDecoder().decode(OverlayStylePartial.self, from: data) else {
            return .default
        }
        var style = OverlayStyle.default
        if let fontSize = partial.fontSize, fontSize.isFinite, (minimumFontSize...maximumFontSize).contains(fontSize) {
            style.fontSize = fontSize
        }
        if let hex = partial.sourceColor, let canonical = canonicalHex(hex) {
            style.sourceColor = canonical
        }
        if let hex = partial.translationColor, let canonical = canonicalHex(hex) {
            style.translationColor = canonical
        }
        if let hex = partial.backgroundColor, let canonical = canonicalHex(hex) {
            style.backgroundColor = canonical
        }
        if let opacity = partial.backgroundOpacity.flatMap(normalizedOpacity) {
            style.backgroundOpacity = opacity
        }
        if let raw = partial.textEdge, let edge = TextEdge(rawValue: raw) {
            style.textEdge = edge
        }
        if let raw = partial.shape, let shape = OverlayShape(rawValue: raw) {
            style.shape = shape
        }
        if let lines = partial.lines {
            style.lines = min(4, max(1, lines))
        }
        if let raw = partial.displayMode, let mode = DisplayMode(rawValue: raw) {
            style.displayMode = mode
        }
        if let x = partial.originX, x.isFinite {
            style.originX = x
        }
        if let y = partial.originY, y.isFinite {
            style.originY = y
        }
        if let width = partial.width, width.isFinite, width > 0 {
            style.width = width
        }
        if let height = partial.height, height.isFinite, height > 0 {
            style.height = height
        }
        return style
    }

    /// 把窗口左下角夹进某块可见屏幕。有重叠就进重叠面积最大的那块；完全在外面就进离中心最近的那块。
    static func clampedOrigin(
        x: CGFloat,
        y: CGFloat,
        windowWidth: CGFloat,
        windowHeight: CGFloat,
        visibleFrames: [CGRect]
    ) -> CGPoint {
        clampedFrame(
            x: x,
            y: y,
            windowWidth: windowWidth,
            windowHeight: windowHeight,
            minimumSize: CGSize(width: 1, height: 1),
            visibleFrames: visibleFrames
        ).origin
    }

    /// 把窗口尺寸和左下角一起夹进某块可见屏幕；用于启动和显示器变化后的恢复。
    static func clampedFrame(
        x: CGFloat,
        y: CGFloat,
        windowWidth: CGFloat,
        windowHeight: CGFloat,
        minimumSize: CGSize = minimumWindowSize,
        visibleFrames: [CGRect]
    ) -> CGRect {
        let frames = visibleFrames.filter { $0.width > 1 && $0.height > 1 }
        let size = CGSize(width: max(windowWidth, 1), height: max(windowHeight, 1))
        let origin = CGPoint(x: x, y: y)
        guard !frames.isEmpty else {
            return CGRect(origin: origin, size: clampedSize(size, minimumSize: minimumSize, maximumSize: nil))
        }

        let window = CGRect(origin: origin, size: size)
        var target: CGRect?
        var bestArea: CGFloat = 0
        for frame in frames {
            let area = intersectionArea(frame, window)
            if area > bestArea {
                bestArea = area
                target = frame
            }
        }
        let screen: CGRect
        if let target {
            screen = target
        } else {
            let center = CGPoint(x: window.midX, y: window.midY)
            var nearest = frames[0]
            var nearestDistance = distanceSquared(nearest, center)
            for frame in frames.dropFirst() {
                let distance = distanceSquared(frame, center)
                if distance < nearestDistance {
                    nearest = frame
                    nearestDistance = distance
                }
            }
            screen = nearest
        }
        let fittedSize = clampedSize(size, minimumSize: minimumSize, maximumSize: screen.size)
        return CGRect(origin: clamp(origin, size: fittedSize, into: screen), size: fittedSize)
    }

    static func preferredVisibleFrame(anchor: CGPoint, frames: [CGRect], fallback: CGRect) -> CGRect {
        let usable = frames.filter { $0.width > 1 && $0.height > 1 }
        guard !usable.isEmpty else { return fallback }
        if let hit = usable.first(where: { $0.contains(anchor) }) {
            return hit
        }
        return usable.min { distanceSquared($0, anchor) < distanceSquared($1, anchor) } ?? fallback
    }

    private static func normalizedOpacity(_ value: Double) -> Double? {
        guard value.isFinite, (0...1).contains(value) else { return nil }
        return (value * 10_000).rounded() / 10_000
    }

    private static func intersectionArea(_ a: CGRect, _ b: CGRect) -> CGFloat {
        let hit = a.intersection(b)
        if hit.isNull || hit.isEmpty { return 0 }
        return hit.width * hit.height
    }

    private static func distanceSquared(_ frame: CGRect, _ point: CGPoint) -> CGFloat {
        let dx = max(frame.minX - point.x, point.x - frame.maxX, 0)
        let dy = max(frame.minY - point.y, point.y - frame.maxY, 0)
        return dx * dx + dy * dy
    }

    private static func clampedSize(_ size: CGSize, minimumSize: CGSize, maximumSize: CGSize?) -> CGSize {
        let minWidth = max(minimumSize.width, 1)
        let minHeight = max(minimumSize.height, 1)
        let maxWidth = max(maximumSize?.width ?? CGFloat.greatestFiniteMagnitude, 1)
        let maxHeight = max(maximumSize?.height ?? CGFloat.greatestFiniteMagnitude, 1)
        return CGSize(
            width: min(max(size.width, minWidth), maxWidth),
            height: min(max(size.height, minHeight), maxHeight)
        )
    }

    private static func clamp(_ origin: CGPoint, size: CGSize, into visible: CGRect) -> CGPoint {
        let x: CGFloat
        let y: CGFloat
        if size.width >= visible.width {
            x = visible.minX
        } else {
            x = min(max(origin.x, visible.minX), visible.maxX - size.width)
        }
        if size.height >= visible.height {
            y = visible.minY
        } else {
            y = min(max(origin.y, visible.minY), visible.maxY - size.height)
        }
        return CGPoint(x: x, y: y)
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }

        let defaults = OverlayStyle.default
        check(defaults.fontSize == DesignTokens.TypeScale.subtitleDefault, "令牌默认字号")
        check(defaults.sourceColor == DesignTokens.Color.subtitleSource, "令牌默认原文色")
        check(defaults.translationColor == DesignTokens.Color.subtitleTranslation, "令牌默认译文色")
        check(defaults.backgroundColor == DesignTokens.Color.background, "令牌默认背景色")
        check(defaults.backgroundOpacity == DesignTokens.Opacity.backgroundDefault, "令牌默认透明度")
        check(defaults.fontSize == 28, "默认字号 28")
        check(defaults.sourceColor == "#F0F0F0", "默认原文色")
        check(defaults.translationColor == "#FFE894", "默认译文色")
        check(defaults.backgroundColor == "#000000", "默认背景色")
        check(defaults.backgroundOpacity == 0.72, "默认透明度")
        check(textColorPresets.map(\.hex) == DesignTokens.Palette.text.map(\.hex), "文字调色板来自令牌")
        check(backgroundColorPresets.map(\.hex) == DesignTokens.Palette.background.map(\.hex), "背景调色板来自令牌")
        check(textColorPresets.count == 5 && backgroundColorPresets.count == 5, "5+5 预设调色板")
        check(minimumFontSize == DesignTokens.TypeScale.subtitleMin && maximumFontSize == DesignTokens.TypeScale.subtitleMax, "字号范围来自令牌")
        check(windowPadV == 18 && windowPadH == 22, "内边距 18×22")
        check(windowPadV == CGFloat(DesignTokens.Space.windowPadV) && windowPadH == CGFloat(DesignTokens.Space.windowPadH), "内边距来自令牌")
        check(pairGap == CGFloat(DesignTokens.Space.pairGap) && pairGap == 8, "句对间距来自令牌")
        check(statusFontSize == CGFloat(DesignTokens.TypeScale.status) && statusFontSize == 13, "状态行字号来自令牌")
        check(CGFloat(DesignTokens.Radius.none) == 0 && CGFloat(DesignTokens.Radius.small) == 8 && CGFloat(DesignTokens.Radius.card) == 16, "圆角令牌 0/8/16")
        check(defaults.textEdge == .shadow, "默认文字边缘 shadow")
        check(defaults.shape == .rounded, "默认形状 rounded")
        check(defaults.lines == 2, "默认行数 2")
        check(defaults.displayMode == .bilingual, "默认显示模式 bilingual")
        check(defaults.originX == nil && defaults.originY == nil, "默认无原点")
        check(defaults.width == nil && defaults.height == nil, "默认无尺寸")
        check(defaults.cornerRadius == CGFloat(DesignTokens.Radius.card), "圆角半径取卡片令牌")
        check(defaults.cornerRadius == 16, "圆角半径 16")
        var square = defaults
        square.shape = .square
        check(square.cornerRadius == CGFloat(DesignTokens.Radius.none), "直角半径取 none")
        square.shape = .strip
        check(square.cornerRadius == CGFloat(DesignTokens.Radius.none), "横条半径 0")
        check(windowWidth(for: .strip, visibleWidth: 1440) == 1400, "横条宽=可见宽-40")
        check(windowWidth(for: .rounded, visibleWidth: 1440) == 1100, "圆角卡片宽")
        check(windowWidth(for: .square, visibleWidth: 800) == 720, "直角同卡片宽")
        check(stepFontSize(28, by: 1) == 32, "字号增大一档")
        check(stepFontSize(28, by: -1) == 24, "字号减小一档")
        check(stepFontSize(24, by: -1) == 24, "字号最小停住")
        check(stepFontSize(48, by: 1) == 48, "字号最大停住")
        check(stepFontSize(30, by: 1) == 32 && stepFontSize(30, by: -1) == 28, "非档位字号向相邻档靠")

        var edited = defaults
        edited.fontSize = 40
        edited.sourceColor = "#9FD3FF"
        edited.translationColor = "#B6F2A8"
        edited.backgroundColor = "#0B1A2E"
        edited.backgroundOpacity = 0.9
        edited.textEdge = .outline
        edited.shape = .strip
        edited.lines = 4
        edited.displayMode = .translationOnly
        edited.originX = 320
        edited.originY = 48
        edited.width = 960
        edited.height = 180
        do {
            let data = try JSONEncoder().encode(edited)
            let decoded = try JSONDecoder().decode(OverlayStyle.self, from: data)
            check(decoded == edited, "JSON 往返")
            check(fromJSON(data) == edited, "fromJSON 往返")
            check(decoded.width == 960 && decoded.height == 180, "width/height 往返")
            check(fromJSON(data).width == 960 && fromJSON(data).height == 180, "fromJSON width/height 往返")
        } catch {
            failures.append("JSON 往返异常：\(error)")
        }

        check(fromJSON(Data("{}".utf8)) == defaults, "空对象回默认")
        check(fromJSON(Data()) == defaults, "空数据回默认")
        check(fromJSON(Data("{".utf8)) == defaults, "残缺 JSON 回默认")
        check(fromJSON(Data("[]".utf8)) == defaults, "数组 JSON 回默认")
        check(fromJSON(Data("null".utf8)) == defaults, "null 回默认")
        check(fromJSON(Data(#"{"fontSize":"big"}"#.utf8)) == defaults, "字段类型错误回默认")

        let partial = fromJSON(Data(#"{"fontSize":40,"extra":true,"nested":{"a":1}}"#.utf8))
        check(partial.fontSize == 40, "未知字段忽略")
        check(partial.translationColor == defaults.translationColor, "缺字段用默认")
        check(partial.originX == nil && partial.originY == nil, "缺原点保持空")
        check(partial.width == nil && partial.height == nil, "缺尺寸保持空")
        let sized = fromJSON(Data(#"{"width":777,"height":123}"#.utf8))
        check(sized.width == 777 && sized.height == 123, "尺寸字段读取")
        let onlyWidth = fromJSON(Data(#"{"width":640}"#.utf8))
        check(onlyWidth.width == 640 && onlyWidth.height == nil, "只写 width 时 height 保持空")
        let onlyHeight = fromJSON(Data(#"{"height":150}"#.utf8))
        check(onlyHeight.width == nil && onlyHeight.height == 150, "只写 height 时 width 保持空")
        let badSized = fromJSON(Data(#"{"width":0,"height":-1}"#.utf8))
        check(badSized.width == nil && badSized.height == nil, "非法尺寸回空")
        let nonFinite = fromJSON(Data(#"{"width":"wide","height":null}"#.utf8))
        check(nonFinite.width == nil && nonFinite.height == nil, "尺寸类型不对保持空")

        let unknown = fromJSON(Data(
            #"{"textEdge":"glow","shape":"pill","displayMode":"karaoke","fontSize":32,"lines":3}"#.utf8
        ))
        check(unknown.textEdge == .shadow, "未知 textEdge 回默认")
        check(unknown.shape == .rounded, "未知 shape 回默认")
        check(unknown.displayMode == .bilingual, "未知 displayMode 回默认")
        check(unknown.fontSize == 32 && unknown.lines == 3, "未知 enum 保留其它字段")
        check(fromJSON(Data(#"{"textEdge":"Shadow"}"#.utf8)).textEdge == .shadow, "大小写不对的 enum 回默认")
        check(fromJSON(Data(#"{"textEdge":"none"}"#.utf8)).textEdge == .none, "textEdge none")
        check(fromJSON(Data(#"{"textEdge":"outline"}"#.utf8)).textEdge == .outline, "textEdge outline")
        check(fromJSON(Data(#"{"fontSize":15}"#.utf8)).fontSize == defaults.fontSize, "字号低于令牌下限回默认")
        check(fromJSON(Data(#"{"fontSize":16}"#.utf8)).fontSize == 16, "字号等于下限保留")
        check(fromJSON(Data(#"{"fontSize":72}"#.utf8)).fontSize == 72, "字号等于上限保留")
        check(fromJSON(Data(#"{"fontSize":73}"#.utf8)).fontSize == defaults.fontSize, "字号高于令牌上限回默认")
        check(fromJSON(Data(#"{"lines":0}"#.utf8)).lines == 1, "行数低于 1 夹到 1")
        check(fromJSON(Data(#"{"lines":9}"#.utf8)).lines == 4, "行数高于 4 夹到 4")
        check(fromJSON(Data(#"{"backgroundOpacity":2}"#.utf8)).backgroundOpacity == 0.72, "透明度越界回默认")
        check(fromJSON(Data(#"{"sourceColor":"red"}"#.utf8)).sourceColor == "#F0F0F0", "非法颜色回默认")
        check(fromJSON(Data("{\"sourceColor\":\"#f0f0f0\"}".utf8)).sourceColor == "#F0F0F0", "颜色规范成大写")
        check(fromJSON(Data(#"{"originX":12}"#.utf8)).originX == 12, "只写 originX")
        check(fromJSON(Data(#"{"originX":12}"#.utf8)).originY == nil, "缺 originY 保持空")

        func near(_ value: Double?, _ expected: Double) -> Bool {
            guard let value else { return false }
            return abs(value - expected) < 0.000_001
        }
        let white = parseHex("#F0F0F0")
        check(near(white?.r, 240.0 / 255) && near(white?.g, 240.0 / 255) && near(white?.b, 240.0 / 255), "解析 #F0F0F0")
        let yellow = parseHex("#ffe894")
        check(near(yellow?.r, 255.0 / 255) && near(yellow?.g, 232.0 / 255) && near(yellow?.b, 148.0 / 255), "解析 #FFE894")
        for hex in ["#9FD3FF", "#B6F2A8", "#C8C8C8", "#000000", "#1E1E1E", "#0B1A2E"] {
            check(parseHex(hex) != nil, "解析 \(hex)")
        }
        let black = parseHex("#000000")
        check(black?.r == 0 && black?.g == 0 && black?.b == 0, "解析 #000000")
        let short = parseHex("#fff")
        check(short?.r == 1 && short?.g == 1 && short?.b == 1, "三位 hex 展开")
        check(canonicalHex("0B1A2E") == "#0B1A2E", "可省略 #")
        check(parseHex("red") == nil, "非法 hex")
        check(parseHex("#12") == nil, "长度不对的 hex")
        check(parseHex("#GG0000") == nil, "非十六进制")

        let screen = CGRect(x: 0, y: 0, width: 1440, height: 900)
        let stayed = clampedOrigin(x: 100, y: 80, windowWidth: 400, windowHeight: 200, visibleFrames: [screen])
        check(stayed.x == 100 && stayed.y == 80, "屏内原点不动")
        let right = clampedOrigin(x: 1400, y: 80, windowWidth: 400, windowHeight: 200, visibleFrames: [screen])
        check(right.x == 1040 && right.y == 80, "右侧越界夹回")
        let low = clampedOrigin(x: -40, y: -20, windowWidth: 400, windowHeight: 200, visibleFrames: [screen])
        check(low.x == 0 && low.y == 0, "左下越界夹回")
        let high = clampedOrigin(x: 10, y: 800, windowWidth: 400, windowHeight: 200, visibleFrames: [screen])
        check(high.x == 10 && high.y == 700, "上沿越界夹回")
        let huge = clampedOrigin(x: 100, y: 100, windowWidth: 2000, windowHeight: 2000, visibleFrames: [screen])
        check(huge.x == 0 && huge.y == 0, "窗口大于屏幕时贴住可见区原点")
        let leftScreen = CGRect(x: 0, y: 0, width: 1440, height: 900)
        let rightScreen = CGRect(x: 1440, y: 0, width: 1440, height: 900)
        let onRight = clampedOrigin(x: 2000, y: 100, windowWidth: 400, windowHeight: 200, visibleFrames: [leftScreen, rightScreen])
        check(onRight.x == 2000 && onRight.y == 100, "落在第二块屏幕时留在原处")
        let straddle = clampedOrigin(x: 1300, y: 100, windowWidth: 400, windowHeight: 200, visibleFrames: [leftScreen, rightScreen])
        check(straddle.x == 1440 && straddle.y == 100, "跨屏时夹进重叠更大的那块")
        let straddleLeft = clampedOrigin(x: 1200, y: 100, windowWidth: 400, windowHeight: 200, visibleFrames: [leftScreen, rightScreen])
        check(straddleLeft.x == 1040 && straddleLeft.y == 100, "跨屏偏左时夹回左屏")
        let off = clampedOrigin(x: 4000, y: 3000, windowWidth: 400, windowHeight: 200, visibleFrames: [leftScreen, rightScreen])
        check(off.x == 2480 && off.y == 700, "完全离屏夹进最近屏幕")
        let noScreen = clampedOrigin(x: 5, y: 6, windowWidth: 10, windowHeight: 10, visibleFrames: [])
        check(noScreen.x == 5 && noScreen.y == 6, "没有屏幕信息时保持原点")
        let edgeTiny = clampedOrigin(x: 1430, y: 10, windowWidth: 10, windowHeight: 10, visibleFrames: [screen])
        check(edgeTiny.x == 1430 && edgeTiny.y == 10, "原点夹回不把尺寸抬到最小")
        check(minimumWindowSize == CGSize(width: 360, height: 80), "最小尺寸 360x80")
        let smallFrame = clampedFrame(x: 50, y: 60, windowWidth: 10, windowHeight: 10, visibleFrames: [screen])
        check(smallFrame.origin.x == 50 && smallFrame.origin.y == 60, "过小尺寸保留屏内原点")
        check(smallFrame.size.width == 360 && smallFrame.size.height == 80, "过小尺寸夹到最小")
        let hugeFrame = clampedFrame(x: 100, y: 100, windowWidth: 2000, windowHeight: 2000, visibleFrames: [screen])
        check(hugeFrame.origin.x == 0 && hugeFrame.origin.y == 0, "过大尺寸原点夹回")
        check(hugeFrame.size.width == 1440 && hugeFrame.size.height == 900, "过大尺寸夹到可见区")
        let offFrame = clampedFrame(x: 4000, y: 3000, windowWidth: 500, windowHeight: 220, visibleFrames: [leftScreen, rightScreen])
        check(offFrame.origin.x == 2380 && offFrame.origin.y == 680, "尺寸+位置完全离屏夹进最近屏幕")
        check(offFrame.size.width == 500 && offFrame.size.height == 220, "屏内容尺寸不变")
        let smallOff = clampedFrame(x: -100, y: -80, windowWidth: 20, windowHeight: 30, visibleFrames: [screen])
        check(smallOff.size.width == 360 && smallOff.size.height == 80, "过小且离屏时尺寸夹到最小")
        check(smallOff.origin.x == 0 && smallOff.origin.y == 0, "过小且离屏时原点夹回")
        let hugeOff = clampedFrame(x: 5000, y: -500, windowWidth: 3000, windowHeight: 3000, visibleFrames: [leftScreen, rightScreen])
        check(hugeOff.size.width == 1440 && hugeOff.size.height == 900, "过大且离屏时尺寸夹到最近屏幕")
        check(hugeOff.origin.x == 1440 && hugeOff.origin.y == 0, "过大且离屏时原点贴住最近屏幕")

        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("rslite-style-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        do {
            check(OverlayStyleStore.load(directory: dir) == defaults, "没有文件回默认")
            try OverlayStyleStore.save(edited, directory: dir)
            check(OverlayStyleStore.load(directory: dir) == edited, "磁盘往返")
            let perm = (try FileManager.default.attributesOfItem(atPath: OverlayStyleStore.fileURL(directory: dir).path)[.posixPermissions] as? NSNumber)?.intValue ?? 0
            check(perm & 0o777 == 0o600, "style.json 权限 0600")
            try Data(#"{"fontSize":24,"notAField":true}"#.utf8).write(to: OverlayStyleStore.fileURL(directory: dir))
            let kept = OverlayStyleStore.load(directory: dir)
            check(kept.fontSize == 24 && kept.textEdge == .shadow, "磁盘上的未知字段忽略")
            try Data("%%%".utf8).write(to: OverlayStyleStore.fileURL(directory: dir))
            check(OverlayStyleStore.load(directory: dir) == defaults, "磁盘坏 JSON 回默认")
        } catch {
            failures.append("OverlayStyle 磁盘往返异常：\(error)")
        }

        return failures
    }
}

/// 容忍缺字段和未知字段。枚举先按字符串读，认不出就交给上层回默认，避免整份配置作废。
private struct OverlayStylePartial: Decodable {
    var fontSize: Double?
    var sourceColor: String?
    var translationColor: String?
    var backgroundColor: String?
    var backgroundOpacity: Double?
    var textEdge: String?
    var shape: String?
    var lines: Int?
    var displayMode: String?
    var originX: Double?
    var originY: Double?
    var width: Double?
    var height: Double?
}

enum OverlayStyleStore {
    static let fileName = "style.json"

    static func fileURL(directory: URL = CloudConfigStore.defaultDirectory) -> URL {
        directory.appendingPathComponent(fileName)
    }

    static func load(directory: URL = CloudConfigStore.defaultDirectory) -> OverlayStyle {
        guard let data = try? Data(contentsOf: fileURL(directory: directory)) else {
            return .default
        }
        return OverlayStyle.fromJSON(data)
    }

    static func save(_ style: OverlayStyle, directory: URL = CloudConfigStore.defaultDirectory) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(style)
        try CloudConfigStore.atomicWrite(data, to: fileURL(directory: directory), mode: 0o600)
    }
}
