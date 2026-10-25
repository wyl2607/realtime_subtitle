import Foundation
import Combine
import os

/// 主 App 的当天字幕存档：`Documents/transcripts/YYYY-MM-DD.txt`，
/// 每行 `HH:mm:ss\t原文\t中文\t去重键`。
///
/// 为什么要按句子去重：主 App 每 0.5 秒从 Keychain 读一次最近句对，
/// 同一句会被反复看到。译文通常比原文晚到，所以同一句的第二次写入
/// **不是追加新行，而是重写那一行**（以最后一版为准）。
///
/// 去重键为什么是「会话代号 + 句子 id」：本机引擎每次启动都从 id=1 重新计数，
/// 节点会话的 id 也按会话重新计数。只按 id 去重的话，后一个会话的第 1 句
/// 会把前一个会话的第 1 句覆盖掉——用户看到的是字幕被改写，而不是追加。
/// 会话代号由广播扩展在每次开新引擎会话时生成，随每行一起传回主 App。
///
/// 键为什么写在行尾而不是另存一份索引文件：
/// - 存档文件本身就是完整的索引，重启后读回文件就能重建去重表；
/// - 只有一份要落盘的文件，就不会有「索引写了、文件没写」这种半截状态；
/// - 「文件」App 里看到的就是一个纯文本，不用再解释另一个文件是什么。
/// 老文件（只有三列）的行没有键，不参与去重：新句子只会追加，不会覆盖它们。
///
/// 实现方式：内存里按日期保存整天的行，任何一行变化就把当天文件整体原子重写。
/// 一天最多几千行，重写只在内容真的变化时发生（轮询读到相同句对什么都不做）。
final class TranscriptStore: ObservableObject {
    struct Sentence {
        let id: Int
        /// 产生这一句的引擎会话代号：各引擎的 id 都从 1 重新计数，只靠 id 会覆盖旧句。
        let session: String
        let src: String
        var dst: String
    }

    /// 存档里的一行，给「今天的字幕」页面显示。
    struct Line: Identifiable {
        let id: Int
        let time: String
        let src: String
        let dst: String
    }

    private struct Row {
        /// 去重键；nil = 老文件里的行（没有键，永远不参与去重）。
        let key: String?
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
    private var index: [String: (day: String, row: Int)] = [:]
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

    /// 去重键：会话代号 + 句子 id。跨会话的同 id 不会互相覆盖。
    static func makeKey(session: String, id: Int) -> String {
        "\(session)#\(id)"
    }

    /// 喂入 Keychain 里当前的最近句对。同一句（同键）会被合并，译文后到会原地更新。
    func ingest(_ sentences: [Sentence]) {
        guard !sentences.isEmpty else { return }
        queue.sync {
            var dirtyDays: Set<String> = []
            let today = dateKey(for: now())
            // 先把当天文件读回来并重建去重索引：App 重启后内存里是空的，
            // 不先读回就会把最近几句当成新句子再追加一遍。
            ensureLoaded(today)

            for sentence in sentences {
                let src = sanitize(sentence.src)
                let dst = sanitize(sentence.dst)
                let key = Self.makeKey(session: sentence.session, id: sentence.id)

                if let location = index[key] {
                    let row = days[location.day]![location.row]
                    // 同一句（原文/译文都允许以最后一版为准）。
                    if row.src != src || row.dst != dst {
                        days[location.day]![location.row].src = src
                        days[location.day]![location.row].dst = dst
                        dirtyDays.insert(location.day)
                    }
                    continue
                }

                var rows = days[today] ?? []
                rows.append(Row(key: key, time: timeString(for: now()), src: src, dst: dst))
                days[today] = rows
                index[key] = (today, rows.count - 1)
                dirtyDays.insert(today)
            }

            for day in dirtyDays {
                write(day)
            }
        }
    }

    /// 某一天存档里的所有行（从磁盘读回当天文件后给出），按写入顺序。
    func lines(for date: Date) -> [Line] {
        queue.sync {
            let day = dateKey(for: date)
            ensureLoaded(day)
            let rows = days[day] ?? []
            return rows.enumerated().map { offset, row in
                Line(id: offset, time: row.time, src: row.src, dst: row.dst)
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
                // 第四列是去重键；老文件只有三列 → 没有键 → 不参与去重。
                let rawKey = fields.count > 3 ? fields[3] : ""
                return Row(
                    key: rawKey.isEmpty ? nil : rawKey,
                    time: fields.count > 0 ? fields[0] : "",
                    src: fields.count > 1 ? fields[1] : "",
                    dst: fields.count > 2 ? fields[2] : ""
                )
            }
        days[day] = rows

        // 重建去重索引：文件行尾带着键，读回来就能知道哪一行是哪一句。
        for (offset, row) in rows.enumerated() {
            if let key = row.key {
                index[key] = (day, offset)
            }
        }
    }

    private func write(_ day: String) {
        guard let rows = days[day] else { return }
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true)
            let body = rows
                .map { row in
                    // 第四列是去重键；老行没有键就留空（不参与去重）。
                    "\(row.time)\t\(row.src)\t\(row.dst)\t\(row.key ?? "")"
                }
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
