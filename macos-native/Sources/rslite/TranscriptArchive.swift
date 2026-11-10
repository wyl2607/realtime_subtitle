import Foundation
import os

/// rslite 的当天字幕存档。每行格式与 iOS 的 TranscriptStore 一致：
/// `HH:mm:ss\t原文\t译文\t会话#行键`。
final class TranscriptArchive {
    struct Line: Equatable {
        let time: String
        let src: String
        let dst: String
    }

    private struct Row {
        let key: String?
        var time: String
        var src: String
        var dst: String
    }

    private static let processSession = "\(String(UInt32.random(in: .min ... .max), radix: 16))-\(Int(Date().timeIntervalSince1970))"

    private let directory: URL
    private let session: String
    private let now: () -> Date
    private let dateFormatter: DateFormatter
    private let timeFormatter: DateFormatter
    private let queue = DispatchQueue(label: "com.wyl2607.rslite.transcript-archive")
    private let logger = Logger(subsystem: "com.wyl2607.rslite", category: "transcript-archive")

    private var days: [String: [Row]] = [:]
    private var index: [String: (day: String, row: Int)] = [:]
    private var loadedDays: Set<String> = []
    private var writeCount = 0

    init(
        directory: URL? = nil,
        session: String? = nil,
        now: @escaping () -> Date = Date.init,
        timeZone: TimeZone = .current
    ) {
        self.directory = directory ?? Self.defaultDirectory()
        self.session = session ?? Self.processSession
        self.now = now

        let dateFormatter = DateFormatter()
        dateFormatter.locale = Locale(identifier: "en_US_POSIX")
        dateFormatter.timeZone = timeZone
        dateFormatter.dateFormat = "yyyy-MM-dd"
        self.dateFormatter = dateFormatter

        let timeFormatter = DateFormatter()
        timeFormatter.locale = Locale(identifier: "en_US_POSIX")
        timeFormatter.timeZone = timeZone
        timeFormatter.dateFormat = "HH:mm:ss"
        self.timeFormatter = timeFormatter
    }

    static func defaultDirectory() -> URL {
        let configDirectory = ProcessInfo.processInfo.environment["RSLITE_CONFIG_DIR"]
            ?? ("~/.config/rslite" as NSString).expandingTildeInPath
        return URL(fileURLWithPath: configDirectory, isDirectory: true)
            .appendingPathComponent("transcripts", isDirectory: true)
    }

    func upsert(key: Int, src: String, dst: String?) {
        self.queue.sync {
            let timestamp = self.now()
            let today = self.dateKey(for: timestamp)
            self.ensureLoaded(today)
            let archiveKey = self.archiveKey(for: key)
            let sanitizedSource = self.sanitize(src)
            let sanitizedTranslation = self.sanitize(dst ?? "")

            if let location = self.index[archiveKey] {
                let row = self.days[location.day]![location.row]
                guard row.src != sanitizedSource || row.dst != sanitizedTranslation else { return }
                self.days[location.day]![location.row].src = sanitizedSource
                self.days[location.day]![location.row].dst = sanitizedTranslation
                self.write(location.day)
                return
            }

            var rows = self.days[today] ?? []
            rows.append(Row(
                key: archiveKey,
                time: self.timeString(for: timestamp),
                src: sanitizedSource,
                dst: sanitizedTranslation
            ))
            self.days[today] = rows
            self.index[archiveKey] = (today, rows.count - 1)
            self.write(today)
        }
    }

    func remove(keys: [Int]) {
        guard !keys.isEmpty else { return }
        self.queue.sync {
            self.ensureLoaded(self.dateKey(for: self.now()))
            let archiveKeys = Set(keys.map { self.archiveKey(for: $0) })
            var dirtyDays: Set<String> = []

            for day in self.loadedDays {
                guard let rows = self.days[day] else { continue }
                let kept = rows.filter { row in
                    guard let key = row.key else { return true }
                    return !archiveKeys.contains(key)
                }
                guard kept.count != rows.count else { continue }
                self.days[day] = kept
                dirtyDays.insert(day)
            }

            for day in dirtyDays {
                self.rebuildIndex(for: day)
                self.write(day)
            }
        }
    }

    func lines(for date: Date) -> [Line] {
        self.queue.sync {
            let day = self.dateKey(for: date)
            self.ensureLoaded(day)
            return (self.days[day] ?? []).map { row in
                Line(time: row.time, src: row.src, dst: row.dst)
            }
        }
    }

    func fileURL(for date: Date) -> URL {
        self.queue.sync {
            self.directory.appendingPathComponent("\(self.dateKey(for: date)).txt")
        }
    }

    static func filter(_ lines: [Line], query: String) -> [Line] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return lines }
        return lines.filter { line in
            line.src.range(of: trimmed, options: [.caseInsensitive, .diacriticInsensitive]) != nil
                || line.dst.range(of: trimmed, options: [.caseInsensitive, .diacriticInsensitive]) != nil
        }
    }

    static func plainText(_ lines: [Line]) -> String {
        lines.map { line in
            var text = "\(line.time)  \(line.src)"
            if !line.dst.isEmpty {
                text += "\n\(line.dst)"
            }
            return text
        }.joined(separator: "\n\n")
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ condition: Bool, _ description: String) {
            if !condition { failures.append(description) }
        }

        let fileManager = FileManager.default
        let directory = fileManager.temporaryDirectory
            .appendingPathComponent("rslite-transcript-\(UUID().uuidString)", isDirectory: true)
        defer { try? fileManager.removeItem(at: directory) }

        let timeZone = TimeZone(secondsFromGMT: 0)!
        let yesterday = Date(timeIntervalSince1970: 1_699_948_800)
        let today = Date(timeIntervalSince1970: 1_700_035_200)
        let clock = TranscriptArchiveTestClock(yesterday)
        let archive = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)

        archive.upsert(key: 7, src: "Guten Morgen", dst: nil)
        let yesterdayURL = archive.fileURL(for: yesterday)
        let firstFields = (try? String(contentsOf: yesterdayURL, encoding: .utf8))?
            .split(separator: "\n", omittingEmptySubsequences: true)
            .first?
            .split(separator: "\t", omittingEmptySubsequences: false)
            .map(String.init) ?? []
        check(firstFields.count == 4, "新行应有四列")
        check(firstFields.dropFirst(2).first == "", "nil 译文应写入空第三列")
        check(firstFields.last == "session-a#7", "第四列应为会话去重键")

        let writesBeforeSameUpsert = archive.writesForTesting()
        archive.upsert(key: 7, src: "Guten Morgen", dst: nil)
        check(archive.writesForTesting() == writesBeforeSameUpsert, "内容相同的 upsert 不应重写文件")
        archive.upsert(key: 7, src: "Guten Morgen", dst: "早上好")
        let updated = archive.lines(for: yesterday)
        check(updated.count == 1 && updated.first?.dst == "早上好", "同 key 补译文应原地更新")

        let otherSession = TranscriptArchive(directory: directory, session: "session-b", now: { clock.date }, timeZone: timeZone)
        otherSession.upsert(key: 7, src: "Andere Zeile", dst: nil)
        let remover = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)
        remover.remove(keys: [7])
        let afterRemove = TranscriptArchive(directory: directory, session: "check", now: { clock.date }, timeZone: timeZone)
            .lines(for: yesterday)
        check(afterRemove.count == 1 && afterRemove.first?.src == "Andere Zeile", "remove 不应删除其他会话同 key")

        let sessionA = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)
        sessionA.upsert(key: 8, src: "Erste Fassung", dst: nil)
        let sameSession = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)
        let countBeforeReload = sameSession.lines(for: yesterday).count
        sameSession.upsert(key: 8, src: "Neue Fassung", dst: "新版")
        let afterReload = sameSession.lines(for: yesterday)
        check(
            afterReload.count == countBeforeReload && afterReload.filter { $0.src == "Neue Fassung" }.count == 1,
            "同会话重建索引后应更新原行而不追加"
        )
        let differentSession = TranscriptArchive(directory: directory, session: "session-c", now: { clock.date }, timeZone: timeZone)
        let countBeforeOtherSession = differentSession.lines(for: yesterday).count
        differentSession.upsert(key: 8, src: "Neue Sitzung", dst: nil)
        let afterOtherSession = differentSession.lines(for: yesterday)
        check(
            afterOtherSession.count == countBeforeOtherSession + 1 && afterOtherSession.filter { $0.src == "Neue Sitzung" }.count == 1,
            "不同会话同 key 应追加"
        )

        let legacyDay = "2023-11-15"
        do {
            try fileManager.createDirectory(at: directory, withIntermediateDirectories: true)
            try Data("09:10:11\tAlte Zeile\t旧译文\n".utf8).write(
                to: directory.appendingPathComponent("\(legacyDay).txt"), options: .atomic
            )
        } catch {
            failures.append("准备旧三列文件失败")
        }
        let legacyDate = today
        let legacy = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)
        let legacyLines = legacy.lines(for: legacyDate)
        legacy.remove(keys: [99])
        check(legacyLines == [Line(time: "09:10:11", src: "Alte Zeile", dst: "旧译文")], "旧三列行应被读回")
        check(legacy.lines(for: legacyDate) == legacyLines, "旧三列行不应参与 remove")
        clock.date = legacyDate
        legacy.upsert(key: 99, src: "Neue Zeile", dst: nil)
        legacy.remove(keys: [99])
        check(legacy.lines(for: legacyDate) == legacyLines, "旧三列行不应参与去重且应在删除新行后保留")

        clock.date = yesterday
        let sanitizingArchive = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)
        sanitizingArchive.upsert(key: 10, src: "A\tB\nC", dst: "甲\r乙\t丙")
        let sanitized = sanitizingArchive.lines(for: yesterday).first { $0.src == "A B C" }
        check(sanitized?.dst == "甲 乙 丙", "制表符和换行应替换为空格")

        clock.date = today
        let dayArchive = TranscriptArchive(directory: directory, session: "session-a", now: { clock.date }, timeZone: timeZone)
        dayArchive.upsert(key: 20, src: "Gestern", dst: nil)
        clock.date = Date(timeIntervalSince1970: today.timeIntervalSince1970 + 86_400)
        dayArchive.upsert(key: 20, src: "Gestern neu", dst: "昨天更新")
        dayArchive.upsert(key: 21, src: "Heute", dst: nil)
        check(dayArchive.lines(for: today).contains { $0.src == "Gestern neu" }, "昨天的 key 今天更新应写回昨天文件")
        check(dayArchive.lines(for: clock.date).contains { $0.src == "Heute" }, "今天的新 key 应写入今天文件")

        let searchable = [
            Line(time: "10:00:00", src: "Über den Fluss", dst: "越过河流"),
            Line(time: "10:01:00", src: "Guten Tag", dst: "你好世界"),
        ]
        check(Self.filter(searchable, query: "  ") == searchable, "空搜索应原样返回")
        check(Self.filter(searchable, query: "uber").count == 1, "搜索应忽略变音符")
        check(Self.filter(searchable, query: "世界").count == 1, "搜索应命中译文")
        check(
            Self.plainText(searchable) == "10:00:00  Über den Fluss\n越过河流\n\n10:01:00  Guten Tag\n你好世界"
                && Self.plainText([Line(time: "11:00:00", src: "Nur Quelle", dst: "")]) == "11:00:00  Nur Quelle",
            "plainText 格式应与 iOS 一致"
        )

        return failures
    }

    private func archiveKey(for key: Int) -> String {
        "\(self.session)#\(key)"
    }

    private func dateKey(for date: Date) -> String {
        self.dateFormatter.string(from: date)
    }

    private func timeString(for date: Date) -> String {
        self.timeFormatter.string(from: date)
    }

    private func ensureLoaded(_ day: String) {
        guard !self.loadedDays.contains(day) else { return }
        self.loadedDays.insert(day)

        let url = self.directory.appendingPathComponent("\(day).txt")
        guard let data = try? Data(contentsOf: url), let text = String(data: data, encoding: .utf8) else {
            return
        }

        let rows = text.split(separator: "\n", omittingEmptySubsequences: true).map { line -> Row in
            let fields = line.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
            let key = fields.count > 3 && !fields[3].isEmpty ? fields[3] : nil
            return Row(
                key: key,
                time: fields.indices.contains(0) ? fields[0] : "",
                src: fields.indices.contains(1) ? fields[1] : "",
                dst: fields.indices.contains(2) ? fields[2] : ""
            )
        }
        self.days[day] = rows
        self.rebuildIndex(for: day)
    }

    private func rebuildIndex(for day: String) {
        self.index = self.index.filter { $0.value.day != day }
        for (offset, row) in (self.days[day] ?? []).enumerated() {
            if let key = row.key {
                self.index[key] = (day, offset)
            }
        }
    }

    private func write(_ day: String) {
        guard let rows = self.days[day] else { return }
        do {
            try FileManager.default.createDirectory(at: self.directory, withIntermediateDirectories: true)
            let body = rows.map { row in
                "\(row.time)\t\(row.src)\t\(row.dst)\t\(row.key ?? "")"
            }.joined(separator: "\n") + (rows.isEmpty ? "" : "\n")
            try Data(body.utf8).write(to: self.directory.appendingPathComponent("\(day).txt"), options: .atomic)
            self.writeCount += 1
        } catch {
            self.logger.error("Transcript write failed day=\(day, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }

    private func sanitize(_ text: String) -> String {
        text
            .replacingOccurrences(of: "\t", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
    }

    private func writesForTesting() -> Int {
        self.queue.sync { self.writeCount }
    }
}

private final class TranscriptArchiveTestClock {
    var date: Date

    init(_ date: Date) {
        self.date = date
    }
}
