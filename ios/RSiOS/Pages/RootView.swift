import SwiftUI
import Translation

/// 四个标签页：字幕 / 回看 / 设置 / 诊断。
///
/// 页面之间只通过 AppModel 共享状态。句对/状态变化由 AppModel 订阅 LiveFeed 通知处理
/// （后台也要跑，见 HC-U09）；这里只留必须在前台响应的几个 onChange。
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
        .translationTask(model.assets.translationConfig, action: AssetPreparer.translationAction(for: model.assets))
        .onAppear { model.bootstrap() }
        .onOpenURL { model.handleOpenURL($0) }
        .onChange(of: model.mic.isRunning) { model.refreshLiveActivity() }
        .onChange(of: model.feed.lastStatusUpdate) { _, updatedAt in model.extensionStatusUpdated(updatedAt) }
        .onChange(of: model.appState.isInBackground) { _, inBackground in model.backgroundChanged(inBackground) }
    }
}
