import AppIntents
import SwiftUI
import WidgetKit

/// 控制中心快捷按钮：一键打开主 App 并开始麦克风字幕。
/// 扩展进程不能依赖 `supportedModes = .foreground` 切回主 App 或注入主 App 服务，
/// 因此点按时通过 `OpenURLIntent` 打开 `rsnode://start-mic`，由主 App 的 `.onOpenURL` 接管启动。
struct StartMicCaptionsControl: ControlWidget {
    static let kind = "com.yilinwang.realtimesubtitle.start-mic-captions"
    private static let startMicURL = URL(string: "rsnode://start-mic")!

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: OpenURLIntent(Self.startMicURL)) {
                Label(DesignTokens.Text.micStartTitle, systemImage: "mic.fill")
            }
        }
        .displayName(LocalizedStringResource(String.LocalizationValue(DesignTokens.Text.micSubtitleTitle)))
        .description("打开实时字幕并开始麦克风采集")
    }
}
