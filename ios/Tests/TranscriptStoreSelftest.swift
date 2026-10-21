import Foundation

/// 当天字幕存档自测：跨天换文件、同 id 去重、译文后到原地更新。
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
    store.ingest([TranscriptStore.Sentence(id: 1, src: "Guten Tag.", dst: "")])
    let day1URL = store.fileURL(for: clock)
    check(FileManager.default.fileExists(atPath: day1URL.path), "第一次写入应创建当天文件")

    var day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "第一天首句应只有 1 行（实际 \(day1Lines.count)）")
    check(day1Lines.first?.count == 3, "每行应为 3 个制表符字段")
    check(day1Lines.first?[0] == "23:59:50", "行首时间应为 HH:mm:ss（实际 \(day1Lines.first?[0] ?? "nil")）")
    check(day1Lines.first?[1] == "Guten Tag.", "原文列正确")
    check(day1Lines.first?[2] == "", "译文未到时第三列为空")

    // Keychain 每 0.5 秒读一次，同一句会反复出现——不能重复写。
    store.ingest([TranscriptStore.Sentence(id: 1, src: "Guten Tag.", dst: "")])
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "同 id 重复轮询不应追加（实际 \(day1Lines.count) 行）")

    // 译文后到：更新同一行，而不是新增一行。
    store.ingest([TranscriptStore.Sentence(id: 1, src: "Guten Tag.", dst: "你好")])
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "译文后到不应新增行（实际 \(day1Lines.count) 行）")
    check(day1Lines.first?[2] == "你好", "译文应写回同一行（实际 \(day1Lines.first?[2] ?? "nil")）")
    check(day1Lines.first?[0] == "23:59:50", "译文更新不应改掉原时间")

    // 跨天：新句写到新日期文件。
    clock = date(year: 2026, month: 10, day: 21, hour: 0, minute: 0, second: 10, timeZone: timeZone)
    store.ingest([TranscriptStore.Sentence(id: 2, src: "Wie geht es?", dst: "你好吗？")])
    let day2URL = store.fileURL(for: clock)
    check(day2URL.lastPathComponent == "2026-10-21.txt", "跨天文件名为 YYYY-MM-DD（实际 \(day2URL.lastPathComponent)）")
    check(FileManager.default.fileExists(atPath: day2URL.path), "跨天应创建新文件")
    check(readLines(day2URL).count == 1, "新一天的第一句应只有 1 行")

    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "跨天后前一天文件不应被改动（实际 \(day1Lines.count) 行）")
    check(day1Lines.first?[2] == "你好", "跨天后前一天译文保持")

    // 跨天后上一句的迟到译文仍应更新前一天的同 id 行。
    store.ingest([TranscriptStore.Sentence(id: 1, src: "Guten Tag.", dst: "您好")])
    day1Lines = readLines(day1URL)
    check(day1Lines.count == 1, "跨天迟到译文不应新增行")
    check(day1Lines.first?[2] == "您好", "跨天迟到译文应更新前一天的行")
    check(readLines(day2URL).count == 1, "跨天迟到译文不应污染新一天文件")
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
