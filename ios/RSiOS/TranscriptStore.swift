import Foundation
import Combine
import os

/// 主 App 的当天字幕存档：`Documents/transcripts/YYYY-MM-DD.txt`，
/// 每行 `HH:mm:ss\t原文\t中文`。
///
/// 为什么要按句子 id 去重：主 App 每 0.5 秒从 Keychain 读一次最近句对，
/// 同一句会被反复看到。译文通常比原文晚到，所以同一 id 的第二次写入
/// **不是追加新行，而是重写那一行**（以最后一版为准）。
///
/// 实现方式：内存里按日期保存整天的行，任何一行变化就把当天文件整体原子重写。
/// 一天最多几千行，重写只在内容真的变化时发生（轮询读到相同句对什么都不做）。
/// 第一次碰某一天时先把磁盘上已有的行作为“不参与去重的旧行”读进来，
/// 这样 App 重启后不会把之前的内容覆盖掉。
final class TranscriptStore: ObservableObject {
    struct Sentence {
        let id: Int
        let src: String
        var dst: String
    }

    private struct Row {
        let id: Int?
        var time: String
        var src: String
        var dst: String
    }

    private let directory: URL
    private let now: () -> Date
    private let timeZone: TimeZone
    private let dateFormatter: DateFormatter
    private let timeFormatter: DateFormatter
    private let queue = DispatchQueue(label: "com.wyl2607.rsios.transcript-store")
    private let logger = Logger(subsystem: "com.wyl2607.rsios.spike", category: "transcript-store")

    private var days: [String: [Row]] = [:]
    private var index: [Int: (day: String, row: Int)] = [:]
    private var loadedDays: Set<String> = []

    init(
        directory: URL? = nil,
        now: @escaping () -> Date = Date.init,
        timeZone: TimeZone = .current
    ) {
        self.directory = directory ?? FileManager.default
            .urls(for: .documentDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("transcripts", isDirectory: true)
        self.now = now
        self.timeZone = timeZone

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

    /// 喂入 Keychain 里当前的最近句对。重复的 id 会被合并，译文后到会原地更新。
    func ingest(_ sentences: [Sentence]) {
        guard !sentences.isEmpty else { return }
        queue.sync {
            var dirtyDays: Set<String> = []
            let today = dateKey(for: now())

            for sentence in sentences {
                let src = sanitize(sentence.src)
                let dst = sanitize(sentence.dst)

                if let location = index[sentence.id] {
                    let row = days[location.day]![location.row]
                    // 同一天里同一 id 视为同一句（原文/译文都允许以最后一版为准）。
                    // 跨天时只有原文相同才当作迟到的译文；否则是扩展重启后 id 重新计数，
                    // 应当在新的一天里作为新句追加。
                    if location.day == today || row.src == src {
                        if row.src != src || row.dst != dst {
                            days[location.day]![location.row].src = src
                            days[location.day]![location.row].dst = dst
                            dirtyDays.insert(location.day)
                        }
                        continue
                    }
                }

                ensureLoaded(today)
                var rows = days[today] ?? []
                rows.append(Row(id: sentence.id, time: timeString(for: now()), src: src, dst: dst))
                days[today] = rows
                index[sentence.id] = (today, rows.count - 1)
                dirtyDays.insert(today)
            }

            for day in dirtyDays {
                write(day)
            }
        }
    }

    func fileURL(for date: Date) -> URL {
        directory.appendingPathComponent("\(dateKey(for: date)).txt")
    }

    func dateKey(for date: Date) -> String {
        dateFormatter.string(from: date)
    }

    func timeString(for date: Date) -> String {
        timeFormatter.string(from: date)
    }

    private func ensureLoaded(_ day: String) {
        guard !loadedDays.contains(day) else { return }
        loadedDays.insert(day)

        let url = directory.appendingPathComponent("\(day).txt")
        guard let data = try? Data(contentsOf: url),
              let text = String(data: data, encoding: .utf8) else {
            return
        }

        let rows: [Row] = text
            .split(separator: "\n", omittingEmptySubsequences: true)
            .map { line in
                let fields = line.split(separator: "\t", omittingEmptySubsequences: false).map(String.init)
                return Row(
                    id: nil,
                    time: fields.count > 0 ? fields[0] : "",
                    src: fields.count > 1 ? fields[1] : "",
                    dst: fields.count > 2 ? fields[2] : ""
                )
            }
        days[day] = rows
    }

    private func write(_ day: String) {
        guard let rows = days[day] else { return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let body = rows
                .map { "\($0.time)\t\($0.src)\t\($0.dst)" }
                .joined(separator: "\n") + (rows.isEmpty ? "" : "\n")
            let url = directory.appendingPathComponent("\(day).txt")
            try body.data(using: .utf8)?.write(to: url, options: .atomic)
        } catch {
            logger.error("Transcript write failed day=\(day, privacy: .public) error=\(error.localizedDescription, privacy: .public)")
        }
    }

    /// 一行一条记录，制表符和换行会把格式撑坏，统一压成空格。
    private func sanitize(_ text: String) -> String {
        text
            .replacingOccurrences(of: "\t", with: " ")
            .replacingOccurrences(of: "\r", with: " ")
            .replacingOccurrences(of: "\n", with: " ")
    }
}
