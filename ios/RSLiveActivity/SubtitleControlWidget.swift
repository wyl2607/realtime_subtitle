import AppIntents
import SwiftUI
import WidgetKit

/// 控制中心快捷按钮：一键打开主 App 并开始麦克风字幕。
/// 点按时通过 `StartMicCaptionsIntent`（`supportedModes = .foreground`）拉起主 App 执行。
struct StartMicCaptionsControl: ControlWidget {
    static let kind = "com.yilinwang.realtimesubtitle.start-mic-captions"

    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: Self.kind) {
            ControlWidgetButton(action: StartMicCaptionsIntent()) {
                Label("开始麦克风字幕", systemImage: "mic.fill")
            }
        }
        .displayName("麦克风字幕")
        .description("打开实时字幕并开始麦克风采集")
    }
}
