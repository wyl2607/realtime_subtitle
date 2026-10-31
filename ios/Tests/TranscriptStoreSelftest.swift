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
    check(FileManager.default.fileExists(atPath: day1URL.path), "第一次写入应创建当天文件")

    var day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "第一天首句应只有 1 行（实际 \(day1Lines.count)）")
    check(day1Lines.first?.count == 4, "每行应为 4 个制表符字段（时间/原文/译文/去重键）")
    check(day1Lines.first?[0] == "23:59:50", "行首时间应为 HH:mm:ss（实际 \(day1Lines.first?[0] ?? "nil")）")
    check(day1Lines.first?[1] == "Guten Tag.", "原文列正确")
    check(day1Lines.first?[2] == "", "译文未到时第三列为空")
    check(day1Lines.first?[3] == "s1#1", "行尾去重键应为「会话代号#id」（实际 \(day1Lines.first?[3] ?? "nil")）")

    // Keychain 每 0.5 秒读一次，同一句会反复出现——不能重复写。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "")])
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "同键重复轮询不应追加（实际 \(day1Lines.count) 行）")

    // 译文后到：更新同一行，而不是新增一行。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "你好")])
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "译文后到不应新增行（实际 \(day1Lines.count) 行）")
    check(day1Lines.first?[2] == "你好", "译文应写回同一行（实际 \(day1Lines.first?[2] ?? "nil")）")
    check(day1Lines.first?[0] == "23:59:50", "译文更新不应改掉原时间")

    // 换了一个引擎会话（同一天、同一个 id）：必须追加新行，不能覆盖上一句。
    // 各引擎的 id 都从 1 重新计数，只按 id 去重就会把这一句写到上一句头上。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s2", src: "Guten Abend.", dst: "")])
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "不同会话的同 id 应追加新行（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 0, 1) == "Guten Tag.", "旧行不应被改写")
    check(field(day1Lines, 0, 2) == "你好", "旧行的译文应保留")
    check(field(day1Lines, 1, 1) == "Guten Abend.", "新行应是新会话的那句")
    check(field(day1Lines, 1, 3) == "s2#1", "新行的去重键应带新会话代号（实际 \(field(day1Lines, 1, 3))）")

    // 跨天：新句写到新日期文件。
    clock = date(year: 2026, month: 10, day: 21, hour: 0, minute: 0, second: 10, timeZone: timeZone)
    store.ingest([TranscriptStore.Sentence(id: 2, session: "s1", src: "Wie geht es?", dst: "你好吗？")])
    let day2URL = store.fileURL(for: clock)
    check(day2URL.lastPathComponent == "2026-10-21.txt", "跨天文件名为 YYYY-MM-DD（实际 \(day2URL.lastPathComponent)）")
    check(FileManager.default.fileExists(atPath: day2URL.path), "跨天应创建新文件")
    check(readLines(day2URL).count == 1, "新一天的第一句应只有 1 行")

    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "跨天后前一天文件不应被改动（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 0, 2) == "你好", "跨天后前一天译文保持")

    // 跨天后上一句的迟到译文仍应更新前一天的同一行。
    store.ingest([TranscriptStore.Sentence(id: 1, session: "s1", src: "Guten Tag.", dst: "您好")])
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
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 2, "重启后同键句子不应被重复追加（实际 \(day1Lines.count) 行）")
    check(field(day1Lines, 0, 2) == "您好", "重启后同键句子应改写原行而不是新增")

    // 重启后换个会话的同 id 仍要追加新行（重建索引不能把会话维度弄丢）。
    reopened.ingest([TranscriptStore.Sentence(id: 1, session: "s3", src: "Guten Abend.", dst: "")])
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
    let legacyLines = readLines(legacyURL)
    check(legacyLines.count == 2, "老文件的行不应被覆盖，应追加新行（实际 \(legacyLines.count) 行）")
    check(field(legacyLines, 0, 1) == "Alter Satz", "老行应原样保留")
    check(field(legacyLines, 0, 3) == "", "老行没有去重键（第四列为空）")
    check(field(legacyLines, 1, 1) == "Neuer Satz", "新行应是新句子")
    check(field(legacyLines, 1, 3) == "s1#1", "新行应带去重键（实际 \(field(legacyLines, 1, 3))）")

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
