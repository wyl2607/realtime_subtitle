import AppKit
import Foundation

@MainActor
final class OverlayController: NSObject {
    private struct Line {
        var original: String
        var translation: String?
    }

    private let panel: NSPanel
    private let stack = NSStackView()
    private let volatileLabel = NSTextField(labelWithString: "")
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
    private let pauseItem = NSMenuItem(title: "暂停", action: #selector(togglePause), keyEquivalent: "")
    private let clickThroughItem = NSMenuItem(title: "鼠标穿透", action: #selector(toggleClickThrough), keyEquivalent: "")

    private var lines: [Int: Line] = [:]
    private var order: [Int] = []
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
        volatileLabel.stringValue = text
    }

    func addFinal(id: Int, text: String) {
        lines[id] = Line(original: text, translation: nil)
        order.append(id)
        trim()
        render()
    }

    func addTranslation(id: Int, text: String) {
        guard var line = lines[id] else {
            return
        }
        line.translation = text
        lines[id] = line
        render()
    }

    func setStatus(_ text: String) {
        volatileLabel.stringValue = text
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
        volatileLabel.lineBreakMode = .byTruncatingTail
        volatileLabel.maximumNumberOfLines = 1

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

        for id in order.suffix(2) {
            guard let line = lines[id] else {
                continue
            }
            stack.addArrangedSubview(label(line.original, color: .white, size: 22))
            if let translation = line.translation, !translation.isEmpty {
                stack.addArrangedSubview(label(translation, color: NSColor(calibratedRed: 1.0, green: 0.91, blue: 0.58, alpha: 1), size: 22))
            }
        }
        stack.addArrangedSubview(volatileLabel)
    }

    private func label(_ text: String, color: NSColor, size: CGFloat) -> NSTextField {
        let field = NSTextField(labelWithString: text)
        field.textColor = color
        field.font = .systemFont(ofSize: size, weight: .semibold)
        field.lineBreakMode = .byWordWrapping
        field.maximumNumberOfLines = 2
        return field
    }

    private func trim() {
        while order.count > 2 {
            let id = order.removeFirst()
            lines.removeValue(forKey: id)
        }
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
