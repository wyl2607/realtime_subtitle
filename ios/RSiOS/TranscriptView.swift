import SwiftUI
import UIKit

/// 「今天的字幕」页面：读当天的存档文件，可滚动回看。
///
/// 为什么需要这一页：扩展只把最近 3 句放进 Keychain（PiP 画面就那么多），
/// 但存档文件里存的是全天每一句。页面直接读存档文件，主 App 一打开就能回看。
struct TranscriptView: View {
    let store: TranscriptStore
    /// 每次有新句对进来就 +1：存档是后台写的，页面靠它触发重新读文件。
    let refreshToken: Int

    @State private var lines: [TranscriptStore.Line] = []
    @State private var loadedDate = Date()
    @State private var query = ""
    @State private var translator = RetryTranslator()
    @State private var retryStatuses: [RetryLineKey: RetryLineStatus] = [:]
    /// 回看默认最新在上：打开就看到刚才那句，不用滑到底。用户可在工具栏切回时间顺序，记住选择。
    @AppStorage("transcriptNewestFirst") private var newestFirst = true

    /// 存档文件按时间顺序；复制/导出也用这个顺序，只有屏幕上的排列跟着开关走。
    private var matchedLines: [TranscriptStore.Line] {
        TranscriptStore.filter(lines, query: query)
    }

    private var displayedLines: [TranscriptStore.Line] {
        newestFirst ? matchedLines.reversed() : matchedLines
    }

    /// 当天文件真的落盘了才给导出项：存档是后台边跑边写的，刚打开页面时可能还没有文件。
    private var canExport: Bool {
        guard !lines.isEmpty else { return false }
        return FileManager.default.fileExists(atPath: store.fileURL(for: Date()).path)
    }

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: DesignTokens.Space.pairGap) {
                if lines.isEmpty {
                    emptyHint("今天还没有字幕")
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
        .navigationTitle("今天的字幕")
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
            ToolbarItem(placement: .topBarTrailing) {
                Menu {
                    Button {
                        UIPasteboard.general.string = TranscriptStore.plainText(self.matchedLines)
                    } label: {
                        Label("复制全部", systemImage: "doc.on.doc")
                    }
                    .disabled(self.matchedLines.isEmpty)

                    if self.canExport {
                        ShareLink(item: self.store.fileURL(for: Date())) {
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
        let retryStatus = retryStatuses[retryKey] ?? .idle
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
        let key = RetryLineKey(id: line.id, time: line.time, src: line.src)
        let current = retryStatuses[key] ?? .idle
        guard RetryTranslationPolicy.canRetry(
            src: line.src,
            dst: line.dst,
            isRetrying: current == .retrying
        ) else {
            return
        }

        let targetDate = loadedDate
        retryStatuses[key] = .retrying

        Task {
            do {
                let translated = try await translator.translate(key: key, text: line.src)
                let saved = await store.updateTranslation(for: line, on: targetDate, dst: translated)
                if saved {
                    retryStatuses.removeValue(forKey: key)
                    reload()
                } else {
                    retryStatuses[key] = .failed(DesignTokens.Text.retryTxFailed)
                }
            } catch RetryTranslationFailure.alreadyInFlight {
                // 同一句已经在翻，保持当前进度状态不排队。
            } catch let failure as RetryTranslationFailure {
                retryStatuses[key] = .failed(RetryTranslationPolicy.failureMessage(for: failure))
            } catch is CancellationError {
                retryStatuses.removeValue(forKey: key)
            } catch {
                retryStatuses[key] = .failed(DesignTokens.Text.retryTxFailed)
            }
        }
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

    private func reload() {
        let today = Date()
        loadedDate = today
        lines = store.lines(for: today)
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
