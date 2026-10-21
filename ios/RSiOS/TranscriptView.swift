import SwiftUI

/// 「今天的字幕」页面：读当天的存档文件，可滚动回看。
///
/// 为什么需要这一页：扩展只把最近 3 句放进 Keychain（PiP 画面就那么多），
/// 但存档文件里存的是全天每一句。页面直接读存档文件，主 App 一打开就能回看。
struct TranscriptView: View {
    let store: TranscriptStore
    /// 每次有新句对进来就 +1：存档是后台写的，页面靠它触发重新读文件。
    let refreshToken: Int

    @State private var lines: [TranscriptStore.Line] = []

    var body: some View {
        ScrollView {
            LazyVStack(alignment: .leading, spacing: 12) {
                if lines.isEmpty {
                    Text("今天还没有字幕")
                        .font(.body)
                        .foregroundColor(.secondary)
                } else {
                    ForEach(lines) { line in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(line.time)
                                .font(.caption)
                                .foregroundColor(.secondary)
                            Text(line.src)
                                .font(.body)
                            if !line.dst.isEmpty {
                                Text(line.dst)
                                    .font(.body)
                                    .foregroundColor(.secondary)
                            }
                        }
                        .frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
            .padding()
        }
        .navigationTitle("今天的字幕")
        .onAppear { reload() }
        .onChange(of: refreshToken) { _, _ in reload() }
    }

    private func reload() {
        lines = store.lines(for: Date())
    }
}
