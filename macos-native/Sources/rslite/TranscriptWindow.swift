import AppKit

/// 当天字幕的轻量历史窗口。归档本身不依赖 AppKit，窗口只负责查询和展示。
@MainActor
final class TranscriptWindow: NSObject, NSWindowDelegate, NSSearchFieldDelegate {
    private let archive: TranscriptArchive
    private let window: NSWindow
    private let searchField = NSSearchField(frame: .zero)
    private let copyButton = NSButton(title: "复制全部", target: nil, action: nil)
    private let revealButton = NSButton(title: "在访达中显示", target: nil, action: nil)
    // scrollableTextView 自带正确的尺寸/自动伸缩设置；手工 NSTextView(frame: .zero) 放进
    // NSScrollView 时宽度会停在 0，文字排不出来。
    private let scrollView = NSTextView.scrollableTextView()
    private var textView: NSTextView { self.scrollView.documentView as! NSTextView }
    private var allLines: [TranscriptArchive.Line] = []
    private var filteredLines: [TranscriptArchive.Line] = []

    init(archive: TranscriptArchive) {
        self.archive = archive
        self.window = NSWindow(
            contentRect: NSRect(x: 0, y: 0, width: 560, height: 640),
            styleMask: [.titled, .closable, .resizable, .miniaturizable],
            backing: .buffered,
            defer: false
        )
        super.init()
        self.configureWindow()
    }

    func show() {
        self.window.makeKeyAndOrderFront(nil)
        self.reload()
        NSApp.activate(ignoringOtherApps: true)
    }

    /// 存档每变一句都会调到这里；窗口没开时不刷新，打开时 show() 会再读一次。
    func reload() {
        guard self.window.isVisible else { return }
        let shouldScrollToBottom = self.isAtBottom()
        self.allLines = self.archive.lines(for: Date())
        self.refreshText(scrollToBottom: shouldScrollToBottom)
    }

    func windowShouldClose(_ sender: NSWindow) -> Bool {
        self.window.orderOut(nil)
        return false
    }

    func controlTextDidChange(_ notification: Notification) {
        self.refreshText(scrollToBottom: false)
    }

    private func configureWindow() {
        self.window.title = "今天的字幕"
        self.window.isReleasedWhenClosed = false
        self.window.delegate = self
        self.window.minSize = NSSize(width: 420, height: 360)

        let content = NSView(frame: .zero)
        content.translatesAutoresizingMaskIntoConstraints = false
        self.window.contentView = content

        self.searchField.placeholderString = "搜索"
        self.searchField.delegate = self
        self.searchField.translatesAutoresizingMaskIntoConstraints = false

        self.copyButton.target = self
        self.copyButton.action = #selector(self.copyAll)
        self.revealButton.target = self
        self.revealButton.action = #selector(self.revealInFinder)
        let controls = NSStackView(views: [self.searchField, self.copyButton, self.revealButton])
        controls.orientation = .horizontal
        controls.alignment = .centerY
        controls.spacing = 8
        controls.translatesAutoresizingMaskIntoConstraints = false
        self.searchField.setContentHuggingPriority(.defaultLow, for: .horizontal)
        self.searchField.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)

        self.textView.isEditable = false
        self.textView.isSelectable = true
        self.textView.isRichText = false
        self.textView.usesFindPanel = false
        self.textView.font = .monospacedSystemFont(ofSize: 13, weight: .regular)
        self.textView.textContainerInset = NSSize(width: 12, height: 12)

        self.scrollView.borderType = .bezelBorder
        self.scrollView.autohidesScrollers = true
        self.scrollView.translatesAutoresizingMaskIntoConstraints = false

        content.addSubview(controls)
        content.addSubview(self.scrollView)
        NSLayoutConstraint.activate([
            controls.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            controls.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            controls.topAnchor.constraint(equalTo: content.topAnchor, constant: 12),
            self.scrollView.leadingAnchor.constraint(equalTo: content.leadingAnchor, constant: 12),
            self.scrollView.trailingAnchor.constraint(equalTo: content.trailingAnchor, constant: -12),
            self.scrollView.topAnchor.constraint(equalTo: controls.bottomAnchor, constant: 10),
            self.scrollView.bottomAnchor.constraint(equalTo: content.bottomAnchor, constant: -12),
        ])
    }

    private func refreshText(scrollToBottom: Bool) {
        self.filteredLines = TranscriptArchive.filter(self.allLines, query: self.searchField.stringValue)
        let text = TranscriptArchive.plainText(self.filteredLines)
        self.textView.string = text.isEmpty ? "今天还没有字幕" : text
        if scrollToBottom {
            self.textView.scrollToEndOfDocument(nil)
        }
    }

    private func isAtBottom() -> Bool {
        guard let documentView = self.scrollView.documentView else { return true }
        let visible = self.scrollView.contentView.documentVisibleRect
        return visible.maxY >= documentView.bounds.maxY - 1
    }

    @objc private func copyAll() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(TranscriptArchive.plainText(self.filteredLines), forType: .string)
    }

    @objc private func revealInFinder() {
        let fileURL = self.archive.fileURL(for: Date())
        let directory = fileURL.deletingLastPathComponent()
        let fileManager = FileManager.default
        if !fileManager.fileExists(atPath: directory.path) {
            try? fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
        }
        let target = fileManager.fileExists(atPath: fileURL.path) ? fileURL : directory
        NSWorkspace.shared.activateFileViewerSelecting([target])
    }
}
