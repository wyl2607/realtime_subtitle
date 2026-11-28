import Foundation

/// 当天字幕存档自测：跨天换文件、同键去重、译文后到原地更新、
/// 换会话的同 id 追加新行、重启后从文件重建去重索引。
/// 临时目录放在工作目录下的 `.tmp/`（run-selftest.sh 结束时会整体删掉）。
func runTranscriptStoreSelftest(_ check: @escaping (Bool, String) -> Void) {
    let base = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        .appendingPathComponent(".tmp")
        .appendingPathComponent("transcript-store-selftest-\(UUID().uuidString)")
    defer { try? FileManager.default.removeItem(at: base) }

    let timeZone = TimeZone(identifier: "UTC")!
    var clock = date(year: 2026, month: 10, day: 20, hour: 23, minute: 59, second: 50, timeZone: timeZone)
    let store = TranscriptStore(directory: base, now: { clock }, timeZone: timeZone)

    // 第一句原文先到，译文为空。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "")])
    let day1URL = store.fileURL(for: clock)
    // 硬约束 HC-P02：ingest 由主界面调用，不能同步等到磁盘写完再返回。
    check(
        !FileManager.default.fileExists(atPath: day1URL.path),
        "违反 HC-P02：ingest 返回时不该已经落盘（写盘必须异步合并，不能阻塞调用方）"
    )
    store.flush()
    check(FileManager.default.fileExists(atPath: day1URL.path), "flush 后应创建当天文件")

    var day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "第一天首句应只有 1 行（实际 \(day1Lines.count)）")
    check(day1Lines.first?.count == 4, "每行应为 4 个制表符字段（时间/原文/译文/去重键）")
    check(day1Lines.first?[0] == "23:59:50", "行首时间应为 HH:mm:ss（实际 \(day1Lines.first?[0] ?? "nil")）")
    check(day1Lines.first?[1] == "Guten Tag.", "原文列正确")
    check(day1Lines.first?[2] == "", "译文未到时第三列为空")
    check(day1Lines.first?[3] == "s1#1", "行尾去重键应为「会话代号#id」（实际 \(day1Lines.first?[3] ?? "nil")）")

    // Keychain 每 0.5 秒读一次，同一句会反复出现——不能重复写。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "")])
    store.flush()
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "同键重复轮询不应追加（实际 \(day1Lines.count) 行）")

    // 译文后到：更新同一行，而不是新增一行。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "你好")])
    store.flush()
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "译文后到不应新增行（实际 \(day1Lines.count) 行）")
    check(day1Lines.first?[2] == "你好", "译文应写回同一行（实际 \(day1Lines.first?[2] ?? "nil")）")
    check(day1Lines.first?[0] == "23:59:50", "译文更新不应改掉原时间")

    // 硬约束 HC-B10：重译写回后，最近句对的空译文回灌不能覆盖已保存的译文；
    // 但引擎随后给出的非空新译文仍应覆盖旧译文。
    let retryProtectionStore = TranscriptStore(
        directory: base.appendingPathComponent("retry-protection"),
        now: { clock },
        timeZone: timeZone
    )
    let retryProtectionURL = retryProtectionStore.fileURL(for: clock)
    retryProtectionStore.ingest([TranscriptStore.Sentence(id: 10, session: "hc-b10", src: "Guten Tag.", dst: "")])
    retryProtectionStore.flush()
    retryProtectionStore.updateTranslation(
        for: retryProtectionStore.lines(for: clock)[0], on: clock, dst: "你好"
    )
    retryProtectionStore.flush()
    retryProtectionStore.ingest([TranscriptStore.Sentence(id: 10, session: "hc-b10", src: "Guten Tag.", dst: "")])
    retryProtectionStore.flush()
    check(field(readLines(retryProtectionURL), 0, 2) == "你好", "违反 HC-B10：空译文回灌覆盖了已保存的译文")

    retryProtectionStore.ingest([TranscriptStore.Sentence(id: 10, session: "hc-b10", src: "Guten Tag.", dst: "您好")])
    retryProtectionStore.flush()
    check(field(readLines(retryProtectionURL), 0, 2) == "您好", "违反 HC-B10：非空新译文没有覆盖旧译文")

    // 换了一个引擎会话（同一天、同一个 id）：必须追加新行，不能覆盖上一句。
    // 各引擎的 id 都从 1 重新计数，只按 id 去重就会把这一句写到上一句头上。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s2", src: "Guten Abend.", dst: "")])
    store.flush()
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "不同会话的同 id 应追加新行（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 0, 1) == "Guten Tag.", "旧行不应被改写")
    check(field(day1Lines, 0, 2) == "你好", "旧行的译文应保留")
    check(field(day1Lines, 1, 1) == "Guten Abend.", "新行应是新会话的那句")
    check(field(day1Lines, 1, 3) == "s2#1", "新行的去重键应带新会话代号（实际 \(field(day1Lines, 1, 3))）")

    // 跨天：新句写到新日期文件。
    clock = date(year: 2026, month: 10, day: 21, hour: 0, minute: 0, second: 10, timeZone: timeZone)
    store.ingest([TranscriptStore.Sentence(id: 2, session: "s1", src: "Wie geht es?", dst: "你好吗？")])
    store.flush()
    let day2URL = store.fileURL(for: clock)
    check(day2URL.lastPathComponent == "2026-10-21.txt", "跨天文件名为 YYYY-MM-DD（实际 \(day2URL.lastPathComponent)）")
    check(FileManager.default.fileExists(atPath: day2URL.path), "跨天应创建新文件")
    check(readLines(day2URL).count == 1, "新一天的第一句应只有 1 行")

    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "跨天后前一天文件不应被改动（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 0, 2) == "你好", "跨天后前一天译文保持")

    // 跨天后上一句的迟到译文仍应更新前一天的同一行。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "您好")])
    store.flush()
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "跨天迟到译文不应新增行")
    check(field(day1Lines, 0, 2) == "您好", "跨天迟到译文应更新前一天的行")
    check(field(day1Lines, 1, 1) == "Guten Abend.", "跨天迟到译文不应动到另一会话的行")
    check(readLines(day2URL).count == 1, "跨天迟到译文不应污染新一天文件")

    // App 重启（换一个 store 实例读同一个目录）：磁盘上的行要能重建去重索引，
    // 否则重启后最近几句会被重复追加一遍。
    // 时钟拨回前一天，下面几组断言都针对第一天那个文件。
    clock = date(year: 2026, month: 10, day: 20, hour: 23, minute: 59, second: 55, timeZone: timeZone)
    let reopened = TranscriptStore(directory: base, now: { clock }, timeZone: timeZone)
    reopened.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "您好")])
    reopened.flush()
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "重启后同键句子不应被重复追加（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 0, 2) == "您好", "重启后同键句子应改写原行而不是新增")

    // 重启后换个会话的同 id 仍要追加新行（重建索引不能把会话维度弄丢）。
    reopened.ingest([TranscriptStore.Sentence(id: 1, session: "s3", src: "Guten Abend.", dst: "")])
    reopened.flush()
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 3, "重启后新会话的同 id 应追加新行（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 2, 3) == "s3#1", "重启后新行的去重键应带新会话代号（实际 \(field(day1Lines, 2, 3))）")

    // 老文件（只有三列、没有去重键）的行不参与去重：新句子只追加，不覆盖它。
    let legacyDir = base.appendingPathComponent("legacy")
    try? FileManager.default.createDirectory(at: legacyDir, withIntermediateDirectories: true)
    let legacyURL = legacyDir.appendingPathComponent("2026-10-20.txt")
    try? "12:00:00\tAlter Satz\t旧句子\n".write(to: legacyURL, atomically: true, encoding: .utf8)
    let legacyStore = TranscriptStore(directory: legacyDir, now: { clock }, timeZone: timeZone)
    legacyStore.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Neuer Satz", dst: "新句子")])
    legacyStore.flush()
    let legacyLines = readLines(legacyURL)
    check(legacyLines.count == 2, "老文件的行不应被覆盖，应追加新行（实际 \(legacyLines.count) 行）")
    check(field(legacyLines, 0, 1) == "Alter Satz", "老行应原样保留")
    check(field(legacyLines, 0, 3) == "", "老行没有去重键（第四列为空）")
    check(field(legacyLines, 1, 1) == "Neuer Satz", "新行应是新句子")
    check(field(legacyLines, 1, 3) == "s1#1", "新行应带去重键（实际 \(field(legacyLines, 1, 3))）")

    // 硬约束 HC-P02：合并写不能丢句。原文先到、译文后到，窗口里只落一次盘，
    // 但最终文件必须一行不少、译文是最后一版。
    let asyncDir = base.appendingPathComponent("async")
    let asyncStore = TranscriptStore(directory: asyncDir, now: { clock }, timeZone: timeZone)
    let asyncURL = asyncStore.fileURL(for: clock)
    for id in 1...50 {
        asyncStore.ingest([TranscriptStore.Sentence(id: id, session: "batch", src: "Satz \(id)", dst: "")])
    }
    check(
        !FileManager.default.fileExists(atPath: asyncURL.path),
        "违反 HC-P02：连续 ingest 期间不该每次落盘（合并窗口内只写一次）"
    )
    asyncStore.ingest([TranscriptStore.Sentence(id: 50, session: "batch", src: "Satz 50", dst: "第五十句")])
    asyncStore.flush()
    let asyncLines = readLines(asyncURL)
    check(
        asyncLines.count == 50,
        "违反 HC-P02：合并写丢了句子（期望 50 行，实际 \(asyncLines.count)）"
    )
    check(
        field(asyncLines, 49, 2) == "第五十句",
        "违反 HC-P02：合并写丢了迟到的译文（实际 \(field(asyncLines, 49, 2))）"
    )

    // 「今天的字幕」页面读的是同一个文件：行数与顺序要和写出去的一致。
    let pageLines = reopened.lines(for: clock)
    check(pageLines.count == 3, "回看页面应读出 3 行（实际 \(pageLines.count)）")
    check(pageLines.first?.time == "23:59:50", "回看页面首行时间应与文件一致")
    check(pageLines.last?.src == "Guten Abend.", "回看页面末行原文应与文件一致")

    // 搜索过滤：空查询原样返回，大小写/变音符号不敏感，任一列命中都算。
    let filterLines = [
        TranscriptStore.Line(id: 0, time: "10:00:00", src: "Guten Tag.", dst: "你好"),
        TranscriptStore.Line(id: 1, time: "10:00:05", src: "Schöne Grüße", dst: ""),
        TranscriptStore.Line(id: 2, time: "10:00:09", src: "Äpfel und Birnen", dst: "苹果和梨"),
    ]
    check(TranscriptStore.filter(filterLines, query: "").count == 3, "空查询应原样返回全部行")
    check(TranscriptStore.filter(filterLines, query: "   ").count == 3, "空白查询应原样返回全部行")
    check(TranscriptStore.filter(filterLines, query: "guten").map(\.id) == [0], "搜索应不区分大小写")
    check(TranscriptStore.filter(filterLines, query: "schone").map(\.id) == [1], "搜索应忽略变音符号 ö→o")
    check(TranscriptStore.filter(filterLines, query: "apfel").map(\.id) == [2], "搜索应忽略变音符号 ä→a")
    check(TranscriptStore.filter(filterLines, query: "你好").map(\.id) == [0], "只命中译文也算匹配")
    check(TranscriptStore.filter(filterLines, query: "xyz").isEmpty, "没有命中应返回空数组")

    // 复制全部：时间 + 原文，译文另起一行；没有译文只留原文；句间空一行。
    let expectedPlainText = """
    10:00:00  Guten Tag.
    你好

    10:00:05  Schöne Grüße

    10:00:09  Äpfel und Birnen
    苹果和梨
    """
    check(
        TranscriptStore.plainText(filterLines) == expectedPlainText,
        "plainText 的格式或空译文的省略不对（实际 \(TranscriptStore.plainText(filterLines))）"
    )
    check(TranscriptStore.plainText([]) == "", "没有行时 plainText 应为空串")

    // 按句更新译文（回看页重新翻译落盘）：覆盖重名同秒句、重新 load、其他句不变、找不到时不改文件。
    let retryDir = base.appendingPathComponent("retry-update")
    try? FileManager.default.createDirectory(at: retryDir, withIntermediateDirectories: true)
    let retryDate = date(year: 2026, month: 10, day: 22, hour: 12, minute: 0, second: 0, timeZone: timeZone)
    let retryStore = TranscriptStore(directory: retryDir, now: { retryDate }, timeZone: timeZone)
    let retryURL = retryStore.fileURL(for: retryDate)
    let initialContent = [
        "12:00:00\tHallo.\t\ts1#1",
        "12:00:00\tHallo.\t\ts1#2",
        "12:00:01\tDanke.\t谢谢\ts1#3",
    ].joined(separator: "\n") + "\n"
    try? initialContent.write(to: retryURL, atomically: true, encoding: .utf8)

    let loadedBefore = retryStore.lines(for: retryDate)
    check(loadedBefore.count == 3, "重名测试应读出 3 行（实际 \(loadedBefore.count)）")
    if loadedBefore.count == 3 {
        // 同时间同原文的两句，只更新第二句（id=1）。
        retryStore.updateTranslation(for: loadedBefore[1], on: retryDate, dst: "你好（第二句）")
        retryStore.flush()

        let reloadedStore = TranscriptStore(directory: retryDir, now: { retryDate }, timeZone: timeZone)
        let loadedAfter = reloadedStore.lines(for: retryDate)
        check(loadedAfter.count == 3, "按句更新后行数应不变（实际 \(loadedAfter.count)）")
        check(loadedAfter[0].dst == "", "重名第一句不应被改写（实际 \(loadedAfter[0].dst)）")
        check(loadedAfter[1].dst == "你好（第二句）", "重名第二句应读到更新后的译文（实际 \(loadedAfter[1].dst)）")
        check(loadedAfter[2].dst == "谢谢", "其他已翻译句子应保持不变（实际 \(loadedAfter[2].dst)）")

        let fileLinesAfter = readLines(retryURL)
        check(field(fileLinesAfter, 1, 3) == "s1#2", "更新译文应保留原来的去重键（实际 \(field(fileLinesAfter, 1, 3))）")

        // 找不到该句（越界 id / 错时间 / 错原文 / 不存在的日期）时不改任何文件。
        let beforeMissingBytes = try? Data(contentsOf: retryURL)
        let missingByID = TranscriptStore.Line(id: 99, time: "12:00:00", src: "Hallo.", dst: "")
        let missingByTime = TranscriptStore.Line(id: 0, time: "12:00:59", src: "Hallo.", dst: "")
        let missingBySrc = TranscriptStore.Line(id: 0, time: "12:00:00", src: "Anderer Satz", dst: "")
        let otherDate = date(year: 2026, month: 10, day: 23, hour: 12, minute: 0, second: 0, timeZone: timeZone)

        let missingResults = LockedResults()
        let missingGroup = DispatchGroup()
        for (targetLine, targetDay) in [
            (missingByID, retryDate),
            (missingByTime, retryDate),
            (missingBySrc, retryDate),
            (loadedBefore[0], otherDate),
        ] {
            missingGroup.enter()
            reloadedStore.updateTranslation(for: targetLine, on: targetDay, dst: "不应写入") { ok in
                missingResults.append(ok)
                missingGroup.leave()
            }
        }
        missingGroup.wait()
        reloadedStore.flush()

        let recordedResults = missingResults.snapshot()
        check(recordedResults == [false, false, false, false], "找不到该句时应全部返回 false（实际 \(recordedResults)）")
        let afterMissingBytes = try? Data(contentsOf: retryURL)
        check(beforeMissingBytes == afterMissingBytes, "找不到该句时原存档文件字节不应被改动")
        check(
            !FileManager.default.fileExists(atPath: reloadedStore.fileURL(for: otherDate).path),
            "找不到该句时不应为不存在的日期创建文件"
        )
    }
}

private func date(
    year: Int, month: Int, day: Int, hour: Int, minute: Int, second: Int, timeZone: TimeZone
) -> Date {
    var calendar = Calendar(identifier: .gregorian)
    calendar.timeZone = timeZone
    return calendar.date(from: DateComponents(
        year: year, month: month, day: day, hour: hour, minute: minute, second: second
    ))!
}

private func readLines(_ url: URL) -> [[String]] {
    guard let data = try? Data(contentsOf: url),
          let text = String(data: data, encoding: .utf8) else {
        return []
    }
    return text
        .split(separator: "\n", omittingEmptySubsequences: true)
        .map { $0.split(separator: "\t", omittingEmptySubsequences: false).map(String.init) }
}

/// 取第 row 行的第 column 列；越界返回空串（断言里好写，也不怕文件被截断）。
private func field(_ lines: [[String]], _ row: Int, _ column: Int) -> String {
    guard lines.indices.contains(row), lines[row].indices.contains(column) else { return "" }
    return lines[row][column]
}

private final class LockedResults: @unchecked Sendable {
    private let lock = NSLock()
    private var values: [Bool] = []

    func append(_ value: Bool) {
        lock.withLock { values.append(value) }
    }

    func snapshot() -> [Bool] {
        lock.withLock { values }
    }
}
