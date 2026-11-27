import Foundation
import Observation
import Speech
import Translation

/// 本机识别/翻译资源的准备流程。
///
/// 从视图里搬出来的原因：以前这段逻辑和十几个 @State 混在一个 1300 多行的视图里，
/// 字幕页、设置页、诊断页都要看资源状态，只能各自重复判断。现在状态只有这一份。
@MainActor
@Observable
final class AssetPreparer {
    enum Readiness: Equatable {
        case unknown
        case ready
        case missing
    }

    private(set) var isPreparing = false
    private(set) var statusText = ""
    /// 语音授权被拒的提示单独存：statusText 每完成一步都会整段重写，拼进去会被覆盖。
    private(set) var speechDeniedHint: String?
    private(set) var asr: Readiness = .unknown
    private(set) var translation: Readiness = .unknown
    /// 交给 RootView 的 `.translationTask`：翻译资源下载要弹系统确认框，只有它能拿到会话。
    var translationConfig: TranslationSession.Configuration?

    @ObservationIgnored private var didAutoCheck = false

    /// 字幕页顶部要不要提示：只要有一项确定缺失就提示，未知时不打扰。
    var needsAttention: Bool {
        asr == .missing || translation == .missing
    }

    private static let source = Locale.Language(identifier: "de")
    private static let target = Locale.Language(identifier: "zh-Hans")

    /// 打开 App 时自动查一次：缺资源就直接走准备流程。以前只有手点「准备本机识别资源」
    /// 才会装，重装 App 后资源跟着没了，用户开了广播只看到空白画中画。节点档用不到本机资源。
    func prepareIfMissing() {
        guard !didAutoCheck else { return }
        didAutoCheck = true
        let mode = SharedStore.readEngineMode()
        guard mode == "local" || mode == "auto" else { return }
        Task {
            guard let transcriber = await Self.makeTranscriber() else { return }
            let asrStatus = await AssetInventory.status(forModules: [transcriber])
            let txStatus = await LanguageAvailability().status(from: Self.source, to: Self.target)
            asr = asrStatus == .installed ? .ready : .missing
            translation = txStatus == .installed ? .ready : .missing
            if needsAttention, !isPreparing { prepare() }
        }
    }

    func prepare() {
        isPreparing = true
        statusText = "检查资源状态..."
        speechDeniedHint = nil
        checkSpeechAuthorization()

        Task {
            do {
                guard let transcriber = await Self.makeTranscriber() else {
                    isPreparing = false
                    statusText = "错误：不支持德语识别"
                    return
                }
                var asrStatus = await AssetInventory.status(forModules: [transcriber])
                if asrStatus != .installed {
                    // 只查状态永远不会把资源装上：本机引擎缺资源时报 asr_assets_missing，
                    // 界面又把用户指回这里，等于死循环。必须真的发起安装。
                    statusText = "正在下载识别资源..."
                    if let request = try await AssetInventory.assetInstallationRequest(supporting: [transcriber]) {
                        try await request.downloadAndInstall()
                    }
                    // 用户可能中途取消，状态以安装后的查询为准。
                    asrStatus = await AssetInventory.status(forModules: [transcriber])
                }
                asr = asrStatus == .installed ? .ready : .missing

                let txStatus = await LanguageAvailability().status(from: Self.source, to: Self.target)
                translation = txStatus == .installed ? .ready : .missing
                statusText = "ASR资源: \(asrStatus) · 翻译 de→zh-Hans: \(txStatus)"
                if asrStatus != .installed {
                    statusText += " · 识别资源仍未安装，请重试"
                }

                if txStatus != .installed {
                    var next = TranslationSession.Configuration(source: Self.source, target: Self.target)
                    // 同一对语言连续点按钮时 Configuration 相等，修饰符不会重开会话；
                    // invalidate 保证这一次点击仍然触发系统下载确认。
                    next.invalidate()
                    translationConfig = next
                    statusText += " · 等待系统确认下载翻译资源..."
                } else {
                    isPreparing = false
                }
            } catch {
                isPreparing = false
                statusText = "错误: \(error.localizedDescription)"
            }
        }
    }

    /// `.translationTask` 回调：prepareTranslation 正常返回不代表真的装上（用户可能取消），
    /// 以再次查询 LanguageAvailability 为准。
    /// nonisolated：会话不是 Sendable，必须留在 `.translationTask` 给它的上下文里用，
    /// 只把查询结果带回主线程。
    nonisolated func prepareTranslation(_ session: TranslationSession) async {
        _ = try? await session.prepareTranslation()
        let status = await LanguageAvailability().status(
            from: Locale.Language(identifier: "de"),
            to: Locale.Language(identifier: "zh-Hans")
        )
        await finishTranslationPrep(installed: status == .installed, description: "\(status)")
    }

    /// 给 `.translationTask` 的回调在非隔离的静态函数里创建：写在视图里会被推断成主线程隔离，
    /// 会话就得跨隔离边界传递（Swift 6 拒绝，它不是 Sendable）。
    nonisolated static func translationAction(for preparer: AssetPreparer) -> @Sendable (TranslationSession) async -> Void {
        { session in await preparer.prepareTranslation(session) }
    }

    private func finishTranslationPrep(installed: Bool, description: String) {
        translation = installed ? .ready : .missing
        isPreparing = false
        statusText = "翻译 de→zh-Hans: \(description)"
    }

    private func checkSpeechAuthorization() {
        let mode = SharedStore.readEngineMode()
        guard mode == "local" || mode == "auto" else { return }
        switch SFSpeechRecognizer.authorizationStatus() {
        case .notDetermined:
            SFSpeechRecognizer.requestAuthorization { status in
                guard status == .denied || status == .restricted else { return }
                Task { @MainActor in self.speechDeniedHint = DesignTokens.Text.localSpeechDenied }
            }
        case .denied, .restricted:
            speechDeniedHint = DesignTokens.Text.localSpeechDenied
        default:
            break
        }
    }

    private static func makeTranscriber() async -> SpeechTranscriber? {
        guard let supported = await SpeechTranscriber.supportedLocale(equivalentTo: Locale(identifier: "de-DE")) else {
            return nil
        }
        return SpeechTranscriber(
            locale: supported,
            transcriptionOptions: [],
            reportingOptions: [.volatileResults, .fastResults],
            attributeOptions: [.audioTimeRange]
        )
    }
}
