import AppKit
import Foundation
import QuartzCore

private final class OverlayPanel: NSPanel {
    var arrowKeyHandler: ((NSEvent) -> Bool)?

    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }

    override func keyDown(with event: NSEvent) {
        if arrowKeyHandler?(event) == true { return }
        super.keyDown(with: event)
    }
}

/// 内容区盖住文字。NSTextField 的 mouseDownCanMoveWindow 恒为 false，按在字幕上拖不动。
/// 四边留给系统缩放：hitTest 返回 nil 后，事件才会落到无边框窗口的边缘缩放上。
private final class OverlayRootView: NSVisualEffectView {
    var resizeInset: CGFloat = 5

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard !isHidden, alphaValue > 0 else { return nil }
        if window?.ignoresMouseEvents == true { return nil }
        let local = convert(point, from: superview)
        guard bounds.contains(local) else { return nil }
        if local.x <= resizeInset || local.y <= resizeInset ||
            local.x >= bounds.width - resizeInset || local.y >= bounds.height - resizeInset {
            return nil
        }
        return super.hitTest(point)
    }
}

/// 盖在「未翻译 ⟳」文字上的命中层。范围只有文字本身，点下去重试，不拖窗口。
private final class UntranslatedMarkerHitView: NSView {
    var key = 0
    var onClick: ((Int) -> Void)?

    override var isOpaque: Bool { false }
    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard !isHidden, alphaValue > 0, window?.ignoresMouseEvents != true else { return nil }
        let local = convert(point, from: superview)
        return bounds.contains(local) ? self : nil
    }

    override func mouseDown(with event: NSEvent) {
        guard window?.ignoresMouseEvents != true else { return }
        onClick?(key)
    }

    override func scrollWheel(with event: NSEvent) {
        nextResponder?.scrollWheel(with: event)
    }
}

private final class OverlayDragSurfaceView: NSView {
    var scrollHandler: ((NSEvent) -> Void)?
    private var markerHits: [Int: UntranslatedMarkerHitView] = [:]

    override func acceptsFirstMouse(for event: NSEvent?) -> Bool { true }

    /// 按文字矩形铺命中层。不在列表里的旧层去掉，避免滚出画面后还能点到。
    func setMarkerHits(_ hits: [(key: Int, rect: NSRect)], onClick: @escaping (Int) -> Void) {
        let live = Set(hits.map(\.key))
        for key in Array(markerHits.keys) where !live.contains(key) {
            markerHits[key]?.removeFromSuperview()
            markerHits.removeValue(forKey: key)
        }
        for hit in hits {
            let view = markerHits[hit.key] ?? UntranslatedMarkerHitView(frame: hit.rect)
            view.key = hit.key
            view.onClick = onClick
            view.frame = hit.rect
            if view.superview !== self {
                addSubview(view)
                markerHits[hit.key] = view
            }
        }
    }

    override func hitTest(_ point: NSPoint) -> NSView? {
        guard !isHidden, alphaValue > 0, window?.ignoresMouseEvents != true else { return nil }
        let local = convert(point, from: superview)
        guard bounds.contains(local) else { return nil }
        // 标记文字的范围交给标记视图；其余区域仍由拖动层接收。
        for marker in markerHits.values {
            if let hit = marker.hitTest(local) {
                return hit
            }
        }
        return self
    }

    override func mouseDown(with event: NSEvent) {
        guard let window, !window.ignoresMouseEvents else { return }
        window.performDrag(with: event)
        window.makeKey()
    }

    override func scrollWheel(with event: NSEvent) {
        if let scrollHandler {
            scrollHandler(event)
            return
        }
        super.scrollWheel(with: event)
    }
}

/// 句对容器。源文和译文各一个标签，按 key 复用，不在每次重画时新建。
private final class SubtitlePairView: NSStackView {
    let key: Int
    let sourceField = NSTextField(labelWithString: "")
    let translationField = NSTextField(labelWithString: "")
    var sourceSignature = ""
    var translationSignature = ""
    var didFadeIn = false
    var appliedOpacity = -1.0
    /// 第二行当前是可点的「未翻译 ⟳」。拖动层只为这种句子让出文字范围。
    var markerClickable = false

    init(key: Int) {
        self.key = key
        super.init(frame: .zero)
        orientation = .vertical
        alignment = .leading
        spacing = OverlayStyle.pairGap
        translatesAutoresizingMaskIntoConstraints = false
        setHuggingPriority(.required, for: .vertical)
        configure(sourceField)
        configure(translationField)
        addArrangedSubview(sourceField)
        addArrangedSubview(translationField)
    }

    required init?(coder: NSCoder) {
        return nil
    }

    private func configure(_ field: NSTextField) {
        field.translatesAutoresizingMaskIntoConstraints = false
        field.lineBreakMode = .byWordWrapping
        field.maximumNumberOfLines = 3
        field.setContentCompressionResistancePriority(.required, for: .vertical)
        if let cell = field.cell as? NSTextFieldCell {
            cell.wraps = true
            cell.isScrollable = false
        }
    }
}

/// 字幕区。内容高出窗口时由顶部渐隐裁切，而不是硬切。
private final class FadingClipView: NSView {
    var fadesTop = false {
        didSet {
            if fadesTop != oldValue { needsLayout = true }
        }
    }

    private let gradient = CAGradientLayer()
    private let topFade: CGFloat = 36

    override init(frame frameRect: NSRect) {
        super.init(frame: frameRect)
        wantsLayer = true
    }

    required init?(coder: NSCoder) {
        return nil
    }

    override func layout() {
        super.layout()
        guard let layer else { return }
        layer.masksToBounds = true
        guard fadesTop, bounds.height > 1 else {
            layer.mask = nil
            return
        }
        let fade = min(0.45, topFade / bounds.height)
        gradient.frame = bounds
        // 非翻转图层：y = 1 是顶边。顶边透明，往下很快恢复不透明。
        gradient.startPoint = CGPoint(x: 0.5, y: 1)
        gradient.endPoint = CGPoint(x: 0.5, y: 0)
        gradient.colors = [
            NSColor.clear.cgColor,
            NSColor.white.cgColor,
            NSColor.white.cgColor,
        ]
        gradient.locations = [0, NSNumber(value: Double(fade)), 1]
        layer.mask = gradient
    }
}

private final class ReviewBadgeButton: NSButton {
    var scrollHandler: ((NSEvent) -> Void)?

    override var intrinsicContentSize: NSSize {
        var size = super.intrinsicContentSize
        size.width += 16
        size.height += 6
        return size
    }

    override func scrollWheel(with event: NSEvent) {
        if let scrollHandler {
            scrollHandler(event)
            return
        }
        super.scrollWheel(with: event)
    }
}

@MainActor
final class OverlayController: NSObject {
    private let panel: OverlayPanel
    private let root: OverlayRootView
    private let tint: NSView
    private let dragSurface: OverlayDragSurfaceView
    private let stack = NSStackView()
    private let clipView = FadingClipView(frame: .zero)
    private let reviewBadge = ReviewBadgeButton(title: OverlayMotion.newCaptionBadgeTitle, target: nil, action: nil)
    private let volatileLabel = NSTextField(labelWithString: "")
    /// 当前识别层。和草稿分开，避免 setStatus/setVolatile 把「云端·Groq」盖掉。
    private let tierLabel = NSTextField(labelWithString: "本机")
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let statusMenu = NSMenu()
    private let styleMenu = NSMenu(title: "字幕样式")
    private let styleMenuItem = NSMenuItem(title: "字幕样式", action: nil, keyEquivalent: "")
    private let pauseItem = NSMenuItem(title: "暂停", action: #selector(togglePause), keyEquivalent: "")
    private let clickThroughItem = NSMenuItem(title: "鼠标穿透", action: #selector(toggleClickThrough), keyEquivalent: "")

    // 字幕历史（含已滚出屏幕的行）。固定行数只画末尾 style.lines 对；瀑布流最多画 displayHistoryLimit 对。
    private var store = LineStore()
    private var pairViews: [Int: SubtitlePairView] = [:]
    private var appearedAt: [Int: Double] = [:]
    private var renderedKeys: Set<Int> = []
    private var review = CaptionReview.live
    private var reviewIdleTask: Task<Void, Never>?
    private var holdTask: Task<Void, Never>?
    private var didRenderOnce = false
    private var waterfallActive = false
    private var scrollAccumulator: CGFloat = 0
    /// 译文可能先于 MainActor 上的 applyNode 回来。先按 key 记下，行出现再补上。
    private var pendingTranslation: [Int: String] = [:]
    /// 翻译失败的句子。键是句子 id，瀑布流和回看里的旧句用同一份。
    private var failures: [Int: OverlayMotion.FailureState] = [:]
    /// 失败原因只记账，不改标记文案（error / unavailable 都显示「未翻译 ⟳」）。
    private var failureReasons: [Int: String] = [:]
    private var expireTasks: [Int: Task<Void, Never>] = [:]
    /// 标记被正常译文替换时，下一帧给译文做一次现有淡入。
    private var fadeTranslationIDs: Set<Int> = []
    private var translationFading: Set<Int> = []
    private var style: OverlayStyle
    private var isPaused = false
    private var originSaveTask: Task<Void, Never>?
    private var isApplyingProgrammaticFrame = false
    /// 用户开始拖边时的尺寸。松手时只把真正拉过的边写进 style，避免拉宽把高度误存成用户高度。
    private var liveResizeStart: NSSize?
    private let onPauseChanged: (Bool) -> Void
    private let onQuit: () -> Void
    private let onRetry: (Int) -> Bool

    init(
        onPauseChanged: @escaping (Bool) -> Void,
        onQuit: @escaping () -> Void,
        onRetry: @escaping (Int) -> Bool = { _ in false }
    ) {
        self.onPauseChanged = onPauseChanged
        self.onQuit = onQuit
        self.onRetry = onRetry
        let loaded = OverlayStyleStore.load()
        self.style = loaded

        let frame = Self.initialFrame(for: loaded)
        panel = OverlayPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel, .resizable],
            backing: .buffered,
            defer: false
        )
        root = OverlayRootView(frame: NSRect(origin: .zero, size: frame.size))
        tint = NSView(frame: NSRect(origin: .zero, size: frame.size))
        dragSurface = OverlayDragSurfaceView(frame: NSRect(origin: .zero, size: frame.size))

        super.init()
        configurePanel()
        configureStatusItem()
        panel.delegate = self
        panel.arrowKeyHandler = { [weak self] event in
            self?.handleArrowKey(event) ?? false
        }
        dragSurface.scrollHandler = { [weak self] event in
            self?.handleScroll(event)
        }
        applyChrome()
        applyTypography()
        render(animate: false)
    }

    func show() {
        panel.orderFrontRegardless()
    }

    func setVolatile(_ text: String) {
        // 草稿只看最新的尾巴：两行放不下时从前面截，截在词边界上
        let limit = 140
        var shown = text
        if shown.count > limit {
            let tail = shown.suffix(limit)
            shown = "…" + (tail.firstIndex(of: " ").map { String(tail[tail.index(after: $0)...]) } ?? String(tail))
        }
        if volatileLabel.stringValue != shown {
            volatileLabel.stringValue = shown
        }
        render()
    }

    func addFinal(id: Int, text: String, t0: Double? = nil, t1: Double? = nil) {
        store.addLocal(key: id, t0: t0, t1: t1, text: text)
        render()
    }

    func addTranslation(id: Int, text: String) {
        let hadMarker = failures[id] != nil
        let alreadyShown = pairViews[id]?.didFadeIn == true
        clearFailure(id)
        if hadMarker && alreadyShown {
            fadeTranslationIDs.insert(id)
        }
        if store.lines.contains(where: { $0.key == id }) {
            store.setTranslation(key: id, text: text)
            pendingTranslation.removeValue(forKey: id)
        } else {
            pendingTranslation[id] = text
        }
        render()
    }

    /// 译文位置改显示「未翻译 ⟳」。同 id 的译文随后到达时由 addTranslation 换回正常译文。
    func markUntranslated(id: Int, reason: String) {
        if failures[id] == .failed, failureReasons[id] == reason {
            return
        }
        cancelExpire(id)
        fadeTranslationIDs.remove(id)
        translationFading.remove(id)
        failureReasons[id] = reason
        failures[id] = .failed
        render()
    }

    private func clearFailure(_ id: Int) {
        cancelExpire(id)
        failures[id] = nil
        failureReasons[id] = nil
    }

    private func cancelExpire(_ id: Int) {
        expireTasks[id]?.cancel()
        expireTasks[id] = nil
    }

    /// 点「未翻译 ⟳」。接受则改成「翻译中…」；拒绝则改成「已过期」，3 秒后锁定为不可再点的灰色静态文字。
    private func retryMarked(id: Int) {
        guard !panel.ignoresMouseEvents else { return }
        guard failures[id] == .failed else { return }
        let accepted = onRetry(id)
        failures[id] = OverlayMotion.markerAfterRetry(accepted: accepted)
        if accepted {
            cancelExpire(id)
        } else {
            scheduleExpireLock(id)
        }
        render()
    }

    private func scheduleExpireLock(_ id: Int) {
        cancelExpire(id)
        let wait = OverlayMotion.expiredLockSec
        expireTasks[id] = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            guard let current = self.failures[id] else { return }
            let settled = OverlayMotion.markerSettled(current, elapsed: wait)
            guard settled != current else { return }
            self.failures[id] = settled
            self.render(animate: false)
        }
    }

    private func beginTranslationFade(id: Int, field: NSTextField) {
        let fade = OverlayMotion.motionSeconds(milliseconds: DesignTokens.Motion.fadeInMs, reduceMotion: reduceMotion)
        translationFading.insert(id)
        guard fade > 0 else {
            field.alphaValue = 1
            translationFading.remove(id)
            return
        }
        field.alphaValue = 0
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = fade
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            field.animator().alphaValue = 1
        }, completionHandler: {
            Task { @MainActor in
                self.translationFading.remove(id)
            }
        })
    }

    /// 瀑布流和回看里可见的「未翻译 ⟳」都铺一层与文字等大的命中视图。
    private func syncMarkerHits() {
        var hits: [(key: Int, rect: NSRect)] = []
        let clipInDrag = dragSurface.convert(clipView.bounds, from: clipView)
        for view in stack.arrangedSubviews {
            guard let pair = view as? SubtitlePairView, pair.markerClickable, !pair.isHidden else { continue }
            let field = pair.translationField
            guard !field.isHidden, field.bounds.width > 1, field.bounds.height > 1 else { continue }
            let font = field.font ?? NSFont.systemFont(
                ofSize: CGFloat(style.fontSize * OverlayMotion.secondLineFontScale(.untranslated)),
                weight: .semibold
            )
            let textSize = (field.stringValue as NSString).size(withAttributes: [.font: font])
            let drawing = field.cell?.drawingRect(forBounds: field.bounds) ?? field.bounds
            let localText = OverlayMotion.markerTextRect(fieldRect: drawing, textSize: textSize)
            guard OverlayMotion.markerClickHits(
                point: CGPoint(x: localText.midX, y: localText.midY),
                textRect: localText,
                clickThrough: false,
                clickable: true
            ) else { continue }
            let inDrag = field.convert(localText, to: dragSurface)
            let visible = inDrag.intersection(clipInDrag)
            if visible.isNull || visible.isEmpty || visible.width < 1 || visible.height < 1 { continue }
            hits.append((pair.key, visible))
        }
        dragSurface.setMarkerHits(hits) { [weak self] id in
            self?.retryMarked(id: id)
        }
    }

    /// 节点的精修句到达（P5）：整体替换被它覆盖的本机行，找不到就按时间插入。
    /// 云端译文未到时先继承被替换本机行的译文，到了再覆盖，避免替换瞬间空白。
    func addNodeFinal(nodeID: String, t0: Double?, t1: Double?, src: String, dst: String?) {
        let provided = dst?.trimmingCharacters(in: .whitespacesAndNewlines)
        let shown = (provided?.isEmpty == false ? provided : nil)
            ?? inheritedSubtitleTranslation(lines: store.lines, t0: t0, t1: t1)
        let before = Set(store.lines.map(\.key))
        store.applyNode(nodeID: nodeID, t0: t0, t1: t1, srcText: src, dstText: shown)
        if let created = store.lines.first(where: { !before.contains($0.key) }),
           let pending = pendingTranslation.removeValue(forKey: created.key) {
            store.setTranslation(key: created.key, text: pending)
        }
        render()
    }

    func setStatus(_ text: String) {
        if volatileLabel.stringValue != text {
            volatileLabel.stringValue = text
        }
        render()
    }

    /// 菜单栏标题显示当前识别在哪里：音频是否正在离开本机，用户要能一眼看出来。
    func setMode(_ mode: SubtitleMode) {
        setModeLabel(mode.label)
    }

    func setModeLabel(_ label: String) {
        statusItem.length = NSStatusItem.variableLength
        statusItem.button?.title = "字·\(label)"
        if tierLabel.stringValue != label {
            tierLabel.stringValue = label
        }
        render()
    }

    private static func initialFrame(for style: OverlayStyle) -> NSRect {
        let frames = NSScreen.screens.map(\.visibleFrame)
        let fallback = CGRect(x: 0, y: 0, width: 1280, height: 800)
        let main = NSScreen.main?.visibleFrame ?? fallback
        let anchor = CGPoint(x: style.originX ?? main.midX, y: style.originY ?? main.midY)
        let screen = OverlayStyle.preferredVisibleFrame(anchor: anchor, frames: frames, fallback: main)
        let defaultWidth = OverlayStyle.windowWidth(for: style.shape, visibleWidth: screen.width)
        let width: CGFloat
        if style.shape == .strip {
            width = defaultWidth
        } else if let savedWidth = style.width {
            width = CGFloat(savedWidth)
        } else {
            width = defaultWidth
        }
        let height = CGFloat(style.height ?? 220)
        let origin: CGPoint
        if let x = style.originX, let y = style.originY {
            origin = CGPoint(x: x, y: y)
        } else if style.shape == .strip {
            origin = CGPoint(x: screen.minX + 20, y: screen.minY)
        } else {
            origin = CGPoint(x: screen.midX - width / 2, y: screen.minY + 70)
        }
        return OverlayStyle.clampedFrame(
            x: origin.x,
            y: origin.y,
            windowWidth: width,
            windowHeight: height,
            visibleFrames: frames.isEmpty ? [screen] : frames
        )
    }

    private func configurePanel() {
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.becomesKeyOnlyIfNeeded = true
        // 菜单栏应用一点标题栏就会 deactivate；默认 hidesOnDeactivate 会把字幕面板收掉。
        panel.hidesOnDeactivate = false
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        updateSizeLimits(for: panel.frame)

        root.autoresizingMask = [.width, .height]
        root.material = .hudWindow
        root.blendingMode = .behindWindow
        root.state = .active
        root.wantsLayer = true
        // 浅色模式下 hudWindow 会渲染成浅灰底，白字/黄字看不清；强制暗色外观，
        // 再垫一层不受材质影响的底色（直接设 root 的 layer 底色会被材质盖掉）。
        root.appearance = NSAppearance(named: .darkAqua)

        tint.autoresizingMask = [.width, .height]
        tint.wantsLayer = true
        root.addSubview(tint)

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = OverlayStyle.pairGap
        stack.translatesAutoresizingMaskIntoConstraints = false
        stack.setHuggingPriority(.required, for: .vertical)
        stack.setContentCompressionResistancePriority(.required, for: .vertical)

        volatileLabel.textColor = .secondaryLabelColor
        volatileLabel.lineBreakMode = .byWordWrapping
        volatileLabel.maximumNumberOfLines = 2
        volatileLabel.preferredMaxLayoutWidth = textWidth

        tierLabel.textColor = .secondaryLabelColor
        tierLabel.font = .systemFont(ofSize: OverlayStyle.statusFontSize)
        tierLabel.lineBreakMode = .byTruncatingTail
        tierLabel.maximumNumberOfLines = 1
        tierLabel.preferredMaxLayoutWidth = textWidth

        clipView.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(clipView)
        clipView.addSubview(stack)
        stack.addArrangedSubview(volatileLabel)
        stack.addArrangedSubview(tierLabel)

        reviewBadge.target = self
        reviewBadge.action = #selector(jumpToLatest)
        reviewBadge.isBordered = false
        reviewBadge.font = .systemFont(ofSize: OverlayStyle.statusFontSize, weight: .medium)
        reviewBadge.wantsLayer = true
        reviewBadge.layer?.backgroundColor = badgeBackgroundColor().cgColor
        reviewBadge.layer?.cornerRadius = CGFloat(DesignTokens.Radius.small)
        reviewBadge.attributedTitle = NSAttributedString(
            string: OverlayMotion.newCaptionBadgeTitle,
            attributes: [
                .foregroundColor: nsColor(DesignTokens.Color.subtitleSource, fallback: OverlayStyle.defaultSourceColor),
                .font: NSFont.systemFont(ofSize: OverlayStyle.statusFontSize, weight: .medium),
            ]
        )
        reviewBadge.translatesAutoresizingMaskIntoConstraints = false
        reviewBadge.isHidden = true
        reviewBadge.scrollHandler = { [weak self] event in
            self?.handleScroll(event)
        }

        dragSurface.translatesAutoresizingMaskIntoConstraints = false
        root.addSubview(dragSurface)
        root.addSubview(reviewBadge)
        panel.contentView = root

        let padH = OverlayStyle.windowPadH
        let padV = OverlayStyle.windowPadV
        NSLayoutConstraint.activate([
            clipView.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: padH),
            clipView.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -padH),
            clipView.topAnchor.constraint(equalTo: root.topAnchor, constant: padV),
            clipView.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -padV),
            // 贴底。高出的句子从裁切区顶边溢出去，由渐隐遮罩收掉。
            stack.leadingAnchor.constraint(equalTo: clipView.leadingAnchor),
            stack.trailingAnchor.constraint(equalTo: clipView.trailingAnchor),
            stack.bottomAnchor.constraint(equalTo: clipView.bottomAnchor),
            dragSurface.leadingAnchor.constraint(equalTo: root.leadingAnchor),
            dragSurface.trailingAnchor.constraint(equalTo: root.trailingAnchor),
            dragSurface.topAnchor.constraint(equalTo: root.topAnchor),
            dragSurface.bottomAnchor.constraint(equalTo: root.bottomAnchor),
            reviewBadge.centerXAnchor.constraint(equalTo: root.centerXAnchor),
            reviewBadge.bottomAnchor.constraint(equalTo: root.bottomAnchor, constant: -(padV + 4)),
        ])
    }

    private func configureStatusItem() {
        statusItem.button?.title = "字·本机"
        statusMenu.autoenablesItems = false
        statusMenu.delegate = self

        pauseItem.target = self
        statusMenu.addItem(pauseItem)

        clickThroughItem.target = self
        clickThroughItem.state = .off
        statusMenu.addItem(clickThroughItem)

        statusMenu.addItem(.separator())
        styleMenu.autoenablesItems = false
        styleMenuItem.submenu = styleMenu
        statusMenu.addItem(styleMenuItem)
        rebuildStyleMenu()

        let bigger = NSMenuItem(title: "增大字号", action: #selector(increaseFontSize), keyEquivalent: "+")
        bigger.keyEquivalentModifierMask = [.command]
        bigger.target = self
        statusMenu.addItem(bigger)
        let smaller = NSMenuItem(title: "减小字号", action: #selector(decreaseFontSize), keyEquivalent: "-")
        smaller.keyEquivalentModifierMask = [.command]
        smaller.target = self
        statusMenu.addItem(smaller)

        statusMenu.addItem(.separator())
        let settings = NSMenuItem(title: "设置…", action: #selector(openSettings), keyEquivalent: ",")
        settings.keyEquivalentModifierMask = [.command]
        settings.target = self
        statusMenu.addItem(settings)

        statusMenu.addItem(.separator())
        let quit = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        statusMenu.addItem(quit)
        statusItem.menu = statusMenu
    }

    private func rebuildStyleMenu() {
        styleMenu.removeAllItems()
        styleMenu.addItem(submenu("字号", OverlayStyle.fontSizePresets.map { size in
            checked("\(Int(size))", on: abs(style.fontSize - size) < 0.01, action: #selector(chooseFontSize(_:)), tag: Int(size))
        }))
        styleMenu.addItem(submenu("原文颜色", OverlayStyle.textColorPresets.map { preset in
            checked(preset.title, on: OverlayStyle.canonicalHex(style.sourceColor) == preset.hex, action: #selector(chooseSourceColor(_:)), object: preset.hex)
        }))
        styleMenu.addItem(submenu("译文颜色", OverlayStyle.textColorPresets.map { preset in
            checked(preset.title, on: OverlayStyle.canonicalHex(style.translationColor) == preset.hex, action: #selector(chooseTranslationColor(_:)), object: preset.hex)
        }))
        styleMenu.addItem(submenu("背景", OverlayStyle.backgroundColorPresets.map { preset in
            checked(preset.title, on: OverlayStyle.canonicalHex(style.backgroundColor) == preset.hex, action: #selector(chooseBackgroundColor(_:)), object: preset.hex)
        }))
        styleMenu.addItem(submenu("透明度", OverlayStyle.opacityPresets.map { preset in
            checked(preset.title, on: abs(style.backgroundOpacity - preset.value) < 0.001, action: #selector(chooseOpacity(_:)), object: NSNumber(value: preset.value))
        }))
        styleMenu.addItem(submenu("文字边缘", OverlayStyle.textEdgeChoices.map { choice in
            checked(choice.title, on: style.textEdge == choice.value, action: #selector(chooseTextEdge(_:)), object: choice.value.rawValue)
        }))
        styleMenu.addItem(submenu("形状", OverlayStyle.shapeChoices.map { choice in
            checked(choice.title, on: style.shape == choice.value, action: #selector(chooseShape(_:)), object: choice.value.rawValue)
        }))
        styleMenu.addItem(submenu("行数", linesMenuItems()))
        styleMenu.addItem(submenu("显示模式", OverlayStyle.displayModeChoices.map { choice in
            checked(choice.title, on: style.displayMode == choice.value, action: #selector(chooseDisplayMode(_:)), object: choice.value.rawValue)
        }))
        styleMenu.addItem(.separator())
        let reset = NSMenuItem(title: "恢复默认", action: #selector(resetStyle), keyEquivalent: "")
        reset.target = self
        styleMenu.addItem(reset)
    }

    private func submenu(_ title: String, _ items: [NSMenuItem]) -> NSMenuItem {
        let menu = NSMenu(title: title)
        menu.autoenablesItems = false
        items.forEach(menu.addItem)
        let root = NSMenuItem(title: title, action: nil, keyEquivalent: "")
        root.submenu = menu
        return root
    }

    private func linesMenuItems() -> [NSMenuItem] {
        var items = (1...4).map { count in
            checked("\(count)", on: style.lines == count, action: #selector(chooseLines(_:)), tag: count)
        }
        let hint = NSMenuItem(title: OverlayMotion.waterfallMenuHint, action: nil, keyEquivalent: "")
        hint.attributedTitle = NSAttributedString(
            string: OverlayMotion.waterfallMenuHint,
            attributes: [
                .foregroundColor: NSColor.secondaryLabelColor,
                .font: NSFont.systemFont(ofSize: NSFont.smallSystemFontSize),
            ]
        )
        hint.isEnabled = false
        items.append(hint)
        return items
    }

    private func checked(_ title: String, on: Bool, action: Selector, object: Any? = nil, tag: Int = 0) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.state = on ? .on : .off
        item.representedObject = object
        item.tag = tag
        return item
    }

    private func render(animate: Bool = true) {
        updateWrapWidths()
        let width = textWidth
        let history = Array(store.lines.suffix(LineStore.displayHistoryLimit)).filter(isDisplayable)
        noteArrivals(in: history)
        review = review.clamped(historyCount: history.count)

        let liveKeys = Set(history.map(\.key))
        for key in pairViews.keys where !liveKeys.contains(key) {
            if let view = pairViews.removeValue(forKey: key) {
                // 不在 stack 里的视图（建了但还没轮到上屏）调 removeArrangedSubview 会抛
                // NSInternalInconsistencyException，见 CLAUDE.md §7 第 26 条。
                if stack.arrangedSubviews.contains(where: { $0 === view }) {
                    stack.removeArrangedSubview(view)
                }
                view.removeFromSuperview()
            }
            appearedAt.removeValue(forKey: key)
        }
        for line in history {
            let pair = pairViews[line.key] ?? SubtitlePairView(key: line.key)
            pairViews[line.key] = pair
            update(pair, line: line, width: width)
        }

        let heights = Dictionary(uniqueKeysWithValues: history.map { line in
            (line.key, measuredPairHeight(pairViews[line.key]!, width: width))
        })
        let statusHeight = statusBlockHeight(width: width)
        let fixedKeys = history.suffix(style.lines).map(\.key)
        let linesHeight = blockHeight(pairHeights: fixedKeys.map { heights[$0] ?? 0 }, statusHeight: statusHeight)
        if liveResizeStart == nil,
           OverlayMotion.shouldClearUserHeight(
            userHeight: style.height,
            linesHeight: Double(linesHeight),
            slack: OverlayMotion.waterfallSlack
           ) {
            style.height = nil
            scheduleStyleSave()
        }
        let effectiveHeight: Double? = liveResizeStart == nil ? style.height : Double(panel.frame.height)
        let waterfall = OverlayMotion.isWaterfall(
            userHeight: effectiveHeight,
            linesHeight: Double(linesHeight),
            slack: OverlayMotion.waterfallSlack
        )
        waterfallActive = waterfall

        let now = Date().timeIntervalSinceReferenceDate
        let visibleKeys: [Int]
        let clipsTop: Bool
        if waterfall {
            let pool = Array(history.dropLast(min(review.offsetFromBottom, history.count)))
                .filter { (heights[$0.key] ?? 0) > 0.5 }
            let bottomFirst = pool.reversed().map { Double(heights[$0.key] ?? 0) }
            let bridge = statusHeight > 0 && !pool.isEmpty ? OverlayStyle.pairGap : 0
            let available = Double(panel.frame.height - OverlayStyle.windowPadV * 2 - statusHeight - bridge)
            let fit = OverlayMotion.waterfallFit(
                availableHeight: available,
                pairHeightsBottomFirst: bottomFirst,
                pairGap: Double(OverlayStyle.pairGap)
            )
            let count = min(fit.pairCount, pool.count)
            visibleKeys = pool.suffix(count).map(\.key)
            clipsTop = fit.clipsTop && count > 0
            holdTask?.cancel()
            holdTask = nil
        } else {
            if review.isReviewing || review.showsBadge {
                review = .live
                reviewIdleTask?.cancel()
                reviewIdleTask = nil
            }
            let shown = history.map { line in
                OverlayMotion.ShownLine(key: line.key, shownAt: appearedAt[line.key] ?? -.infinity)
            }
            visibleKeys = OverlayMotion.visibleLineKeys(shown: shown, now: now, lines: style.lines)
            clipsTop = false
            scheduleHoldRelease(history: history, now: now)
        }
        for key in visibleKeys where appearedAt[key] == nil {
            appearedAt[key] = now
        }

        let pairs = visibleKeys.compactMap { pairViews[$0] }
        let newestKey = history.last?.key
        for pair in pairs where pair.key != newestKey {
            pair.didFadeIn = true
        }
        for pair in pairs where !pair.didFadeIn {
            pair.alphaValue = 0
        }
        let animateMotion = animate && didRenderOnce
        arrange(pairs, animate: animateMotion)
        applyOpacity(pairs: pairs, waterfall: waterfall, animate: animateMotion)
        clipView.fadesTop = clipsTop
        clipView.needsLayout = true
        reviewBadge.isHidden = !(waterfall && review.showsBadge)
        if liveResizeStart == nil {
            stack.layoutSubtreeIfNeeded()
            let content = ceil(stack.fittingSize.height) + OverlayStyle.windowPadV * 2
            fitHeight(waterfall: waterfall, contentHeight: content, animate: animateMotion)
        }
        stack.layoutSubtreeIfNeeded()
        root.layoutSubtreeIfNeeded()
        syncMarkerHits()
        didRenderOnce = true
    }

    /// 仅译文在译文到达前不占行。失败句改显示原文加标记，所以仍然占行。
    private func isDisplayable(_ line: SubtitleLine) -> Bool {
        let presented = OverlayMotion.pairLines(
            source: line.srcText,
            translation: line.dstText,
            failure: failures[line.key] ?? .none,
            mode: style.displayMode
        )
        return presented.first != nil || presented.second != nil
    }

    private func noteArrivals(in lines: [SubtitleLine]) {
        let keys = Set(lines.map(\.key))
        let arrivals = keys.subtracting(renderedKeys).count
        let hadBaseline = didRenderOnce
        renderedKeys = keys
        guard hadBaseline, review.isReviewing, arrivals > 0 else { return }
        for _ in 0..<arrivals {
            review = review.newSentenceArrived()
        }
    }

    private func scheduleHoldRelease(history: [SubtitleLine], now: Double) {
        holdTask?.cancel()
        holdTask = nil
        let preferred = style.lines
        guard history.count > preferred else { return }
        let outside = history[history.count - preferred - 1]
        guard let shown = appearedAt[outside.key] else { return }
        let remain = DesignTokens.Motion.minHoldSec - (now - shown)
        guard remain > 0 else { return }
        let nanos = UInt64((remain + 0.05) * 1_000_000_000)
        holdTask = Task { @MainActor [weak self] in
            try? await Task.sleep(nanoseconds: nanos)
            guard let self, !Task.isCancelled else { return }
            self.render(animate: true)
        }
    }

    private func arrange(_ pairs: [SubtitlePairView], animate: Bool) {
        let desired: [NSView] = pairs + [volatileLabel, tierLabel]
        let current = stack.arrangedSubviews
        if current.count == desired.count, zip(current, desired).allSatisfy({ $0.0 === $0.1 }) {
            return
        }
        let duration = animate
            ? OverlayMotion.motionSeconds(milliseconds: DesignTokens.Motion.shiftUpMs, reduceMotion: reduceMotion)
            : 0
        let updates = {
            for view in self.stack.arrangedSubviews where !desired.contains(where: { $0 === view }) {
                self.stack.removeArrangedSubview(view)
                view.removeFromSuperview()
            }
            for (index, view) in desired.enumerated() {
                if self.stack.arrangedSubviews.indices.contains(index), self.stack.arrangedSubviews[index] === view {
                    continue
                }
                if self.stack.arrangedSubviews.contains(where: { $0 === view }) {
                    self.stack.removeArrangedSubview(view)
                }
                let insertAt = min(index, self.stack.arrangedSubviews.count)
                self.stack.insertArrangedSubview(view, at: insertAt)
            }
            self.stack.layoutSubtreeIfNeeded()
        }
        guard duration > 0 else {
            updates()
            return
        }
        NSAnimationContext.runAnimationGroup { context in
            context.duration = duration
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            context.allowsImplicitAnimation = true
            updates()
        }
    }

    private func applyOpacity(pairs: [SubtitlePairView], waterfall: Bool, animate: Bool) {
        let fade = animate
            ? OverlayMotion.motionSeconds(milliseconds: DesignTokens.Motion.fadeInMs, reduceMotion: reduceMotion)
            : 0
        for (index, pair) in pairs.enumerated() {
            let fromNewest = pairs.count - 1 - index
            let target = OverlayMotion.pairOpacity(
                indexFromNewest: fromNewest,
                visibleCount: pairs.count,
                waterfall: waterfall
            )
            if !pair.didFadeIn {
                pair.didFadeIn = true
                pair.appliedOpacity = target
                if fade <= 0 {
                    pair.alphaValue = CGFloat(target)
                } else {
                    pair.alphaValue = 0
                    NSAnimationContext.runAnimationGroup { context in
                        context.duration = fade
                        context.timingFunction = CAMediaTimingFunction(name: .easeOut)
                        pair.animator().alphaValue = CGFloat(target)
                    }
                }
            } else if abs(pair.appliedOpacity - target) > 0.001 {
                pair.appliedOpacity = target
                pair.alphaValue = CGFloat(target)
            }
        }
    }

    private func update(_ pair: SubtitlePairView, line: SubtitleLine, width: CGFloat) {
        pair.sourceField.preferredMaxLayoutWidth = width
        pair.translationField.preferredMaxLayoutWidth = width
        let presented = OverlayMotion.pairLines(
            source: line.srcText,
            translation: line.dstText,
            failure: failures[line.key] ?? .none,
            mode: style.displayMode
        )
        pair.markerClickable = OverlayMotion.secondLineClickable(presented.secondStyle)
        pair.sourceField.isHidden = presented.first == nil
        pair.translationField.isHidden = presented.second == nil
        if let first = presented.first {
            applySubtitle(first, hex: style.sourceColor, to: pair.sourceField, signature: &pair.sourceSignature)
        }
        guard let second = presented.second else {
            pair.translationField.alphaValue = 1
            translationFading.remove(line.key)
            return
        }
        let scale = OverlayMotion.secondLineFontScale(presented.secondStyle)
        let hex = OverlayMotion.secondLineColorHex(presented.secondStyle) ?? style.translationColor
        let edge: TextEdge? = presented.secondStyle == .translation ? nil : TextEdge.none
        applySubtitle(
            second,
            hex: hex,
            to: pair.translationField,
            signature: &pair.translationSignature,
            fontScale: scale,
            textEdge: edge
        )
        if presented.secondStyle == .translation, fadeTranslationIDs.remove(line.key) != nil {
            beginTranslationFade(id: line.key, field: pair.translationField)
        } else if presented.secondStyle != .translation || !translationFading.contains(line.key) {
            pair.translationField.alphaValue = 1
        }
    }

    private func applySubtitle(
        _ text: String,
        hex: String,
        to field: NSTextField,
        signature: inout String,
        fontScale: Double = 1,
        textEdge: TextEdge? = nil
    ) {
        let edge = textEdge ?? style.textEdge
        let sig = "\(text)\u{1}\(hex)\u{1}\(style.fontSize)\u{1}\(edge.rawValue)\u{1}\(fontScale)"
        guard sig != signature else { return }
        let parts = signature.split(separator: "\u{1}", omittingEmptySubsequences: false).map(String.init)
        let oldText = parts.first ?? ""
        let oldEdge = parts.count >= 4 ? parts[3] : ""
        signature = sig
        let font = NSFont.systemFont(ofSize: CGFloat(style.fontSize * fontScale), weight: .semibold)
        let color = nsColor(hex, fallback: hex)
        if edge == .outline {
            // strokeWidth 是字号的百分比；负值同时填充和描边。-8 在 28pt 上大约 2px。
            let paragraph = NSMutableParagraphStyle()
            paragraph.lineBreakMode = .byWordWrapping
            field.attributedStringValue = NSAttributedString(string: text, attributes: [
                .font: font,
                .foregroundColor: color,
                .strokeColor: NSColor.black,
                .strokeWidth: NSNumber(value: -8.0),
                .paragraphStyle: paragraph,
            ])
            field.shadow = nil
            return
        }
        if oldText != text || oldEdge == TextEdge.outline.rawValue || field.stringValue != text {
            field.stringValue = text
        }
        field.textColor = color
        field.font = font
        field.shadow = edge == .shadow ? textShadow() : nil
    }

    private func textShadow() -> NSShadow {
        let shadow = NSShadow()
        shadow.shadowColor = NSColor.black.withAlphaComponent(0.9)
        shadow.shadowOffset = .zero
        shadow.shadowBlurRadius = 3
        return shadow
    }

    private func measuredFieldHeight(_ field: NSTextField, width: CGFloat) -> CGFloat {
        field.preferredMaxLayoutWidth = width
        guard let cell = field.cell else { return 0 }
        let size = cell.cellSize(forBounds: NSRect(x: 0, y: 0, width: max(width, 1), height: .greatestFiniteMagnitude))
        return ceil(size.height)
    }

    private func measuredPairHeight(_ pair: SubtitlePairView, width: CGFloat) -> CGFloat {
        var total: CGFloat = 0
        var parts = 0
        if !pair.sourceField.isHidden {
            total += measuredFieldHeight(pair.sourceField, width: width)
            parts += 1
        }
        if !pair.translationField.isHidden {
            total += measuredFieldHeight(pair.translationField, width: width)
            parts += 1
        }
        if parts > 1 { total += OverlayStyle.pairGap }
        return ceil(total)
    }

    private func statusBlockHeight(width: CGFloat) -> CGFloat {
        let volatileHeight = measuredFieldHeight(volatileLabel, width: width)
        let tierHeight = measuredFieldHeight(tierLabel, width: width)
        if volatileHeight > 0, tierHeight > 0 {
            return volatileHeight + OverlayStyle.pairGap + tierHeight
        }
        return volatileHeight + tierHeight
    }

    private func blockHeight(pairHeights: [CGFloat], statusHeight: CGFloat) -> CGFloat {
        let gap = OverlayStyle.pairGap
        let pairs = pairHeights.reduce(0, +)
        let pairGaps = pairHeights.count > 1 ? gap * CGFloat(pairHeights.count - 1) : 0
        let bridge = (pairs > 0 && statusHeight > 0) ? gap : 0
        return pairs + pairGaps + bridge + statusHeight + OverlayStyle.windowPadV * 2
    }

    // 底边是窗口 origin.y。高度只往上长，拖动记住的左下角才不会每次重排都往上爬。
    private func fitHeight(waterfall: Bool, contentHeight: CGFloat, animate: Bool) {
        var frame = panel.frame
        if !waterfall {
            frame.size.height = max(contentHeight, OverlayStyle.minimumWindowSize.height)
        }
        frame = clampedFrame(frame)
        guard !framesClose(panel.frame, frame) else { return }
        let duration = animate
            ? OverlayMotion.motionSeconds(milliseconds: DesignTokens.Motion.resizeMs, reduceMotion: reduceMotion)
            : 0
        guard duration > 0 else {
            setPanelFrame(frame, display: true)
            return
        }
        isApplyingProgrammaticFrame = true
        NSAnimationContext.runAnimationGroup({ context in
            context.duration = duration
            context.timingFunction = CAMediaTimingFunction(name: .easeOut)
            self.panel.animator().setFrame(frame, display: true)
        }, completionHandler: {
            Task { @MainActor in
                self.isApplyingProgrammaticFrame = false
            }
        })
    }

    private func framesClose(_ lhs: NSRect, _ rhs: NSRect) -> Bool {
        abs(lhs.size.width - rhs.size.width) <= 1
            && abs(lhs.size.height - rhs.size.height) <= 1
            && abs(lhs.origin.x - rhs.origin.x) < 0.5
            && abs(lhs.origin.y - rhs.origin.y) < 0.5
    }

    // NSTextField 在 StackView 里不给最大宽度就不会折行
    private var textWidth: CGFloat {
        max(40, panel.frame.width - OverlayStyle.windowPadH * 2)
    }

    private func updateWrapWidths() {
        let width = textWidth
        volatileLabel.preferredMaxLayoutWidth = width
        tierLabel.preferredMaxLayoutWidth = width
        for pair in pairViews.values {
            pair.sourceField.preferredMaxLayoutWidth = width
            pair.translationField.preferredMaxLayoutWidth = width
        }
    }

    private var reduceMotion: Bool {
        NSWorkspace.shared.accessibilityDisplayShouldReduceMotion
    }

    private func badgeBackgroundColor() -> NSColor {
        let rgb = OverlayStyle.parseHex(DesignTokens.Color.background) ?? (r: 0, g: 0, b: 0)
        return NSColor(srgbRed: rgb.r, green: rgb.g, blue: rgb.b, alpha: 0.82)
    }

    private func handleScroll(_ event: NSEvent) {
        let raw: Int
        if event.hasPreciseScrollingDeltas {
            scrollAccumulator += event.scrollingDeltaY
            let step: CGFloat = 48
            var steps = 0
            while scrollAccumulator >= step {
                scrollAccumulator -= step
                steps += 1
            }
            while scrollAccumulator <= -step {
                scrollAccumulator += step
                steps -= 1
            }
            raw = steps
        } else {
            scrollAccumulator = 0
            if event.scrollingDeltaY > 0 {
                raw = 1
            } else if event.scrollingDeltaY < 0 {
                raw = -1
            } else {
                raw = 0
            }
        }
        let delta = OverlayMotion.reviewScrollDelta(
            rawSteps: raw,
            clickThrough: panel.ignoresMouseEvents,
            waterfall: waterfallActive
        )
        guard delta != 0 else { return }
        let now = Date().timeIntervalSinceReferenceDate
        let maxOffset = max(0, store.lines.suffix(LineStore.displayHistoryLimit).count - 1)
        review = review.scrolled(by: delta, now: now, maxOffset: maxOffset)
        scheduleReviewIdle()
        render(animate: false)
    }

    private func scheduleReviewIdle() {
        reviewIdleTask?.cancel()
        reviewIdleTask = nil
        guard review.isReviewing else { return }
        let deadline = review.lastInputAt + CaptionReview.idleSec
        reviewIdleTask = Task { @MainActor [weak self] in
            let now = Date().timeIntervalSinceReferenceDate
            let wait = max(0, deadline - now)
            try? await Task.sleep(nanoseconds: UInt64(wait * 1_000_000_000))
            guard let self, !Task.isCancelled else { return }
            let tickNow = Date().timeIntervalSinceReferenceDate
            let next = self.review.tick(now: tickNow)
            if next == self.review {
                self.scheduleReviewIdle()
                return
            }
            self.review = next
            self.render(animate: true)
        }
    }

    private func applyChrome() {
        let radius = style.cornerRadius
        root.layer?.cornerRadius = radius
        root.layer?.masksToBounds = true
        tint.layer?.cornerRadius = radius
        tint.layer?.masksToBounds = true
        let rgb = OverlayStyle.parseHex(style.backgroundColor) ?? OverlayStyle.parseHex(OverlayStyle.defaultBackgroundColor)!
        tint.layer?.backgroundColor = NSColor(
            srgbRed: rgb.r,
            green: rgb.g,
            blue: rgb.b,
            alpha: CGFloat(style.backgroundOpacity)
        ).cgColor
    }

    private func applyTypography() {
        volatileLabel.font = .systemFont(ofSize: CGFloat(style.fontSize - 4))
        tierLabel.font = .systemFont(ofSize: OverlayStyle.statusFontSize)
    }

    private func applyGeometry(stickStripToBottom: Bool) {
        let screen = screenForPanel()
        var frame = panel.frame
        if style.shape == .strip {
            frame.size.width = OverlayStyle.windowWidth(for: style.shape, visibleWidth: screen.width)
            style.width = nil
        } else if let savedWidth = style.width {
            frame.size.width = CGFloat(savedWidth)
        } else {
            frame.size.width = OverlayStyle.windowWidth(for: style.shape, visibleWidth: screen.width)
        }
        if style.shape == .strip && stickStripToBottom {
            frame.origin.x = screen.minX + 20
            frame.origin.y = screen.minY
        }
        frame = clampedFrame(frame, fallback: screen)
        updateSizeLimits(for: frame)
        setPanelFrame(frame, display: true)
        updateWrapWidths()
    }

    private func clampedFrame(_ frame: NSRect, fallback: CGRect? = nil) -> NSRect {
        let screen = fallback ?? screenForPanel()
        let screens = NSScreen.screens.map(\.visibleFrame)
        return OverlayStyle.clampedFrame(
            x: frame.origin.x,
            y: frame.origin.y,
            windowWidth: frame.size.width,
            windowHeight: frame.size.height,
            visibleFrames: screens.isEmpty ? [screen] : screens
        )
    }

    private func updateSizeLimits(for frame: NSRect) {
        let screen = OverlayStyle.preferredVisibleFrame(
            anchor: CGPoint(x: frame.midX, y: frame.midY),
            frames: NSScreen.screens.map(\.visibleFrame),
            fallback: NSScreen.main?.visibleFrame ?? CGRect(x: 0, y: 0, width: 1280, height: 800)
        )
        let minSize = OverlayStyle.minimumWindowSize
        let maxHeight = max(minSize.height, screen.height)
        let newMin: NSSize
        let newMax: NSSize
        if style.shape == .strip {
            let locked = lockedStripWidth(on: screen)
            newMin = NSSize(width: locked, height: minSize.height)
            newMax = NSSize(width: locked, height: maxHeight)
        } else {
            newMin = minSize
            newMax = NSSize(width: max(minSize.width, screen.width), height: maxHeight)
        }
        guard panel.minSize != newMin || panel.maxSize != newMax else { return }
        let wasProgrammatic = isApplyingProgrammaticFrame
        isApplyingProgrammaticFrame = true
        panel.minSize = newMin
        panel.maxSize = newMax
        isApplyingProgrammaticFrame = wasProgrammatic
    }

    /// 贴底横条的目标宽：可见宽 − 40，但不小于最小宽，也不宽过这块屏幕。
    private func lockedStripWidth(on screen: CGRect) -> CGFloat {
        let natural = OverlayStyle.windowWidth(for: .strip, visibleWidth: screen.width)
        let floor = min(OverlayStyle.minimumWindowSize.width, max(screen.width, 1))
        return min(max(natural, floor), max(screen.width, 1))
    }

    /// 贴底横条宽度锁死，不写进 style.width。和当前帧比较夹回后的结果，避免来回 setFrame。
    private func syncStripWidthIfNeeded() {
        guard style.shape == .strip else { return }
        let screen = screenForPanel()
        var frame = panel.frame
        frame.size.width = lockedStripWidth(on: screen)
        frame = clampedFrame(frame, fallback: screen)
        if abs(panel.frame.width - frame.width) <= 1,
           abs(panel.frame.origin.x - frame.origin.x) < 0.5,
           abs(panel.frame.origin.y - frame.origin.y) < 0.5 {
            return
        }
        setPanelFrame(frame, display: true)
    }

    private func setPanelFrame(_ frame: NSRect, display: Bool) {
        isApplyingProgrammaticFrame = true
        panel.setFrame(frame, display: display)
        isApplyingProgrammaticFrame = false
    }

    private func screenForPanel() -> CGRect {
        let frames = NSScreen.screens.map(\.visibleFrame)
        let fallback = NSScreen.main?.visibleFrame ?? CGRect(x: 0, y: 0, width: 1280, height: 800)
        return OverlayStyle.preferredVisibleFrame(
            anchor: CGPoint(x: panel.frame.midX, y: panel.frame.midY),
            frames: frames,
            fallback: fallback
        )
    }

    private func nsColor(_ hex: String, fallback: String) -> NSColor {
        let rgb = OverlayStyle.parseHex(hex) ?? OverlayStyle.parseHex(fallback) ?? (r: 1, g: 1, b: 1)
        return NSColor(srgbRed: rgb.r, green: rgb.g, blue: rgb.b, alpha: 1)
    }

    private func persistAndApply(stickStripToBottom: Bool) {
        applyGeometry(stickStripToBottom: stickStripToBottom)
        applyChrome()
        applyTypography()
        render()
        originSaveTask?.cancel()
        style.originX = Double(panel.frame.origin.x)
        style.originY = Double(panel.frame.origin.y)
        try? OverlayStyleStore.save(style)
    }

    private func scheduleStyleSave() {
        originSaveTask?.cancel()
        originSaveTask = Task { @MainActor [weak self] in
            do {
                try await Task.sleep(nanoseconds: 200_000_000)
            } catch {
                return
            }
            guard let self else { return }
            try? OverlayStyleStore.save(self.style)
        }
    }

    private func rememberOrigin() {
        let origin = panel.frame.origin
        let x = Double(origin.x)
        let y = Double(origin.y)
        if style.originX == x, style.originY == y { return }
        style.originX = x
        style.originY = y
        scheduleStyleSave()
    }

    /// 只记录相对手势起点真正变过的边。横条不写宽度。高度记用户拉到的值，显示时再和内容取较大者。
    private func rememberSize(changedFrom start: NSSize) {
        var changed = false
        if style.shape == .strip {
            if style.width != nil {
                style.width = nil
                changed = true
            }
        } else if abs(panel.frame.width - start.width) > 1 {
            style.width = Double(panel.frame.width)
            changed = true
        }
        if abs(panel.frame.height - start.height) > 1 {
            style.height = Double(panel.frame.height)
            changed = true
        }
        if changed {
            scheduleStyleSave()
        }
    }

    private func handleArrowKey(_ event: NSEvent) -> Bool {
        let blocked: NSEvent.ModifierFlags = [.command, .option, .control]
        if !event.modifierFlags.intersection(blocked).isEmpty { return false }
        let delta: CGFloat = event.modifierFlags.contains(.shift) ? 50 : 10
        var frame = panel.frame
        switch event.keyCode {
        case 123:
            frame.origin.x -= delta
        case 124:
            frame.origin.x += delta
        case 125:
            frame.origin.y -= delta
        case 126:
            frame.origin.y += delta
        default:
            return false
        }
        frame = clampedFrame(frame)
        setPanelFrame(frame, display: true)
        syncStripWidthIfNeeded()
        rememberOrigin()
        return true
    }

    @objc private func chooseFontSize(_ sender: NSMenuItem) {
        guard OverlayStyle.fontSizePresets.contains(Double(sender.tag)) else { return }
        style.fontSize = Double(sender.tag)
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func increaseFontSize() {
        let next = OverlayStyle.stepFontSize(style.fontSize, by: 1)
        guard abs(next - style.fontSize) > 0.01 else { return }
        style.fontSize = next
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func decreaseFontSize() {
        let next = OverlayStyle.stepFontSize(style.fontSize, by: -1)
        guard abs(next - style.fontSize) > 0.01 else { return }
        style.fontSize = next
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseSourceColor(_ sender: NSMenuItem) {
        guard let hex = sender.representedObject as? String, let canonical = OverlayStyle.canonicalHex(hex) else { return }
        style.sourceColor = canonical
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseTranslationColor(_ sender: NSMenuItem) {
        guard let hex = sender.representedObject as? String, let canonical = OverlayStyle.canonicalHex(hex) else { return }
        style.translationColor = canonical
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseBackgroundColor(_ sender: NSMenuItem) {
        guard let hex = sender.representedObject as? String, let canonical = OverlayStyle.canonicalHex(hex) else { return }
        style.backgroundColor = canonical
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseOpacity(_ sender: NSMenuItem) {
        guard let number = sender.representedObject as? NSNumber else { return }
        style.backgroundOpacity = number.doubleValue
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseTextEdge(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let edge = TextEdge(rawValue: raw) else { return }
        style.textEdge = edge
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseShape(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let shape = OverlayShape(rawValue: raw) else { return }
        let stick = shape == .strip && style.shape != .strip
        style.shape = shape
        persistAndApply(stickStripToBottom: stick)
    }

    @objc private func chooseLines(_ sender: NSMenuItem) {
        guard (1...4).contains(sender.tag) else { return }
        style.lines = sender.tag
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func chooseDisplayMode(_ sender: NSMenuItem) {
        guard let raw = sender.representedObject as? String, let mode = DisplayMode(rawValue: raw) else { return }
        style.displayMode = mode
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func resetStyle() {
        let x = style.originX
        let y = style.originY
        style = .default
        style.originX = x
        style.originY = y
        persistAndApply(stickStripToBottom: false)
    }

    @objc private func openSettings() {
        SettingsWindow.open { [weak self] style in
            self?.applyExternalStyle(style)
        }
    }

    /// 设置窗口改样式走这里。位置和用户拖出的尺寸以悬浮窗自己为准，不用设置页从磁盘读到的那份，
    /// 否则会和拖动/缩放的防抖落盘互相覆盖。
    private func applyExternalStyle(_ incoming: OverlayStyle) {
        var next = incoming
        next.originX = style.originX
        next.originY = style.originY
        next.width = style.width
        next.height = style.height
        let stick = next.shape == .strip && style.shape != .strip
        style = next
        persistAndApply(stickStripToBottom: stick)
    }

    @objc private func togglePause() {
        isPaused.toggle()
        pauseItem.title = isPaused ? "继续" : "暂停"
        onPauseChanged(isPaused)
    }

    @objc private func toggleClickThrough() {
        panel.ignoresMouseEvents.toggle()
        clickThroughItem.state = panel.ignoresMouseEvents ? .on : .off
    }

    @objc private func jumpToLatest() {
        review = review.jumpToBottom()
        reviewIdleTask?.cancel()
        reviewIdleTask = nil
        render(animate: true)
    }

    @objc private func quit() {
        let pending = originSaveTask != nil
        originSaveTask?.cancel()
        originSaveTask = nil
        if pending {
            try? OverlayStyleStore.save(style)
        }
        onQuit()
    }
}

extension OverlayController: NSWindowDelegate {
    func windowDidMove(_ notification: Notification) {
        guard (notification.object as? NSWindow) === panel else { return }
        updateSizeLimits(for: panel.frame)
        if !isApplyingProgrammaticFrame {
            syncStripWidthIfNeeded()
        }
        rememberOrigin()
    }

    func windowWillStartLiveResize(_ notification: Notification) {
        guard (notification.object as? NSWindow) === panel else { return }
        liveResizeStart = panel.frame.size
    }

    func windowDidResize(_ notification: Notification) {
        guard (notification.object as? NSWindow) === panel else { return }
        updateSizeLimits(for: panel.frame)
        if liveResizeStart != nil {
            render(animate: false)
        } else {
            updateWrapWidths()
        }
    }

    func windowDidEndLiveResize(_ notification: Notification) {
        guard (notification.object as? NSWindow) === panel else { return }
        let start = liveResizeStart
        liveResizeStart = nil
        if let start {
            rememberSize(changedFrom: start)
        }
        syncStripWidthIfNeeded()
        render(animate: true)
    }
}

extension OverlayController: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        rebuildStyleMenu()
    }
}
