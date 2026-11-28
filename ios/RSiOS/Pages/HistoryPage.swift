import SwiftUI

/// 回看页：按日查看存档（默认今天，可切换往日、搜索、导出）。以前藏在字幕页最底下的一个按钮里。
struct HistoryPage: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        NavigationStack {
            TranscriptView(store: model.store, refreshToken: model.transcriptRefresh)
        }
    }
}
