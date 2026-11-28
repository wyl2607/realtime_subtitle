import SwiftUI
import UIKit

/// 「回看」页面：默认读当天存档，有往日存档时可切换日期并搜索、导出或重译。
///
/// 为什么需要这一页：扩展只把最近 3 句放进 Keychain（PiP 画面就那么多），
/// 但存档文件里存的是按天记录的每一句。页面直接读存档文件，主 App 一打开就能回看。
struct TranscriptView: View {
    private struct RetryStatusKey: Hashable {
        let day: String
        let line: RetryLineKey
    }

    let store: TranscriptStore
    /// 每次有新句对进来就 +1：存档是后台写的，页面靠它触发重新读文件。
    let refreshToken: Int

    @State private var lines: [TranscriptStore.Line] = []
    @State private var availableDates: [Date] = []
    @State private var selectedDate: Date
    @State private var loadedDate: Date
    @State private var query = ""
    @State private var translator = RetryTranslator()
    @State private var retryStatuses: [RetryStatusKey: RetryLineStatus] = [:]
    /// 回看默认最新在上：打开就看到刚才那句，不用滑到底。用户可在工具栏切回时间顺序，记住选择。
    @AppStorage("transcriptNewestFirst") private var newestFirst = true

    init(store: TranscriptStore, refreshToken: Int) {
        self.store = store
        self.refreshToken = refreshToken
        let today = store.dayStart(for: Date())
        _selectedDate = State(initialValue: today)
        _loadedDate = State(initialValue: today)
    }

    private var todayKey: String {
        store.dateKey(for: Date())
    }

    private var selectedKey: String {
        store.dateKey(for: selectedDate)
    }

    private var isViewingToday: Bool {
        selectedKey == todayKey
    }

    private var pastDateKeys: [String] {
        availableDates
            .map { store.dateKey(for: $0) }
            .filter { $0 != todayKey }
    }

    private var navigationTitleText: String {
        isViewingToday ? "今天的字幕" : displayDate(forKey: selectedKey, fallback: selectedDate)
    }

    private var selectedDayBinding: Binding<String> {
        Binding(
            get: { selectedKey },
            set: { newKey in
                guard newKey != selectedKey else { return }
                let nextDate = store.date(forKey: newKey) ?? store.dayStart(for: Date())
                selectedDate = nextDate
                reload(for: nextDate)
            }
        )
    }

    /// 存档文件按时间顺序；复制/导出也用这个顺序，只有屏幕上的排列跟着开关走。
    private var matchedLines: [TranscriptStore.Line] {
        TranscriptStore.filter(lines, query: query)
    }

    private var displayedLines: [TranscriptStore.Line] {
        newestFirst ? matchedLines.reversed() : matchedLines
    }

    /// 所选日期的文件真的落盘了才给导出项：当天的存档是后台边跑边写的，刚打开页面时可能还没有文件。
    private var canExport: Bool {
        guard !lines.isEmpty else { return false }
        return FileManager.default.fileExists(atPath: store.fileURL(for: selectedDate).path)
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: DesignTokens.Space.pairGap) {
                if lines.isEmpty {
                    emptyHint(isViewingToday ? "今天还没有字幕" : DesignTokens.Text.historyEmptySelectedDay)
                } else if matchedLines.isEmpty {
                    emptyHint("没有匹配的字幕")
                } else {
                    ForEach(displayedLines) { line in
                        card(line)
                    }
                }
            }
            .padding(DesignTokens.Space.l)
        }
        .background(Color(tokenHex: DesignTokens.Color.background).ignoresSafeArea())
        .navigationTitle(navigationTitleText)
        .searchable(text: $query, prompt: "搜索原文或译文")
        .toolbar {
            ToolbarItem(placement: .topBarLeading) {
                Button {
                    newestFirst.toggle()
                } label: {
                    Label(
                        newestFirst ? "最新在上" : "最早在上",
                        systemImage: newestFirst ? "arrow.up.to.line" : "arrow.down.to.line"
                    )
                    .labelStyle(.titleAndIcon)
                }
                .accessibilityHint("切换字幕的排列顺序")
            }
            if !pastDateKeys.isEmpty || !isViewingToday {
                ToolbarItem(placement: .topBarTrailing) {
                    Menu {
                        Picker(DesignTokens.Text.historySelectDate, selection: selectedDayBinding) {
                            Text(DesignTokens.Text.historyToday).tag(todayKey)
                            if !pastDateKeys.isEmpty {
                                Divider()
                                ForEach(pastDateKeys, id: \.self) { key in
                                    Text(displayDate(forKey: key)).tag(key)
                                }
                            }
                        }
                    } label: {
                        Label(DesignTokens.Text.historySelectDate, systemImage: "calendar")
                    }
                    .accessibilityLabel(DesignTokens.Text.historySelectDate)
                }
            }
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        UIPasteboard.general.string = TranscriptStore.plainText(self.matchedLines)
                    } label: {
                        Label("复制全部", systemImage: "doc.on.doc")
                    }
                    .disabled(self.matchedLines.isEmpty)

                    if self.canExport {
                        ShareLink(item: self.store.fileURL(for: self.selectedDate)) {
                            Label("导出当天文件", systemImage: "square.and.arrow.up")
                        }
                    }
                } label: {
                    Image(systemName: "square.and.arrow.up")
                }
            }
        }
        .preferredColorScheme(.dark)
        .onAppear { reload() }
        .onChange(of: refreshToken) { _, _ in reload() }
    }

    /// 每句一张卡片，配色和桌面版一致：原文近白、译文暖黄、没译文显示可点按的重试按钮。
    private func card(_ line: TranscriptStore.Line) -> some View {
        let retryKey = RetryLineKey(id: line.id, time: line.time, src: line.src)
        let statusKey = RetryStatusKey(day: loadedKey, line: retryKey)
        let retryStatus = retryStatuses[statusKey] ?? .idle
        let needsTranslation = line.dst.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty

        return VStack(alignment: .leading, spacing: DesignTokens.Space.xs) {
            Text(line.time)
                .font(.caption)
                .foregroundColor(Color(tokenHex: DesignTokens.Color.subtitleSecondary))
            Text(line.src)
                .font(.system(size: bodyFontSize * DesignTokens.TypeScale.sourceScale))
                .foregroundColor(Color(tokenHex: DesignTokens.Color.subtitleSource))
            if needsTranslation {
                untranslatedButton(for: line, status: retryStatus)
            } else {
                Text(line.dst)
                    .font(.system(size: bodyFontSize))
                    .foregroundColor(Color(tokenHex: DesignTokens.Color.subtitleTranslation))
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .padding(DesignTokens.Space.m)
        .background(
            RoundedRectangle(cornerRadius: DesignTokens.Radius.card)
                .fill(Color.white.opacity(0.06))
        )
        .contextMenu {
            if RetryTranslationPolicy.canRetry(src: line.src, dst: line.dst, isRetrying: false) {
                Button(DesignTokens.Text.retryTranslation) {
                    retryTranslation(for: line)
                }
                .disabled(retryStatus == .retrying)
            }
            Button("复制原文") {
                UIPasteboard.general.string = line.src
            }
            if !needsTranslation {
                Button("复制译文") {
                    UIPasteboard.general.string = line.dst
                }
            }
            Button("复制这一句") {
                UIPasteboard.general.string = needsTranslation
                    ? line.src
                    : "\(line.src)\n\(line.dst)"
            }
        }
    }

    @ViewBuilder
    private func untranslatedButton(for line: TranscriptStore.Line, status: RetryLineStatus) -> some View {
        Button {
            retryTranslation(for: line)
        } label: {
            switch status {
            case .idle:
                Text(DesignTokens.Text.untranslated)
                    .font(.system(size: bodyFontSize))
                    .foregroundColor(Color(tokenHex: DesignTokens.Color.untranslated))
            case .retrying:
                HStack(spacing: DesignTokens.Space.s) {
                    ProgressView()
                        .controlSize(.small)
                    Text(DesignTokens.Text.translating)
                        .font(.system(size: bodyFontSize))
                        .foregroundColor(Color(tokenHex: DesignTokens.Color.untranslated))
                }
            case .failed(let reason):
                Text(reason)
                    .font(.system(size: bodyFontSize))
                    .foregroundColor(Color(tokenHex: DesignTokens.Color.statusError))
            }
        }
        .buttonStyle(.bordered)
        .controlSize(.small)
        .tint(
            status.isFailed
                ? Color(tokenHex: DesignTokens.Color.statusError)
                : Color(tokenHex: DesignTokens.Color.untranslated)
        )
        .disabled(status == .retrying)
        .accessibilityLabel(DesignTokens.Text.retryTranslation)
    }

    private func retryTranslation(for line: TranscriptStore.Line) {
        let retryKey = RetryLineKey(id: line.id, time: line.time, src: line.src)
        let targetDate = loadedDate
        let targetKey = store.dateKey(for: targetDate)
        let statusKey = RetryStatusKey(day: targetKey, line: retryKey)
        let current = retryStatuses[statusKey] ?? .idle
        guard RetryTranslationPolicy.canRetry(
            src: line.src,
            dst: line.dst,
            isRetrying: current == .retrying
        ) else {
            return
        }

        retryStatuses[statusKey] = .retrying

        Task {
            do {
                let translated = try await translator.translate(key: retryKey, text: line.src)
                let saved = await store.updateTranslation(for: line, on: targetDate, dst: translated)
                if saved {
                    retryStatuses.removeValue(forKey: statusKey)
                    if store.dateKey(for: selectedDate) == targetKey {
                        reload(for: targetDate)
                    }
                } else {
                    retryStatuses[statusKey] = .failed(DesignTokens.Text.retryTxFailed)
                }
            } catch RetryTranslationFailure.alreadyInFlight {
                // 同一句已经在翻，保持当前进度状态不排队。
            } catch let failure as RetryTranslationFailure {
                retryStatuses[statusKey] = .failed(RetryTranslationPolicy.failureMessage(for: failure))
            } catch is CancellationError {
                retryStatuses.removeValue(forKey: statusKey)
            } catch {
                retryStatuses[statusKey] = .failed(DesignTokens.Text.retryTxFailed)
            }
        }
    }

    private var loadedKey: String {
        store.dateKey(for: loadedDate)
    }

    private func displayDate(forKey key: String, fallback: Date? = nil) -> String {
        guard let date = store.date(forKey: key) ?? fallback else { return key }
        return date.formatted(date: .abbreviated, time: .omitted)
    }

    private func emptyHint(_ text: String) -> some View {
        Text(text)
            .font(.body)
            .foregroundColor(Color(tokenHex: DesignTokens.Color.subtitleSecondary))
    }

    /// 字号以系统 body 为基准，原文再乘 sourceScale，跟随动态字体。
    private var bodyFontSize: CGFloat {
        UIFont.preferredFont(forTextStyle: .body).pointSize
    }

    private func reload(for targetDate: Date? = nil) {
        let dateToLoad = targetDate ?? selectedDate
        let targetKey = store.dateKey(for: dateToLoad)
        Task {
            async let datesTask = store.availableDates()
            async let linesTask = store.loadLines(for: dateToLoad)
            let (dates, loadedLines) = await (datesTask, linesTask)
            availableDates = dates
            // 防止快速切换日期时，旧日期的异步读结果覆盖新选日期的列表。
            guard store.dateKey(for: selectedDate) == targetKey else { return }
            loadedDate = dateToLoad
            lines = loadedLines
        }
    }
}

/// 把生成的设计令牌 "#RRGGBB" 转成颜色；解析失败回退白色，令牌坏了要看得出来。
/// 名字带 token 前缀，避免和别的文件里的 Color(hex:) 撞车。
fileprivate extension Color {
    init(tokenHex: String) {
        let hex = tokenHex.hasPrefix("#") ? String(tokenHex.dropFirst()) : tokenHex
        guard hex.count == 6, let value = UInt32(hex, radix: 16) else {
            self = .white
            return
        }
        self.init(
            .sRGB,
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255,
            opacity: 1
        )
    }
}
