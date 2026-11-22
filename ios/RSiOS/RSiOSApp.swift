import SwiftUI

@main
struct RSiOSApp: App {
    /// 整个 App 只有这一份共享状态，四个页面从环境里取。
    @State private var model = AppModel()

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(model)
        }
    }
}
