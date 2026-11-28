import AppIntents

/// 向 Siri、快捷指令 App 与聚焦搜索暴露的中文快捷短语。
/// 屏幕广播只能通过系统 Broadcast Picker 启动，这里不提供广播类 Intent。
struct RealtimeSubtitleShortcuts: AppShortcutsProvider {
    static var shortcutTileColor: ShortcutTileColor { .navy }

    static var appShortcuts: [AppShortcut] {
        AppShortcut(
            intent: StartMicCaptionsIntent(),
            phrases: [
                "用 \(.applicationName) 开始字幕",
                "用 \(.applicationName) 开始麦克风字幕",
                "\(.applicationName) 开始字幕"
            ],
            shortTitle: "开始麦克风字幕",
            systemImageName: "mic.fill"
        )
        AppShortcut(
            intent: StopCaptionsIntent(),
            phrases: [
                "用 \(.applicationName) 停止字幕",
                "用 \(.applicationName) 停止麦克风字幕",
                "\(.applicationName) 停止字幕"
            ],
            shortTitle: "停止麦克风字幕",
            systemImageName: "mic.slash.fill"
        )
    }
}
