import AppIntents
import Foundation

/// 停止麦克风字幕。
/// 只需停掉正在运行的采集并结束锁屏实时活动，可在后台完成而不打断用户当前界面。
struct StopCaptionsIntent: AppIntent {
    static let title: LocalizedStringResource = "停止麦克风字幕"
    static let description: IntentDescription? = IntentDescription("停止正在运行的麦克风字幕采集。")
    static let supportedModes: IntentModes = .background

    @Dependency
    private var model: AppModel

    func perform() async throws -> some IntentResult & ProvidesDialog {
        let decision = await MainActor.run {
            MicCaptionIntentPolicy.decideStop(micIsRunning: model.mic.isRunning)
        }
        switch decision {
        case .stop:
            await MainActor.run {
                model.stopMic()
            }
            return .result(dialog: IntentDialog(LocalizedStringResource(String.LocalizationValue(DesignTokens.Text.micStopped))))
        case .ignoreAlreadyStopped:
            return .result(dialog: IntentDialog(LocalizedStringResource(String.LocalizationValue(DesignTokens.Text.micNotRunning))))
        }
    }
}
