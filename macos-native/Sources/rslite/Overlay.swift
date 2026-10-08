import AppKit
import Foundation

@MainActor
final class OverlayController: NSObject {
    private let panel: NSPanel
    private let stack = NSStackView()
    private let volatileLabel = NSTextField(labelWithString: "")
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let pauseItem = NSMenuItem(title: "暂停", action: #selector(togglePause), keyEquivalent: "")
    private let clickThroughItem = NSMenuItem(title: "鼠标穿透", action: #selector(toggleClickThrough), keyEquivalent: "")

    // 字幕历史（含已滚出屏幕的行）；界面只画末尾两行。节点精修结果按 P5 在这里替换本机行。
    private var store = LineStore()
    private var isPaused = false
    private let onPauseChanged: (Bool) -> Void
    private let onQuit: () -> Void

    init(onPauseChanged: @escaping (Bool) -> Void, onQuit: @escaping () -> Void) {
        self.onPauseChanged = onPauseChanged
        self.onQuit = onQuit

        let screenFrame = NSScreen.main?.visibleFrame ?? NSRect(x: 0, y: 0, width: 1280, height: 800)
        let size = NSSize(width: min(900, screenFrame.width - 80), height: 190)
        let origin = NSPoint(
            x: screenFrame.midX - size.width / 2,
            y: screenFrame.minY + 70
        )
        panel = NSPanel(
            contentRect: NSRect(origin: origin, size: size),
            styleMask: [.borderless, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )

        super.init()
        configurePanel()
        configureStatusItem()
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
        store.setTranslation(key: id, text: text)
        render()
    }

    /// 节点的精修句到达（P5）：整体替换被它覆盖的本机行，找不到就按时间插入。
    func addNodeFinal(nodeID: String, t0: Double?, t1: Double?, src: String, dst: String?) {
        store.applyNode(nodeID: nodeID, t0: t0, t1: t1, srcText: src, dstText: dst)
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
        statusItem.button?.title = "字·\(label)"
    }

    private func configurePanel() {
        panel.level = .statusBar
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.isMovableByWindowBackground = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true

        let root = NSVisualEffectView(frame: panel.contentView?.bounds ?? .zero)
        root.autoresizingMask = [.width, .height]
        root.material = .hudWindow
        root.blendingMode = .behindWindow
        root.state = .active
        root.wantsLayer = true
        root.layer?.cornerRadius = 18
        root.layer?.backgroundColor = NSColor.black.withAlphaComponent(0.62).cgColor

        stack.orientation = .vertical
        stack.alignment = .leading
        stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false

        volatileLabel.textColor = .secondaryLabelColor
        volatileLabel.font = .systemFont(ofSize: 20)
        volatileLabel.lineBreakMode = .byWordWrapping
        volatileLabel.maximumNumberOfLines = 2
        volatileLabel.preferredMaxLayoutWidth = textWidth

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
        statusItem.button?.title = "字"

        let menu = NSMenu()
        pauseItem.target = self
        menu.addItem(pauseItem)

        clickThroughItem.target = self
        clickThroughItem.state = .off
        menu.addItem(clickThroughItem)

        menu.addItem(.separator())
        let quit = NSMenuItem(title: "退出", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        statusItem.menu = menu
    }

    private func render() {
        stack.arrangedSubviews.forEach { view in
            stack.removeArrangedSubview(view)
            view.removeFromSuperview()
        }

        for line in store.lines.suffix(2) {
            stack.addArrangedSubview(label(line.srcText, color: .white, size: 22))
            if let translation = line.dstText, !translation.isEmpty {
                stack.addArrangedSubview(label(translation, color: NSColor(calibratedRed: 1.0, green: 0.91, blue: 0.58, alpha: 1), size: 22))
            }
        }
        stack.addArrangedSubview(volatileLabel)
        fitHeight()
    }

    // 文字换行后高度会变：窗口跟着内容长高/变矮，底边不动（字幕贴着屏幕下方）
    private func fitHeight() {
        stack.layoutSubtreeIfNeeded()
        let height = ceil(stack.fittingSize.height) + 36
        var frame = panel.frame
        guard abs(frame.height - height) > 1 else { return }
        frame.origin.y += frame.height - height
        frame.size.height = height
        panel.setFrame(frame, display: true)
    }

    // NSTextField 在 StackView 里不给最大宽度就不会折行
    private var textWidth: CGFloat {
        panel.frame.width - 44
    }

    private func label(_ text: String, color: NSColor, size: CGFloat) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.textColor = color
        field.font = .systemFont(ofSize: size, weight: .semibold)
        field.lineBreakMode = .byWordWrapping
        field.maximumNumberOfLines = 3
        field.preferredMaxLayoutWidth = textWidth
        return field
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
        onQuit()
    }
}
