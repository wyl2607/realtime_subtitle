import CoreGraphics
import Foundation

/// 字幕动效、最短停留、瀑布流和回看的纯逻辑。不建窗口，`--selftest` 直接调用。
enum OverlayMotion {
    static let waterfallMenuHint = "拉高窗口可显示更多（瀑布流）"
    static let newCaptionBadgeTitle = "↓ 新字幕"
    /// 比「行数」所需高度再高出这么多，才算用户拉高了窗口，避免 1pt 测量误差来回切换。
    static let waterfallSlack: Double = 1

    struct ShownLine: Equatable {
        var key: Int
        var shownAt: Double
    }

    struct WaterfallFit: Equatable {
        var pairCount: Int
        var clipsTop: Bool
    }

    /// 固定行数模式下，哪些句对还留在画面上。
    /// `shown` 按时间从旧到新。一句上屏未满 `minHoldSec` 时，最多比 `lines` 多留一行。
    static func visibleLineKeys(
        shown: [ShownLine],
        now: Double,
        lines: Int,
        minHoldSec: Double = DesignTokens.Motion.minHoldSec
    ) -> [Int] {
        let preferred = max(0, lines)
        guard preferred > 0, !shown.isEmpty else { return [] }
        if shown.count <= preferred {
            return shown.map(\.key)
        }
        let outside = shown[shown.count - preferred - 1]
        let hold = max(0, minHoldSec)
        let stillHolding = now - outside.shownAt < hold
        let count = stillHolding ? preferred + 1 : preferred
        return shown.suffix(count).map(\.key)
    }

    /// 瀑布流 fit 之后的最短停留：紧挨完整可见段上方的那一对若上屏未满 `minHoldSec`，
    /// 就再多留它一对，并靠顶部渐隐裁切。`clipsTop` 时 `fit.pairCount` 已含顶部
    /// 半可见的一对，不能再加一对。回看时不保最短停留。
    /// `shown` 与 `pool` 同序（从旧到新），`fit` 是本轮按高度算出的结果。
    static func waterfallHoldCount(
        shown: [ShownLine],
        now: Double,
        fit: WaterfallFit,
        reviewing: Bool,
        minHoldSec: Double = DesignTokens.Motion.minHoldSec
    ) -> WaterfallFit {
        guard !reviewing, !fit.clipsTop, !shown.isEmpty else { return fit }
        let base = min(max(0, fit.pairCount), shown.count)
        guard base > 0, base < shown.count else { return fit }
        let outside = shown[shown.count - base - 1]
        guard now - outside.shownAt < max(0, minHoldSec) else { return fit }
        return WaterfallFit(pairCount: base + 1, clipsTop: true)
    }

    /// 窗口里能放下几对。放不下的最上一对也算进去，调用方用顶部渐隐裁它。
    /// `verticalPadding` 是上下内边距之和。句对高度必须大于 0。
    static func waterfallFit(
        windowHeight: Double,
        pairHeight: Double,
        verticalPadding: Double,
        pairGap: Double
    ) -> WaterfallFit {
        guard pairHeight > 0, windowHeight.isFinite, pairHeight.isFinite else {
            return WaterfallFit(pairCount: 0, clipsTop: false)
        }
        let available = windowHeight - verticalPadding
        guard available > 0 else {
            return WaterfallFit(pairCount: 0, clipsTop: false)
        }
        var count = 0
        var used = 0.0
        let epsilon = 1e-6
        while true {
            let gap = count > 0 ? pairGap : 0
            if used + gap + pairHeight <= available + epsilon {
                used += gap + pairHeight
                count += 1
                continue
            }
            break
        }
        let gapBeforePartial = count > 0 ? pairGap : 0
        if used + gapBeforePartial < available - epsilon {
            return WaterfallFit(pairCount: count + 1, clipsTop: true)
        }
        return WaterfallFit(pairCount: count, clipsTop: false)
    }

    static func waterfallPairCount(
        windowHeight: Double,
        pairHeight: Double,
        verticalPadding: Double,
        pairGap: Double
    ) -> Int {
        waterfallFit(
            windowHeight: windowHeight,
            pairHeight: pairHeight,
            verticalPadding: verticalPadding,
            pairGap: pairGap
        ).pairCount
    }

    /// 从底往上量真实句对高度。`pairHeightsBottomFirst` 第 0 个是最底部（最新）的一对。
    static func waterfallFit(
        availableHeight: Double,
        pairHeightsBottomFirst: [Double],
        pairGap: Double
    ) -> WaterfallFit {
        guard availableHeight > 0 else {
            return WaterfallFit(pairCount: 0, clipsTop: false)
        }
        var used = 0.0
        var full = 0
        let epsilon = 1e-6
        for (index, height) in pairHeightsBottomFirst.enumerated() {
            guard height > 0 else { continue }
            let gap = index == 0 ? 0.0 : pairGap
            if used + gap + height <= availableHeight + epsilon {
                used += gap + height
                full += 1
            } else if used + gap < availableHeight - epsilon {
                return WaterfallFit(pairCount: full + 1, clipsTop: true)
            } else {
                return WaterfallFit(pairCount: full, clipsTop: false)
            }
        }
        return WaterfallFit(pairCount: full, clipsTop: false)
    }

    /// 有用户高度，且高于「行数」所需高度，才进入瀑布流。
    static func isWaterfall(userHeight: Double?, linesHeight: Double, slack: Double = 0) -> Bool {
        guard let userHeight, userHeight.isFinite, linesHeight.isFinite else { return false }
        return userHeight > linesHeight + slack
    }

    /// 用户把窗口拉回不超过「行数」所需高度时，清掉记住的高度。
    static func shouldClearUserHeight(userHeight: Double?, linesHeight: Double, slack: Double = 0) -> Bool {
        guard let userHeight, userHeight.isFinite, linesHeight.isFinite else { return false }
        return userHeight <= linesHeight + slack
    }

    /// 非最新一对变暗。瀑布流里越旧越淡，最旧约 `DesignTokens.Opacity.waterfallOldest`。
    static func pairOpacity(indexFromNewest: Int, visibleCount: Int, waterfall: Bool) -> Double {
        if indexFromNewest <= 0 || visibleCount <= 1 {
            return 1
        }
        if !waterfall {
            return DesignTokens.Opacity.olderPair
        }
        if visibleCount == 2 {
            return DesignTokens.Opacity.olderPair
        }
        let span = Double(visibleCount - 2)
        let t = span <= 0 ? 0 : Double(indexFromNewest - 1) / span
        let clamped = min(1, max(0, t))
        return DesignTokens.Opacity.olderPair + (DesignTokens.Opacity.waterfallOldest - DesignTokens.Opacity.olderPair) * clamped
    }

    /// 系统「减少动态效果」开启时时长为 0。否则把毫秒令牌换成秒。
    static func motionSeconds(milliseconds: Double, reduceMotion: Bool) -> Double {
        if reduceMotion || !milliseconds.isFinite || milliseconds <= 0 {
            return 0
        }
        return milliseconds / 1000
    }

    static func motionSeconds(reduceMotion: Bool) -> (fade: Double, shift: Double, resize: Double) {
        (
            fade: motionSeconds(milliseconds: DesignTokens.Motion.fadeInMs, reduceMotion: reduceMotion),
            shift: motionSeconds(milliseconds: DesignTokens.Motion.shiftUpMs, reduceMotion: reduceMotion),
            resize: motionSeconds(milliseconds: DesignTokens.Motion.resizeMs, reduceMotion: reduceMotion)
        )
    }

    /// 鼠标穿透或还在固定行数模式时，滚轮不翻历史。
    static func reviewScrollDelta(rawSteps: Int, clickThrough: Bool, waterfall: Bool) -> Int {
        if clickThrough || !waterfall || rawSteps == 0 {
            return 0
        }
        return rawSteps
    }

    /// 译文位失败标记的字号 = 译文号 × 这个比例。
    static let markerFontScale = 0.8
    /// retry 返回 false 后，过这么久锁定为不可再点的静态「已过期」。
    static let expiredLockSec = 3.0
    static let retryingText = "翻译中…"
    static let expiredText = "已过期"

    /// 一句的翻译结局。`.none` 表示还没失败（可能已有译文，也可能还在等）。
    enum FailureState: Equatable {
        case none
        case failed
        case retrying
        case expired(isStatic: Bool)
    }

    /// 第二行怎么画。颜色和字号由 `secondLineColorHex` / `secondLineFontScale` 决定，不建窗口。
    enum SecondLineStyle: Equatable {
        case hidden
        case translation
        case untranslated
        case retrying
        case expired
    }

    /// 一句在某个显示模式下的两行。`nil` 表示这一行不画。
    struct PairLines: Equatable {
        var first: String?
        var second: String?
        var secondStyle: SecondLineStyle
    }

    /// 点击重试的即时结果。false 先进入未锁定的「已过期」，满 `expiredLockSec` 后变成静态。
    static func markerAfterRetry(accepted: Bool) -> FailureState {
        accepted ? .retrying : .expired(isStatic: false)
    }

    /// 已过期满 3 秒后锁定为静态；其它状态不变。已经锁定的不会退回。
    static func markerSettled(_ state: FailureState, elapsed: Double) -> FailureState {
        guard case .expired(let isStatic) = state else { return state }
        if isStatic || elapsed >= expiredLockSec {
            return .expired(isStatic: true)
        }
        return .expired(isStatic: false)
    }

    static func secondLineFontScale(_ style: SecondLineStyle) -> Double {
        switch style {
        case .hidden, .translation:
            return 1
        case .untranslated, .retrying, .expired:
            return markerFontScale
        }
    }

    /// 正常译文的颜色来自用户样式，这里返回 nil。失败标记用未翻译灰。
    static func secondLineColorHex(_ style: SecondLineStyle) -> String? {
        switch style {
        case .hidden, .translation:
            return nil
        case .untranslated, .retrying, .expired:
            return DesignTokens.Color.untranslated
        }
    }

    /// 只有「未翻译 ⟳」能点。翻译中和已过期是静态文字。
    static func secondLineClickable(_ style: SecondLineStyle) -> Bool {
        style == .untranslated
    }

    /// 标记文字在字段里的命中矩形：左对齐，宽高按文字本身，不占满整行。
    static func markerTextRect(fieldRect: CGRect, textSize: CGSize) -> CGRect {
        let width = min(max(textSize.width.rounded(.up), 0), fieldRect.width)
        let height = min(max(textSize.height.rounded(.up), 0), fieldRect.height)
        let y = fieldRect.minY + (fieldRect.height - height) / 2
        return CGRect(x: fieldRect.minX, y: y, width: width, height: height)
    }

    /// 鼠标穿透、不可点、或点在文字矩形外，都不算命中标记。
    static func markerClickHits(point: CGPoint, textRect: CGRect, clickThrough: Bool, clickable: Bool) -> Bool {
        if clickThrough || !clickable || textRect.isNull || textRect.isEmpty {
            return false
        }
        return textRect.contains(point)
    }

    /// 每句显示什么。(原文, 译文?, 失败状态, 显示模式) → (第一行, 第二行, 第二行样式)。
    /// 非空译文优先：失败之后译文到达，第二行恢复成正常译文。
    static func pairLines(
        source: String,
        translation: String?,
        failure: FailureState,
        mode: DisplayMode
    ) -> PairLines {
        let translated = translation?.isEmpty == false ? translation : nil
        let cue: FailureState = translated == nil ? failure : .none
        let marker = markerLine(cue)
        switch mode {
        case .sourceOnly:
            return PairLines(first: source, second: nil, secondStyle: .hidden)
        case .bilingual:
            if let marker {
                return PairLines(first: source, second: marker.text, secondStyle: marker.style)
            }
            if let translated {
                return PairLines(first: source, second: translated, secondStyle: .translation)
            }
            return PairLines(first: source, second: nil, secondStyle: .hidden)
        case .translationOnly:
            // 仅译文在失败时改画出原文，避免这一句因为没有译文而整行消失。
            if let marker {
                return PairLines(first: source, second: marker.text, secondStyle: marker.style)
            }
            if let translated {
                return PairLines(first: nil, second: translated, secondStyle: .translation)
            }
            return PairLines(first: nil, second: nil, secondStyle: .hidden)
        }
    }

    private static func markerLine(_ cue: FailureState) -> (text: String, style: SecondLineStyle)? {
        switch cue {
        case .none:
            return nil
        case .failed:
            return (DesignTokens.Text.untranslated, .untranslated)
        case .retrying:
            return (retryingText, .retrying)
        case .expired:
            return (expiredText, .expired)
        }
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        func near(_ value: Double, _ expected: Double) -> Bool {
            abs(value - expected) < 0.000_001
        }

        let hold = DesignTokens.Motion.minHoldSec
        check(visibleLineKeys(shown: [], now: 0, lines: 2, minHoldSec: hold).isEmpty, "最短停留：空")
        check(visibleLineKeys(shown: [ShownLine(key: 1, shownAt: 0)], now: 10, lines: 2) == [1], "最短停留：不足行数全部留下")
        let two = [
            ShownLine(key: 1, shownAt: 0),
            ShownLine(key: 2, shownAt: 1),
        ]
        check(visibleLineKeys(shown: two, now: 10, lines: 2) == [1, 2], "最短停留：刚好行数")
        let aged = [
            ShownLine(key: 1, shownAt: 0),
            ShownLine(key: 2, shownAt: 1),
            ShownLine(key: 3, shownAt: 2),
        ]
        check(visibleLineKeys(shown: aged, now: 10, lines: 2, minHoldSec: hold) == [2, 3], "最短停留：满了只留行数")
        check(visibleLineKeys(shown: aged, now: 3, lines: 2, minHoldSec: hold) == [2, 3], "最短停留：刚好满 minHold 允许顶出")
        let fresh = [
            ShownLine(key: 1, shownAt: 8),
            ShownLine(key: 2, shownAt: 8.5),
            ShownLine(key: 3, shownAt: 9),
        ]
        check(visibleLineKeys(shown: fresh, now: 10, lines: 2, minHoldSec: hold) == [1, 2, 3], "最短停留：未满多显示一行")
        let crowded = (1...5).map { ShownLine(key: $0, shownAt: 9.5) }
        check(visibleLineKeys(shown: crowded, now: 10, lines: 2, minHoldSec: hold) == [3, 4, 5], "最短停留：上限 lines+1")
        check(visibleLineKeys(shown: crowded, now: 10, lines: 1, minHoldSec: hold) == [4, 5], "最短停留：行数为 1 时上限 2")
        check(visibleLineKeys(shown: fresh, now: 10, lines: 0).isEmpty, "最短停留：行数 0 为空")
        let boundary = [
            ShownLine(key: 1, shownAt: 0.1),
            ShownLine(key: 2, shownAt: 1),
            ShownLine(key: 3, shownAt: 2),
        ]
        check(visibleLineKeys(shown: boundary, now: 3, lines: 2, minHoldSec: 3) == [1, 2, 3], "最短停留：差一点点仍未满")

        let waterfallShown = (1...4).map { ShownLine(key: $0, shownAt: 0) }
        let waterfallBaseFit = WaterfallFit(pairCount: 2, clipsTop: false)
        let waterfallHolding = waterfallHoldCount(
            shown: waterfallShown,
            now: 1,
            fit: waterfallBaseFit,
            reviewing: false,
            minHoldSec: hold
        )
        check(waterfallHolding == WaterfallFit(pairCount: 3, clipsTop: true), "瀑布流最短停留：未满多留一对并裁顶")
        check(
            waterfallHoldCount(
                shown: waterfallShown,
                now: 1,
                fit: WaterfallFit(pairCount: 3, clipsTop: true),
                reviewing: false,
                minHoldSec: hold
            ) == WaterfallFit(pairCount: 3, clipsTop: true),
            "瀑布流最短停留：顶部半可见句已计入，不再额外加一对"
        )
        let waterfallAged = waterfallHoldCount(
            shown: waterfallShown,
            now: hold,
            fit: waterfallBaseFit,
            reviewing: false,
            minHoldSec: hold
        )
        check(waterfallAged == waterfallBaseFit, "瀑布流最短停留：满了不再多留")
        let waterfallCrowded = (1...6).map { ShownLine(key: $0, shownAt: 9) }
        check(
            waterfallHoldCount(
                shown: waterfallCrowded,
                now: 10,
                fit: WaterfallFit(pairCount: 2, clipsTop: false),
                reviewing: false,
                minHoldSec: hold
            ) == WaterfallFit(pairCount: 3, clipsTop: true),
            "瀑布流最短停留：最多只多留一对"
        )
        check(
            waterfallHoldCount(
                shown: [],
                now: 10,
                fit: WaterfallFit(pairCount: 0, clipsTop: false),
                reviewing: false,
                minHoldSec: hold
            ) == WaterfallFit(pairCount: 0, clipsTop: false),
            "瀑布流最短停留：空池不动"
        )
        check(
            waterfallHoldCount(
                shown: waterfallShown,
                now: 1,
                fit: waterfallBaseFit,
                reviewing: true,
                minHoldSec: hold
            ) == waterfallBaseFit,
            "瀑布流最短停留：回看不保留"
        )

        let gap = DesignTokens.Space.pairGap
        let pad = DesignTokens.Space.windowPadV * 2
        let exact = waterfallFit(windowHeight: pad + 48, pairHeight: 20, verticalPadding: pad, pairGap: gap)
        check(exact.pairCount == 2 && !exact.clipsTop, "瀑布流句对数：刚好两对")
        let onePixelShortOfSliver = waterfallFit(windowHeight: pad + 49, pairHeight: 20, verticalPadding: pad, pairGap: gap)
        check(onePixelShortOfSliver.pairCount == 2 && !onePixelShortOfSliver.clipsTop, "瀑布流句对数：余量不够句间距")
        let sliver = waterfallFit(windowHeight: pad + 57, pairHeight: 20, verticalPadding: pad, pairGap: gap)
        check(sliver.pairCount == 3 && sliver.clipsTop, "瀑布流句对数：多出一像素裁掉顶部")
        let exactOne = waterfallFit(windowHeight: pad + 20, pairHeight: 20, verticalPadding: pad, pairGap: gap)
        check(exactOne.pairCount == 1 && !exactOne.clipsTop, "瀑布流句对数：刚好一对")
        let short = waterfallFit(windowHeight: pad + 19, pairHeight: 20, verticalPadding: pad, pairGap: gap)
        check(short.pairCount == 1 && short.clipsTop, "瀑布流句对数：不够一对仍裁着显示")
        check(waterfallPairCount(windowHeight: pad, pairHeight: 20, verticalPadding: pad, pairGap: gap) == 0, "瀑布流句对数：只剩内边距")
        check(waterfallPairCount(windowHeight: pad - 1, pairHeight: 20, verticalPadding: pad, pairGap: gap) == 0, "瀑布流句对数：高度小于内边距")
        check(waterfallPairCount(windowHeight: 200, pairHeight: 0, verticalPadding: 0, pairGap: 0) == 0, "瀑布流句对数：句高为 0")
        let noGap = waterfallFit(windowHeight: 180, pairHeight: 30, verticalPadding: 0, pairGap: 0)
        check(noGap.pairCount == 6 && !noGap.clipsTop, "瀑布流句对数：整除无间距")
        let noGapClip = waterfallFit(windowHeight: 181, pairHeight: 30, verticalPadding: 0, pairGap: 0)
        check(noGapClip.pairCount == 7 && noGapClip.clipsTop, "瀑布流句对数：整除后再多 1")
        let variable = waterfallFit(availableHeight: 57, pairHeightsBottomFirst: [20, 20, 20], pairGap: 8)
        check(variable.pairCount == 3 && variable.clipsTop, "瀑布流句对数：不等高时顶部裁切")
        let variableGap = waterfallFit(availableHeight: 50, pairHeightsBottomFirst: [20, 20, 20], pairGap: 8)
        check(variableGap.pairCount == 2 && !variableGap.clipsTop, "瀑布流句对数：不等高余量不够句间距")
        let variableExact = waterfallFit(availableHeight: 48, pairHeightsBottomFirst: [20, 20, 20], pairGap: 8)
        check(variableExact.pairCount == 2 && !variableExact.clipsTop, "瀑布流句对数：不等高刚好两对")

        check(!isWaterfall(userHeight: nil, linesHeight: 200), "瀑布流判定：无用户高度")
        check(isWaterfall(userHeight: 300, linesHeight: 200), "瀑布流判定：高度大于行数所需")
        check(!isWaterfall(userHeight: 200, linesHeight: 200), "瀑布流判定：高度等于行数所需")
        check(!isWaterfall(userHeight: 180, linesHeight: 200), "瀑布流判定：高度小于行数所需")
        check(!shouldClearUserHeight(userHeight: nil, linesHeight: 200), "退出瀑布流：无用户高度不清")
        check(!shouldClearUserHeight(userHeight: 300, linesHeight: 200), "退出瀑布流：仍然更高不清")
        check(shouldClearUserHeight(userHeight: 200, linesHeight: 200), "退出瀑布流：等于行数所需要清高度")
        check(shouldClearUserHeight(userHeight: 180, linesHeight: 200), "退出瀑布流：小于行数所需要清高度")
        check(!isWaterfall(userHeight: 201, linesHeight: 200, slack: waterfallSlack), "瀑布流判定：1pt 误差内不进入")
        check(isWaterfall(userHeight: 202, linesHeight: 200, slack: waterfallSlack), "瀑布流判定：超过误差进入")
        check(shouldClearUserHeight(userHeight: 201, linesHeight: 200, slack: waterfallSlack), "退出瀑布流：误差内清高度")

        var review = CaptionReview.live
        check(!review.isReviewing && !review.showsBadge, "回看：初始在底部")
        review = review.scrolled(by: 1, now: 100, maxOffset: 10)
        check(review.isReviewing && review.offsetFromBottom == 1 && !review.showsBadge, "回看：向上滚动进入回看")
        review = review.newSentenceArrived()
        check(review.isReviewing && review.showsBadge && review.offsetFromBottom == 2, "回看：新句到达显示标签且不回底部")
        let early = review.tick(now: 100 + CaptionReview.idleSec - 0.1)
        check(early.isReviewing && early.showsBadge, "回看：未满 8 秒保持")
        let expired = review.tick(now: 100 + CaptionReview.idleSec)
        check(!expired.isReviewing && !expired.showsBadge && expired.offsetFromBottom == 0, "回看：8 秒无操作回底部")
        var clicked = CaptionReview.live.scrolled(by: 3, now: 0, maxOffset: 10).newSentenceArrived()
        check(clicked.showsBadge, "回看：点击前标签可见")
        clicked = clicked.jumpToBottom()
        check(!clicked.isReviewing && !clicked.showsBadge, "回看：点击标签回底部")
        let liveArrival = CaptionReview.live.newSentenceArrived()
        check(!liveArrival.isReviewing && !liveArrival.showsBadge && liveArrival.offsetFromBottom == 0, "回看：贴底时新句不显示标签")
        let back = CaptionReview.live.scrolled(by: 2, now: 5, maxOffset: 4).scrolled(by: -2, now: 6, maxOffset: 4)
        check(!back.isReviewing && !back.showsBadge, "回看：滚回底部退出")
        check(reviewScrollDelta(rawSteps: 2, clickThrough: true, waterfall: true) == 0, "回看：鼠标穿透不响应滚轮")
        check(reviewScrollDelta(rawSteps: 2, clickThrough: false, waterfall: false) == 0, "回看：固定行数不翻历史")
        check(reviewScrollDelta(rawSteps: 2, clickThrough: false, waterfall: true) == 2, "回看：瀑布流滚轮有效")

        check(near(pairOpacity(indexFromNewest: 0, visibleCount: 3, waterfall: false), 1), "旧句变暗：最新一对全亮")
        check(near(pairOpacity(indexFromNewest: 1, visibleCount: 3, waterfall: false), DesignTokens.Opacity.olderPair), "旧句变暗：固定模式用 olderPair")
        check(near(pairOpacity(indexFromNewest: 1, visibleCount: 4, waterfall: true), DesignTokens.Opacity.olderPair), "瀑布流变淡：次新为 olderPair")
        check(near(pairOpacity(indexFromNewest: 3, visibleCount: 4, waterfall: true), DesignTokens.Opacity.waterfallOldest), "瀑布流变淡：最旧约 0.35")

        let reduced = motionSeconds(reduceMotion: true)
        check(reduced.fade == 0 && reduced.shift == 0 && reduced.resize == 0, "减少动态效果：时长为 0")
        let full = motionSeconds(reduceMotion: false)
        check(near(full.fade, DesignTokens.Motion.fadeInMs / 1000), "动效：淡入秒数")
        check(near(full.shift, DesignTokens.Motion.shiftUpMs / 1000), "动效：上移秒数")
        check(near(full.resize, DesignTokens.Motion.resizeMs / 1000), "动效：高度秒数")
        check(motionSeconds(milliseconds: DesignTokens.Motion.fadeInMs, reduceMotion: true) == 0, "减少动态效果：淡入为 0")
        check(motionSeconds(milliseconds: DesignTokens.Motion.shiftUpMs, reduceMotion: true) == 0, "减少动态效果：上移为 0")
        check(motionSeconds(milliseconds: DesignTokens.Motion.resizeMs, reduceMotion: true) == 0, "减少动态效果：高度为 0")

        check(LineStore.displayHistoryLimit == 60, "瀑布流历史上限 60 对")
        check(waterfallMenuHint == "拉高窗口可显示更多（瀑布流）", "行数菜单说明文案")
        check(newCaptionBadgeTitle == "↓ 新字幕", "新字幕标签文案")
        check(CaptionReview.idleSec == 8, "回看无操作 8 秒")

        let source = "Hallo"
        let translated = "你好"
        let untranslated = DesignTokens.Text.untranslated
        let rows: [(String, String?, FailureState, DisplayMode, String?, String?, SecondLineStyle)] = [
            ("未翻译：双语·有译文", translated, .none, .bilingual, source, translated, .translation),
            ("未翻译：双语·失败", nil, .failed, .bilingual, source, untranslated, .untranslated),
            ("未翻译：双语·重试中", nil, .retrying, .bilingual, source, retryingText, .retrying),
            ("未翻译：双语·已过期", nil, .expired(isStatic: true), .bilingual, source, expiredText, .expired),
            ("未翻译：双语·失败后译文到达", translated, .failed, .bilingual, source, translated, .translation),
            ("未翻译：仅译文·有译文", translated, .none, .translationOnly, nil, translated, .translation),
            ("未翻译：仅译文·失败", nil, .failed, .translationOnly, source, untranslated, .untranslated),
            ("未翻译：仅译文·重试中", nil, .retrying, .translationOnly, source, retryingText, .retrying),
            ("未翻译：仅译文·已过期", nil, .expired(isStatic: false), .translationOnly, source, expiredText, .expired),
            ("未翻译：仅译文·失败后译文到达", translated, .retrying, .translationOnly, nil, translated, .translation),
            ("未翻译：仅原文·有译文", translated, .none, .sourceOnly, source, nil, .hidden),
            ("未翻译：仅原文·失败", nil, .failed, .sourceOnly, source, nil, .hidden),
            ("未翻译：仅原文·重试中", nil, .retrying, .sourceOnly, source, nil, .hidden),
            ("未翻译：仅原文·已过期", nil, .expired(isStatic: true), .sourceOnly, source, nil, .hidden),
            ("未翻译：仅原文·失败后译文到达", translated, .expired(isStatic: true), .sourceOnly, source, nil, .hidden),
        ]
        for row in rows {
            let got = pairLines(source: source, translation: row.1, failure: row.2, mode: row.3)
            check(
                got.first == row.4 && got.second == row.5 && got.secondStyle == row.6,
                row.0
            )
        }
        let waiting = pairLines(source: source, translation: nil, failure: .none, mode: .translationOnly)
        check(waiting.first == nil && waiting.second == nil && waiting.secondStyle == .hidden, "未翻译：仅译文·等待中不显示")
        let empty = pairLines(source: source, translation: "", failure: .failed, mode: .bilingual)
        check(empty.second == untranslated && empty.secondStyle == .untranslated, "未翻译：空译文视为失败标记")
        let holding = pairLines(source: source, translation: nil, failure: .expired(isStatic: false), mode: .bilingual)
        let locked = pairLines(source: source, translation: nil, failure: .expired(isStatic: true), mode: .bilingual)
        check(holding == locked && holding.second == expiredText, "未翻译：已过期锁定前后文案一致")
        check(markerFontScale == 0.8, "未翻译：标记字号比例 0.8")
        check(expiredLockSec == 3, "未翻译：已过期锁定 3 秒")
        check(secondLineFontScale(.untranslated) == markerFontScale, "未翻译：失败标记字号")
        check(secondLineFontScale(.retrying) == markerFontScale, "未翻译：翻译中字号")
        check(secondLineFontScale(.expired) == markerFontScale, "未翻译：已过期字号")
        check(secondLineFontScale(.translation) == 1, "未翻译：正常译文字号不变")
        check(secondLineColorHex(.untranslated) == DesignTokens.Color.untranslated, "未翻译：失败标记颜色")
        check(secondLineColorHex(.retrying) == DesignTokens.Color.untranslated, "未翻译：翻译中颜色")
        check(secondLineColorHex(.expired) == DesignTokens.Color.untranslated, "未翻译：已过期灰色")
        check(secondLineColorHex(.translation) == nil, "未翻译：正常译文沿用译文色")
        check(secondLineClickable(.untranslated), "未翻译：失败标记可点")
        check(!secondLineClickable(.retrying), "未翻译：翻译中不可点")
        check(!secondLineClickable(.expired), "未翻译：已过期不可点")
        check(!secondLineClickable(.translation) && !secondLineClickable(.hidden), "未翻译：正常译文和空行不可点")
        check(markerAfterRetry(accepted: true) == .retrying, "未翻译：重试接受进入翻译中")
        check(markerAfterRetry(accepted: false) == .expired(isStatic: false), "未翻译：重试拒绝先显示已过期")
        check(markerSettled(.expired(isStatic: false), elapsed: 2.9) == .expired(isStatic: false), "未翻译：已过期未满 3 秒")
        check(markerSettled(.expired(isStatic: false), elapsed: 3) == .expired(isStatic: true), "未翻译：已过期满 3 秒锁定静态")
        check(markerSettled(.expired(isStatic: true), elapsed: 0) == .expired(isStatic: true), "未翻译：已锁定不再退回")
        check(markerSettled(.retrying, elapsed: 9) == .retrying, "未翻译：翻译中不因计时变成已过期")
        let field = CGRect(x: 0, y: 100, width: 400, height: 36)
        let textRect = markerTextRect(fieldRect: field, textSize: CGSize(width: 70.2, height: 16))
        check(textRect.width == 71 && textRect.height == 16 && textRect.minX == field.minX, "未翻译：命中矩形只取文字宽")
        check(textRect.maxX < field.maxX, "未翻译：命中矩形不占满整行")
        let inside = CGPoint(x: textRect.midX, y: textRect.midY)
        let beside = CGPoint(x: textRect.maxX + 4, y: textRect.midY)
        check(markerClickHits(point: inside, textRect: textRect, clickThrough: false, clickable: true), "未翻译：文字内可点")
        check(!markerClickHits(point: beside, textRect: textRect, clickThrough: false, clickable: true), "未翻译：文字右侧空白不点")
        check(!markerClickHits(point: inside, textRect: textRect, clickThrough: true, clickable: true), "未翻译：鼠标穿透不点")
        check(!markerClickHits(point: inside, textRect: textRect, clickThrough: false, clickable: false), "未翻译：已过期静态文字不点")
        return failures
    }
}

struct CaptionReview: Equatable {
    var offsetFromBottom: Int
    var showsBadge: Bool
    var lastInputAt: Double

    static let idleSec = 8.0
    static let live = CaptionReview(offsetFromBottom: 0, showsBadge: false, lastInputAt: 0)

    var isReviewing: Bool { offsetFromBottom > 0 }

    func scrolled(by delta: Int, now: Double, maxOffset: Int) -> CaptionReview {
        let upper = max(0, maxOffset)
        let next = min(max(offsetFromBottom + delta, 0), upper)
        if next == 0 {
            return CaptionReview(offsetFromBottom: 0, showsBadge: false, lastInputAt: now)
        }
        return CaptionReview(offsetFromBottom: next, showsBadge: showsBadge, lastInputAt: now)
    }

    func newSentenceArrived() -> CaptionReview {
        guard isReviewing else {
            return CaptionReview(offsetFromBottom: 0, showsBadge: false, lastInputAt: lastInputAt)
        }
        return CaptionReview(offsetFromBottom: offsetFromBottom + 1, showsBadge: true, lastInputAt: lastInputAt)
    }

    func tick(now: Double, idleSec: Double = CaptionReview.idleSec) -> CaptionReview {
        guard isReviewing, now - lastInputAt >= idleSec else { return self }
        return .live
    }

    func jumpToBottom() -> CaptionReview {
        CaptionReview(offsetFromBottom: 0, showsBadge: false, lastInputAt: lastInputAt)
    }

    func clamped(historyCount: Int) -> CaptionReview {
        let maxOffset = max(0, historyCount - 1)
        guard offsetFromBottom > maxOffset else { return self }
        if maxOffset == 0 {
            return CaptionReview(offsetFromBottom: 0, showsBadge: false, lastInputAt: lastInputAt)
        }
        return CaptionReview(offsetFromBottom: maxOffset, showsBadge: showsBadge, lastInputAt: lastInputAt)
    }
}
