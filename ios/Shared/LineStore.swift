import Foundation

/// 一行的来源。节点行带节点标识（云端是 `cloud:groq`），本机行不带。
/// 与 mac rslite `LineSource` 同语义，夹具里的 `source` 字符串由自测负责映射
/// （`.node` → `remote`）。
enum LineSource: Equatable {
    case local
    case node(String)
}

struct SubtitleLine: Equatable {
    /// 行的唯一键：本机行用识别引擎的句子 id，节点行由 LineStore 分配（负数，避免撞车）。
    var key: Int
    var t0: Double?
    var t1: Double?
    var source: LineSource
    var srcText: String
    var dstText: String?
}

/// 字幕历史（RFC P5 合并）。纯逻辑，不碰界面，方便脱离 UIKit 用 swiftc 自测。
/// 界面只显示末尾几行；已经滚出屏幕的行也留在这里（P5：只更新内存里的历史，不回滚画面）。
/// 移植自 `macos-native/Sources/rslite/SentenceCommitter.swift` 的 `LineStore`，
/// 常量、边界、插入顺序逐条对齐——规则一变四端就不一致了。
struct LineStore {
    private(set) var lines: [SubtitleLine] = []
    private var nextNodeKey = -1
    private let capacity: Int

    /// 瀑布流和回看最多画这么多句对。P5 替换仍看 `capacity`（默认 200）。
    static let displayHistoryLimit = 60
    /// 重叠占本机行自身时长的比例达到这个值就整行替换（含等于）。
    static let replaceRatio = 0.5
    private static let epsilon = 1e-9

    init(capacity: Int = 200) {
        self.capacity = capacity
    }

    mutating func addLocal(key: Int, t0: Double?, t1: Double?, text: String) {
        let line = SubtitleLine(key: key, t0: t0, t1: t1, source: .local, srcText: text, dstText: nil)
        // 节点行先到、同一时段的本机行后到：与 P5 同口径（重叠 ≥ 本机行自身时长 50%）判定已被覆盖。
        // 本机行直接丢弃——LineStore 没有独立的历史区，节点行已经是这一时段的权威文本，
        // 追加会造成顺序颠倒且同一句显示两遍（applyNode 的替换只发生在节点行到达那一刻）。
        // 之后对该 key 的 setTranslation 找不到行，自然忽略。
        if Self.shouldReplace(line, byNodeIntervals: Self.nodeIntervals(in: lines)) {
            return
        }
        insert(line)
    }

    mutating func setTranslation(key: Int, text: String) {
        guard let index = lines.firstIndex(where: { $0.key == key }) else {
            return
        }
        lines[index].dstText = text
    }

    /// 节点的 final 到达：替换所有被它覆盖的本机行，没有可替换的就按 t0 插入。
    /// 返回被替换掉的行的 key（调用方据此决定界面要不要重画）。
    @discardableResult
    mutating func applyNode(
        nodeID: String, t0: Double?, t1: Double?, srcText: String, dstText: String?
    ) -> [Int] {
        let key = nextNodeKey
        nextNodeKey -= 1
        insert(SubtitleLine(key: key, t0: t0, t1: t1, source: .node(nodeID), srcText: srcText, dstText: dstText))
        let intervals = Self.nodeIntervals(in: lines)
        let replaced = lines.filter { Self.shouldReplace($0, byNodeIntervals: intervals) }.map(\.key)
        let replacedSet = Set(replaced)
        lines.removeAll { line in
            line.source == .local && replacedSet.contains(line.key)
        }
        return replaced
    }

    /// 只有带时间的本机行才可能被替换；节点行永远不会被再次替换。
    static func shouldReplace(_ line: SubtitleLine, byNodeT0 n0: Double?, t1 n1: Double?) -> Bool {
        guard let n0, let n1 else {
            return false
        }
        return shouldReplace(line, byNodeIntervals: [(n0, n1)])
    }

    /// 当前 store 内所有节点行的时间并集覆盖本机行达到阈值时替换。
    /// 单条节点句仍走同一 helper，因此 001 的 P5 口径不变；TK-010 只是把多个节点句的覆盖累加。
    private static func shouldReplace(_ line: SubtitleLine, byNodeIntervals intervals: [(Double, Double)]) -> Bool {
        guard line.source == .local, let l0 = line.t0, let l1 = line.t1 else {
            return false
        }
        let duration = max(l1 - l0, 0)
        if duration <= epsilon {
            return intervals.contains { n0, n1 in
                l0 >= n0 - epsilon && l0 <= n1 + epsilon
            }
        }
        let overlap = coveredDuration(of: (l0, l1), by: intervals)
        return overlap >= duration * replaceRatio - epsilon
    }

    private static func nodeIntervals(in lines: [SubtitleLine]) -> [(Double, Double)] {
        lines.compactMap { line in
            guard case .node = line.source, let t0 = line.t0, let t1 = line.t1,
                  t0.isFinite, t1.isFinite, t1 >= t0 else {
                return nil
            }
            return (t0, t1)
        }
    }

    private static func coveredDuration(of local: (Double, Double), by intervals: [(Double, Double)]) -> Double {
        let clipped = intervals.compactMap { n0, n1 -> (Double, Double)? in
            let start = max(local.0, n0)
            let end = min(local.1, n1)
            return end > start ? (start, end) : nil
        }.sorted { left, right in
            if abs(left.0 - right.0) <= epsilon {
                return left.1 < right.1
            }
            return left.0 < right.0
        }
        guard var current = clipped.first else {
            return 0
        }
        var total = 0.0
        for interval in clipped.dropFirst() {
            if interval.0 <= current.1 + epsilon {
                current.1 = max(current.1, interval.1)
            } else {
                total += current.1 - current.0
                current = interval
            }
        }
        total += current.1 - current.0
        return total
    }

    /// 本机行一律按到达顺序追加（识别引擎自己保证顺序，且可能没有时间）；
    /// 只有节点行按 t0 定位：插在第一条 t0 更大的「有时间的行」之前，找不到就插在最后一条
    /// 有时间的行之后；t0 为 nil 的行不参与定位（它们的位置只由到达顺序决定）。
    private mutating func insert(_ line: SubtitleLine) {
        if case .node = line.source, let t0 = line.t0 {
            if let index = lines.firstIndex(where: { ($0.t0 ?? -Double.infinity) > t0 }) {
                lines.insert(line, at: index)
            } else if let last = lines.lastIndex(where: { $0.t0 != nil }) {
                lines.insert(line, at: last + 1)
            } else {
                lines.append(line)
            }
        } else {
            lines.append(line)
        }
        if lines.count > capacity {
            lines.removeFirst(lines.count - capacity)
        }
    }
}
