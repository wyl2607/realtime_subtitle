import AppIntents
import SwiftUI

@main
struct RSiOSApp: App {
    /// 整个 App 只有这一份共享状态，四个页面从环境里取；快捷指令通过 AppDependencyManager 注入同一份实例。
    @State private var model: AppModel

    init() {
        let model = AppModel()
        let intentController = model.makeIntentController()
        _model = State(initialValue: model)
        AppDependencyManager.shared.add(dependency: model)
        AppDependencyManager.shared.add(dependency: intentController)
        RealtimeSubtitleShortcuts.updateAppShortcutParameters()
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
        }
    }
}
