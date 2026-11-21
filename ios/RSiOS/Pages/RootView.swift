import SwiftUI
import Translation

/// 四个标签页：字幕 / 回看 / 设置 / 诊断。
///
/// 页面之间只通过 AppModel 共享状态；监听运行态变化（状态词、句对、前后台）放在这里
/// 统一做一次，不跟着某个页面的生命周期——否则用户停在设置页时存档就不写了。
struct RootView: View {
    @Environment(AppModel.self) private var model

    var body: some View {
        @Bindable var model = model
        TabView(selection: $model.selectedTab) {
            Tab("字幕", systemImage: "captions.bubble", value: AppTab.live) {
                LivePage()
            }
            Tab("回看", systemImage: "clock.arrow.circlepath", value: AppTab.history) {
                HistoryPage()
            }
            Tab("设置", systemImage: "gearshape", value: AppTab.settings) {
                SettingsPage()
            }
            Tab("诊断", systemImage: "stethoscope", value: AppTab.diagnostics) {
                DiagnosticsPage()
            }
        }
        .translationTask(model.assets.translationConfig) { session in
            await model.assets.prepareTranslation(session)
        }
        .onAppear { model.bootstrap() }
        .onOpenURL { model.handleOpenURL($0) }
        .onChange(of: model.feed.lines) { model.ingestFeedLines() }
        .onChange(of: model.feed.status) { _, line in model.statusChanged(line) }
        .onChange(of: model.feed.lastStatusUpdate) { _, updatedAt in model.extensionStatusUpdated(updatedAt) }
        .onChange(of: model.appState.isInBackground) { _, inBackground in model.backgroundChanged(inBackground) }
    }
}
