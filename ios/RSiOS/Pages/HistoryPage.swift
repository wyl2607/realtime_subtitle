import SwiftUI

/// 回看页：当天存档，可搜索、导出。以前藏在字幕页最底下的一个按钮里。
struct HistoryPage: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationStack {
            TranscriptView(store: model.store, refreshToken: model.transcriptRefresh)
        }
    }
}
