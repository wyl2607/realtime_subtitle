import AppIntents
import Foundation

/// 开始麦克风字幕。
/// 麦克风采集与实时活动必须从前台启动；用 iOS 26 的 `supportedModes = .foreground`
/// 替代已弃用的 `openAppWhenRun`，从快捷指令 / Siri / 操作按钮 / 控制中心触发时直接切回主 App 执行。
struct StartMicCaptionsIntent: AppIntent {
    static let title: LocalizedStringResource = "开始麦克风字幕"
    static let description: IntentDescription? = IntentDescription("打开实时字幕并开始麦克风字幕采集。")
    static let supportedModes: IntentModes = .foreground

    @Dependency
    private var controller: MicCaptionIntentController

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let decision = await controller.decideStart(Date())
        switch decision {
        case .start:
            await controller.startMic()
            return .result(dialog: IntentDialog("已开始麦克风字幕"))
        case .ignoreAlreadyRunning:
            return .result(dialog: IntentDialog("麦克风字幕已在运行"))
        case .blockedByBroadcast:
            return .result(dialog: IntentDialog("请先停止屏幕广播"))
        }
    }
}
