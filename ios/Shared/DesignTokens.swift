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
        static let statusLocal: String = "本机"
        static let statusHybridPrefix: String = "混合·"
        static let statusCloud: String = "云端·Groq"
        static let statusCloudKeyInvalid: String = "云端 key 无效"
        static let statusCloudCooldownSuffix: String = " · 云端限流冷却"
        static let untranslated: String = "未翻译 ⟳"
        static let nodeUnavailableSuffix: String = " 不可用，已用本机"
        static let nodeRecoveredWaitingSuffix: String = " 已恢复，等本句结束后切回"
        static let nodeRecoveredSwitchedSuffix: String = " 已恢复，已切回节点"
        static let nodeLoadingSuffix: String = " 正在加载模型…"
    }
    enum Palette {
        static let text: [(name: String, hex: String)] = [(name: "白", hex: "#F0F0F0"), (name: "暖黄", hex: "#FFE894"), (name: "浅青", hex: "#9FE7F5"), (name: "浅绿", hex: "#B8F2B0"), (name: "浅粉", hex: "#FFC4D6")]
        static let background: [(name: String, hex: String)] = [(name: "黑", hex: "#000000"), (name: "深灰", hex: "#1E1E1E"), (name: "深蓝", hex: "#0F1B2D"), (name: "深绿", hex: "#10241A"), (name: "深紫", hex: "#1F1630")]
    }
}
