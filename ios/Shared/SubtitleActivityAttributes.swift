import ActivityKit
import Foundation

/// 锁屏/灵动岛上的实时字幕。主 App 和锁屏小组件扩展共用这一个类型。
///
/// 熄屏时用户看不到画中画，苹果推荐的做法是实时活动：识别、翻译、存档照常在后台跑，
/// 锁屏只显示最新一句原文和译文。
struct SubtitleActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var source: String
        var translation: String
        /// 状态词（本机 / 混合·节点 / 云端），和字幕页、画中画同一套文案。
        var status: String
    }

    /// 音源（屏幕广播 / 麦克风），开始后不变。
    var sourceName: String
}
