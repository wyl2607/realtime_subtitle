// 由 scripts/design/gen_tokens.py 从 design/tokens.json 生成，不要手改。

enum DesignTokens {
    enum Color {
        static let subtitleSource: String = "#F0F0F0"
        static let subtitleTranslation: String = "#FFE894"
        static let subtitleDraft: String = "#9DB8D9"
        static let subtitleSecondary: String = "#A8A8A8"
        static let background: String = "#000000"
        static let statusNormal: String = "#8FD19E"
        static let statusWarning: String = "#F2C14E"
        static let statusError: String = "#F07167"
        static let untranslated: String = "#8A8A8A"
    }
    enum Opacity {
        static let backgroundDefault: Double = 0.72
        static let olderPair: Double = 0.6
        static let waterfallOldest: Double = 0.35
    }
    enum TypeScale {
        static let subtitleDefault: Double = 28.0
        static let subtitleMin: Double = 16.0
        static let subtitleMax: Double = 72.0
        static let sourceScale: Double = 0.86
        static let status: Double = 13.0
        static let settingsBody: Double = 13.0
    }
    enum Space {
        static let xs: Double = 4.0
        static let s: Double = 8.0
        static let m: Double = 12.0
        static let l: Double = 16.0
        static let xl: Double = 24.0
        static let windowPadV: Double = 18.0
        static let windowPadH: Double = 22.0
        static let pairGap: Double = 8.0
    }
    enum Radius {
        static let none: Double = 0.0
        static let small: Double = 8.0
        static let card: Double = 16.0
    }
    enum Motion {
        static let fadeInMs: Double = 150.0
        static let shiftUpMs: Double = 180.0
        static let resizeMs: Double = 200.0
        static let minHoldSec: Double = 3.0
    }
    enum Text {
        static let micSubtitleTitle: String = "麦克风字幕"
        static let micStartButton: String = "开始"
        static let micStopButton: String = "停止"
        static let micStatusIdle: String = "未启动"
        static let micStatusRunning: String = "正在采集麦克风音频"
        static let micPermissionDenied: String = "麦克风权限被拒绝，请到 设置 → 实时字幕 打开"
        static let micBlockedByBroadcast: String = "请先停止屏幕广播"
        static let micStoppedByBroadcast: String = "屏幕广播已开始，麦克风已停止"
        static let micInterrupted: String = "麦克风被系统中断，未自动恢复"
        static let micError: String = "麦克风无法启动，请稍后重试"
        static let statusLocal: String = "本机"
        static let statusHybridPrefix: String = "混合·"
        static let statusCloud: String = "云端·Groq"
        static let statusCloudKeyInvalid: String = "云端 key 无效"
        static let statusCloudCooldownSuffix: String = " · 云端限流冷却"
        static let untranslated: String = "未翻译 ⟳"
        static let translating: String = "翻译中…"
        static let nodeUnavailableSuffix: String = " 不可用，已用本机"
        static let nodeRecoveredWaitingSuffix: String = " 已恢复，等本句结束后切回"
        static let nodeRecoveredSwitchedSuffix: String = " 已恢复，已切回节点"
        static let nodeLoadingSuffix: String = " 正在加载模型…"
        static let nodeConnectedSuffix: String = " 已连接"
        static let nodeUnavailableReconnectingSuffix: String = " 不可用，正在重连"
        static let localUnavailableReconnectingPrefix: String = "本机识别不可用，正在重连 "
        static let localAsrUnsupported: String = "本机不支持德语识别"
        static let localAssetsMissing: String = "本机识别资源未下载，请点「准备本机识别资源」"
        static let localFormatFailed: String = "本机音频格式不支持，请停止后重新开始"
        static let localAsrFailed: String = "本机识别出错，请停止后重新开始"
        static let localTxUnavailable: String = "本机翻译资源未下载，只显示原文"
        static let localNeedsIOS26: String = "本机识别需要 iOS 26 或更新"
        static let localSpeechDenied: String = "语音识别权限被拒绝，请到 设置 → 实时字幕 打开"
        static let nodeNotListedSuffix: String = " 不在节点列表，已用本机"
        static let playbackCaptureBlocked: String = "该应用禁止内录，请改用麦克风"
    }
    enum Palette {
        static let text: [(name: String, hex: String)] = [(name: "白", hex: "#F0F0F0"), (name: "暖黄", hex: "#FFE894"), (name: "浅青", hex: "#9FE7F5"), (name: "浅绿", hex: "#B8F2B0"), (name: "浅粉", hex: "#FFC4D6")]
        static let background: [(name: String, hex: String)] = [(name: "黑", hex: "#000000"), (name: "深灰", hex: "#1E1E1E"), (name: "深蓝", hex: "#0F1B2D"), (name: "深绿", hex: "#10241A"), (name: "深紫", hex: "#1F1630")]
    }
}
