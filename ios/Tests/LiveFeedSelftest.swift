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

        var statusSource: SharedStore.ExtStatusEntry? = SharedStore.ExtStatusEntry(
            status: "engine=node last=ready",
            updatedAt: nil
        )
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
        check(feed.lastStatusUpdate == nil, "旧格式无时间戳时兜底轮询不能编造「扩展刚写过」")
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
        statusSource = SharedStore.ExtStatusEntry(status: "engine=node last=ok", updatedAt: nil)
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

        // HC-U08：锁屏时 Keychain 拒绝访问，三份读取都返回 nil——必须保留上一次的值，
        // 不能把状态改成「无」、把字幕和草稿清空（否则画中画/锁屏实时活动一起变空，麦克风还会被误停）。
        let beforeLock = changes
        statusSource = nil
        linesSource = nil
        draftSource = nil
        feed.refresh()
        check(feed.status == "engine=node last=ok", "HC-U08：读不到状态时应保留上一次的状态")
        check(feed.lines.count == 1, "HC-U08：读不到句对时不应清空字幕")
        check(feed.draft == "Guten", "HC-U08：读不到草稿时不应清空草稿")
        check(changes == beforeLock, "HC-U08：读不到时不应发布变更")

        // HC-U11：字幕页滚动字幕的累积。引擎只给最近 3 句，主 App 必须自己按「会话+id」接起来。
        let a1 = RecentLine(id: 1, src: "A", dst: "", session: "s1")
        let a2 = RecentLine(id: 2, src: "B", dst: "", session: "s1")
        let a3 = RecentLine(id: 3, src: "C", dst: "", session: "s1")
        var history = CaptionHistory.merge([], with: [a1, a2, a3])
        history = CaptionHistory.merge(history, with: [a2, a3, RecentLine(id: 4, src: "D", dst: "", session: "s1")])
        check(history.map(\.src) == ["A", "B", "C", "D"], "HC-U11：滑出最近 3 句的旧句要留在滚动字幕里")
        history = CaptionHistory.merge(history, with: [RecentLine(id: 3, src: "C", dst: "丙", session: "s1")])
        check(history.count == 4 && history[2].dst == "丙", "HC-U11：后到的译文要原地补上，不能多出一句")
        history = CaptionHistory.merge(history, with: [RecentLine(id: 1, src: "E", dst: "", session: "s2")])
        check(history.map(\.src) == ["A", "B", "C", "D", "E"], "HC-U11：新会话 id 从 1 重计时不能顶替旧会话的句子")
        let many = (1...10).map { RecentLine(id: $0, src: "\($0)", dst: "", session: "s3") }
        check(CaptionHistory.merge([], with: many, limit: 4).map(\.id) == [7, 8, 9, 10], "HC-U11：超过上限只留最新的")

        // HC-B05：冷启动从 SharedStore 恢复持久化状态时间戳，不依赖进程内 Darwin 通知。
        let coldDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("livefeed-coldstart-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: coldDir) }
        let coldStore = SharedStore(rootURL: coldDir, legacyRead: { _ in nil }, legacyDelete: { _ in true })

        let freshWrittenAt = fixedNow.addingTimeInterval(-5)
        check(coldStore.writeExtStatus("blocks=4 src=broadcast", at: freshWrittenAt), "HC-B05：写入带时间戳的状态应成功")
        var simulatedNow = fixedNow
        let coldFreshFeed = LiveFeed(
            readStatus: { coldStore.readExtStatusEntry() },
            readLines: { nil },
            readDraft: { nil },
            now: { simulatedNow }
        )
        check(coldFreshFeed.status == "blocks=4 src=broadcast", "HC-B05：冷启动应读出 broadcast 状态")
        check(
            coldFreshFeed.lastStatusUpdate.map { abs($0.timeIntervalSince(freshWrittenAt)) < 0.001 } == true,
            "HC-B05：冷启动必须从持久化时间填充 lastStatusUpdate，供 CaptureArbiter 拒绝麦克风"
        )
        var coldChanges = 0
        let coldToken = NotificationCenter.default.addObserver(
            forName: LiveFeed.didChangeNotification,
            object: coldFreshFeed,
            queue: nil
        ) { _ in
            coldChanges += 1
        }
        defer { NotificationCenter.default.removeObserver(coldToken) }
        simulatedNow = fixedNow.addingTimeInterval(20)
        coldFreshFeed.refresh()
        check(
            coldFreshFeed.lastStatusUpdate.map { abs($0.timeIntervalSince(freshWrittenAt)) < 0.001 } == true,
            "HC-B05：未发生新写入的轮询不得延长 lastStatusUpdate，状态过期后自然放行麦克风"
        )
        check(coldChanges == 0, "HC-B05 + HC-P01：时间戳未变化时轮询不应重复发通知")

        // HC-B05：未来时间戳（时钟回拨）冷启动不得被采纳，也不能在时钟追上时重新生效。
        let futureAt = fixedNow.addingTimeInterval(60)
        check(coldStore.writeExtStatus("blocks=9 src=broadcast", at: futureAt), "HC-B05：写入未来时间戳测试状态")
        var clockNow = fixedNow
        let futureFeed = LiveFeed(
            readStatus: { coldStore.readExtStatusEntry() },
            readLines: { nil },
            readDraft: { nil },
            now: { clockNow }
        )
        check(futureFeed.lastStatusUpdate == nil, "HC-B05：未来时间戳不得设置 lastStatusUpdate")
        clockNow = futureAt
        futureFeed.refresh()
        check(futureFeed.lastStatusUpdate == nil, "HC-B05：墙钟追上未来时间戳后旧状态也不得复活阻塞麦克风")

        let recoveredAt = futureAt.addingTimeInterval(1)
        clockNow = recoveredAt
        check(coldStore.writeExtStatus("blocks=10 src=broadcast", at: recoveredAt), "HC-B05：回拨后新写入应恢复正常")
        futureFeed.refresh()
        check(
            futureFeed.lastStatusUpdate.map { abs($0.timeIntervalSince(recoveredAt)) < 0.001 } == true,
            "HC-B05：回拨后新的正常时间戳应恢复同步"
        )

        // HC-B05：旧格式无时间戳（纯文本 ext_status）→ lastStatusUpdate 保持 nil（未知新鲜度保守放行，不永久锁死）。
        let legacyStatusURL = coldDir.appendingPathComponent(SharedStore.Key.extStatus, isDirectory: false)
        try? Data("blocks=7 src=broadcast".utf8).write(to: legacyStatusURL, options: .atomic)
        let legacyFeed = LiveFeed(
            readStatus: { coldStore.readExtStatusEntry() },
            readLines: { nil },
            readDraft: { nil },
            now: { fixedNow }
        )
        check(legacyFeed.status == "blocks=7 src=broadcast", "HC-B05：旧格式状态文本应正常显示")
        check(legacyFeed.lastStatusUpdate == nil, "HC-B05：旧格式无时间戳时 lastStatusUpdate 应保持 nil")

        if failures.isEmpty {
            print("LIVE FEED SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
