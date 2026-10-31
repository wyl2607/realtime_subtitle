import Foundation

/// 状态词和字幕样式都是界面层纯逻辑，单独编一个入口串起两组自测。
/// 这样 run-selftest.sh 只多一组，不用和已有的 @main 入口抢。
@main
struct UISelftestMain {
    static func main() {
        let statusOK = StatusSummarySelftest.run()
        let styleOK = SubtitleStyleSelftest.run()
        if statusOK && styleOK {
            print("UI SELFTEST OK")
        } else {
            print("UI SELFTEST FAILED")
            exit(1)
        }
    }
}
