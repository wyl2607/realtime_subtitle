import Foundation

/// 硬约束 HC-P01 的行为面：主 App 的运行态只用一个读取源，内容没变不发布。
///
/// 为什么要在命令行锁住：四个 0.5 秒定时器合并成一个之后，最容易退化的地方是
/// 「每次都无脑赋值/发布」——那样读取次数少了，但 SwiftUI 仍然整树重绘，
/// 收益基本归零。这里用 LiveFeed 自己的变更通知当计数器：内容相同的 refresh
/// 必须一次通知都不发。
@main
struct LiveFeedSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }

        var statusSource: String? = "engine=node last=ready"
        var linesSource: String? = nil
        var draftSource: String? = nil
        let fixedNow = Date(timeIntervalSince1970: 1_000)
        let feed = LiveFeed(
            readStatus: { statusSource },
            readLines: { linesSource },
            readDraft: { draftSource },
            now: { fixedNow }
        )

        check(feed.status == "engine=node last=ready", "首次 init 应读出状态")
        check(feed.lines.isEmpty, "没有句对时 lines 应为空")
        check(feed.draft.isEmpty, "没有草稿时 draft 应为空")
        check(feed.lastStatusUpdate == nil, "兜底轮询不能标记「扩展刚写过」")
        check(feed.statusNotificationCount == 0, "兜底轮询不增加状态通知计数")
        check(feed.linesNotificationCount == 0, "兜底轮询不增加句对通知计数")

        var changes = 0
        let token = NotificationCenter.default.addObserver(
            forName: LiveFeed.didChangeNotification,
            object: feed,
            queue: nil
        ) { _ in
            changes += 1
        }
        defer { NotificationCenter.default.removeObserver(token) }

        // 内容没变：一次变更通知都不能发。
        feed.refresh()
        check(changes == 0, "内容没变时不应发布变更（违反 HC-P01：合并轮询就不该重复通知）")

        // 状态真的变了：必须发布一次。
        statusSource = "engine=node last=ok"
        feed.refresh()
        check(changes == 1, "状态真的变了必须发布一次变更（实际 \(changes) 次）")
        check(feed.status == "engine=node last=ok", "状态应更新")

        // 句对 JSON 变化：解码后发布。
        linesSource = "[{\"id\":1,\"src\":\"Guten Tag.\",\"dst\":\"你好\",\"session\":\"s1\"}]"
        feed.refresh()
        check(
            feed.lines == [RecentLine(id: 1, src: "Guten Tag.", dst: "你好", session: "s1")],
            "句对应从 JSON 解码"
        )
        check(changes == 2, "句对变化必须发布变更（实际 \(changes) 次）")

        // 草稿变化：只更新草稿。
        draftSource = "Guten"
        feed.refresh()
        check(feed.draft == "Guten", "草稿应更新")
        check(changes == 3, "草稿变化必须发布变更（实际 \(changes) 次）")

        // 同样的输入再来一遍：不能再发布。
        feed.refresh()
        check(changes == 3, "内容没变时重复 refresh 不应再发布（实际 \(changes) 次）")

        // 坏 JSON（扩展写到一半）：保留上一份好句对，不能把字幕清空。
        linesSource = "not json"
        feed.refresh()
        check(feed.lines.count == 1, "坏 JSON 不应清空上一份句对")

        if failures.isEmpty {
            print("LIVE FEED SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
