import AppKit
import Foundation

@MainActor
final class OverlayController: NSObject {
    private let panel: NSPanel
    private let root: NSVisualEffectView
    private let tint: NSView
    private let stack = NSStackView()
    private let volatileLabel = NSTextField(labelWithString: "")
    /// 当前识别层。和草稿分开，避免 setStatus/setVolatile 把「云端·Groq」盖掉。
    private let tierLabel = NSTextField(labelWithString: "本机")
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private let statusMenu = NSMenu()
    private let styleMenu = NSMenu(title: "字幕样式")
    private let styleMenuItem = NSMenuItem(title: "字幕样式", action: nil, keyEquivalent: "")
    private let pauseItem = NSMenuItem(title: "暂停", action: #selector(togglePause), keyEquivalent: "")
    private let clickThroughItem = NSMenuItem(title: "鼠标穿透", action: #selector(toggleClickThrough), keyEquivalent: "")

    // 字幕历史（含已滚出屏幕的行）；界面只画末尾 style.lines 对。节点精修结果按 P5 在这里替换本机行。
    private var store = LineStore()
    /// 译文可能先于 MainActor 上的 applyNode 回来。先按 key 记下，行出现再补上。
    private var pendingTranslation: [Int: String] = [:]
    private var style: OverlayStyle
    private var isPaused = false
    private var originSaveTask: Task<Void, Never>?
    private let onPauseChanged: (Bool) -> Void
    private let onQuit: () -> Void

    init(onPauseChanged: @escaping (Bool) -> Void, onQuit: @escaping () -> Void) {
        self.onPauseChanged = onPauseChanged
        self.onQuit = onQuit
        let loaded = OverlayStyleStore.load()
        self.style = loaded

        let frame = Self.initialFrame(for: loaded)
        panel = NSPanel(
            contentRect: frame,
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        root = NSVisualEffectView(frame: NSRect(origin: .zero, size: frame.size))
        tint = NSView(frame: NSRect(origin: .zero, size: frame.size))

        super.init()
        configurePanel()
        configureStatusItem()
        panel.delegate = self
        applyChrome()
        applyTypography()
        render()
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
        volatileLabel.stringValue = shown
        fitHeight()
    }

    func addFinal(id: Int, text: String, t0: Double? = nil, t1: Double? = nil) {
        store.addLocal(key: id, t0: t0, t1: t1, text: text)
        render()
    }

    func addTranslation(id: Int, text: String) {
        if store.lines.contains(where: { $0.key == id }) {
            store.setTranslation(key: id, text: text)
            pendingTranslation.removeValue(forKey: id)
        } else {
            pendingTranslation[id] = text
        }
        render()
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
        volatileLabel.stringValue = text
        fitHeight()
    }

    /// 菜单栏标题显示当前识别在哪里：音频是否正在离开本机，用户要能一眼看出来。
    func setMode(_ mode: SubtitleMode) {
        setModeLabel(mode.label)
    }

    func setModeLabel(_ label: String) {
        statusItem.length = NSStatusItem.variableLength
        statusItem.button?.title = "字·\(label)"
        tierLabel.stringValue = label
        fitHeight()
    }

    private static func initialFrame(for style: OverlayStyle) -> NSRect {
        let frames = NSScreen.screens.map(\.visibleFrame)
        let fallback = CGRect(x: 0, y: 0, width: 1280, height: 800)
        let main = NSScreen.main?.visibleFrame ?? fallback
        let anchor = CGPoint(x: style.originX ?? main.midX, y: style.originY ?? main.midY)
        let screen = OverlayStyle.preferredVisibleFrame(anchor: anchor, frames: frames, fallback: main)
        let width = OverlayStyle.windowWidth(for: style.shape, visibleWidth: screen.width)
        let height: CGFloat = 220
        let origin: CGPoint
        if let x = style.originX, let y = style.originY {
            origin = OverlayStyle.clampedOrigin(
                x: x,
                y: y,
                windowWidth: width,
                windowHeight: height,
                visibleFrames: frames.isEmpty ? [screen] : frames
            )
        } else if style.shape == .strip {
            origin = CGPoint(x: screen.minX + 20, y: screen.minY)
        } else {
            origin = CGPoint(x: screen.midX - width / 2, y: screen.minY + 70)
        }
        return NSRect(origin: origin, size: NSSize(width: width, height: height))
    }

    private func configurePanel() {
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true

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
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false

        volatileLabel.textColor = .secondaryLabelColor
        volatileLabel.lineBreakMode = .byWordWrapping
        volatileLabel.maximumNumberOfLines = 2
        volatileLabel.preferredMaxLayoutWidth = textWidth

        tierLabel.textColor = .secondaryLabelColor
        tierLabel.font = .systemFont(ofSize: 14)
        tierLabel.lineBreakMode = .byTruncatingTail
        tierLabel.maximumNumberOfLines = 1
        tierLabel.preferredMaxLayoutWidth = textWidth

        root.addSubview(stack)
        panel.contentView = root

        NSLayoutConstraint.activate([
            stack.leadingAnchor.constraint(equalTo: root.leadingAnchor, constant: 22),
            stack.trailingAnchor.constraint(equalTo: root.trailingAnchor, constant: -22),
            stack.topAnchor.constraint(equalTo: root.topAnchor, constant: 18),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: root.bottomAnchor, constant: -18),
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
        styleMenu.addItem(submenu("行数", (1...4).map { count in
            checked("\(count)", on: style.lines == count, action: #selector(chooseLines(_:)), tag: count)
        }))
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

    private func checked(_ title: String, on: Bool, action: Selector, object: Any? = nil, tag: Int = 0) -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: "")
        item.target = self
        item.state = on ? .on : .off
        item.representedObject = object
        item.tag = tag
        return item
    }

    private func render() {
        stack.arrangedSubviews.forEach { view in
            stack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }

        let source = nsColor(style.sourceColor, fallback: OverlayStyle.defaultSourceColor)
        let translation = nsColor(style.translationColor, fallback: OverlayStyle.defaultTranslationColor)
        for line in store.lines.suffix(style.lines) {
            if style.displayMode != .translationOnly {
                stack.addArrangedSubview(subtitleField(line.srcText, color: source))
            }
            if style.displayMode != .sourceOnly,
               let translated = line.dstText,
               !translated.isEmpty {
                stack.addArrangedSubview(subtitleField(translated, color: translation))
            }
        }
        updateWrapWidths()
        stack.addArrangedSubview(volatileLabel)
        stack.addArrangedSubview(tierLabel)
        fitHeight()
    }

    // 底边是窗口 origin.y。高度只往上长，拖动记住的左下角才不会每次重排都往上爬。
    private func fitHeight() {
        updateWrapWidths()
        stack.layoutSubtreeIfNeeded()
        let height = ceil(stack.fittingSize.height) + 36
        var frame = panel.frame
        frame.size.height = max(height, 1)
        let screens = NSScreen.screens.map(\.visibleFrame)
        if !screens.isEmpty {
            let clamped = OverlayStyle.clampedOrigin(
                x: frame.origin.x,
                y: frame.origin.y,
                windowWidth: frame.size.width,
                windowHeight: frame.size.height,
                visibleFrames: screens
            )
            frame.origin = clamped
        }
        if abs(panel.frame.size.height - frame.size.height) <= 1,
           abs(panel.frame.origin.x - frame.origin.x) < 0.5,
           abs(panel.frame.origin.y - frame.origin.y) < 0.5 {
            return
        }
        panel.setFrame(frame, display: true)
    }

    // NSTextField 在 StackView 里不给最大宽度就不会折行
    private var textWidth: CGFloat {
        max(40, panel.frame.width - 44)
    }

    private func updateWrapWidths() {
        let width = textWidth
        volatileLabel.preferredMaxLayoutWidth = width
        tierLabel.preferredMaxLayoutWidth = width
    }

    private func subtitleField(_ text: String, color: NSColor) -> NSTextField {
        let field = NSTextField(labelWithString: "")
        let font = NSFont.systemFont(ofSize: CGFloat(style.fontSize), weight: .semibold)
        field.lineBreakMode = .byWordWrapping
        field.maximumNumberOfLines = 3
        field.preferredMaxLayoutWidth = textWidth
        if let cell = field.cell as? NSTextFieldCell {
            cell.wraps = true
            cell.isScrollable = false
        }
        switch style.textEdge {
        case .outline:
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
        case .shadow:
            field.stringValue = text
            field.textColor = color
            field.font = font
            let shadow = NSShadow()
            shadow.shadowColor = NSColor.black.withAlphaComponent(0.9)
            shadow.shadowOffset = .zero
            shadow.shadowBlurRadius = 3
            field.shadow = shadow
        case .none:
            field.stringValue = text
            field.textColor = color
            field.font = font
            field.shadow = nil
        }
        return field
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
    }

    private func applyGeometry(stickStripToBottom: Bool) {
        let screen = screenForPanel()
        var frame = panel.frame
        frame.size.width = OverlayStyle.windowWidth(for: style.shape, visibleWidth: screen.width)
        if style.shape == .strip && stickStripToBottom {
            frame.origin.x = screen.minX + 20
            frame.origin.y = screen.minY
        }
        let screens = NSScreen.screens.map(\.visibleFrame)
        let clamped = OverlayStyle.clampedOrigin(
            x: frame.origin.x,
            y: frame.origin.y,
            windowWidth: frame.size.width,
            windowHeight: frame.size.height,
            visibleFrames: screens.isEmpty ? [screen] : screens
        )
        frame.origin = clamped
        panel.setFrame(frame, display: true)
        updateWrapWidths()
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

    private func rememberOrigin() {
        let origin = panel.frame.origin
        let x = Double(origin.x)
        let y = Double(origin.y)
        if style.originX == x, style.originY == y { return }
        style.originX = x
        style.originY = y
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

    @objc private func togglePause() {
        isPaused.toggle()
        pauseItem.title = isPaused ? "继续" : "暂停"
        onPauseChanged(isPaused)
    }

    @objc private func toggleClickThrough() {
        panel.ignoresMouseEvents.toggle()
        clickThroughItem.state = panel.ignoresMouseEvents ? .on : .off
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
        rememberOrigin()
    }
}

extension OverlayController: NSMenuDelegate {
    func menuWillOpen(_ menu: NSMenu) {
        rebuildStyleMenu()
    }
}
