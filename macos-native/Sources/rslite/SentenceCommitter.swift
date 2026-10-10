import Foundation

// 系统识别要等一整段话结束才给 final，一段常常是好几句、几十秒——翻译只能干等，
// 上屏也是一大坨。这里在 volatile 里看到**完整的句子**、且连续两次 volatile
// 都一致（前缀稳定）时就提前定稿，逐句送翻译；final 到了只补发剩下的部分。
struct SentenceCommitter {
    private var committedSentences: [String] = []
    private var lastStablePrefix: [String] = []

    /// 喂一条 volatile，返回这次新定稿的句子，以及去掉已定稿部分后剩下的草稿。
    mutating func volatile(_ text: String) -> (newSentences: [String], remainder: String) {
        let sentences = Self.split(text)
        // 最后一句可能还没说完，不算完整句
        let complete = Array(sentences.dropLast())
        var newSentences: [String] = []
        if complete.count > committedSentences.count,
           complete.starts(with: committedSentences),
           lastStablePrefix.count >= complete.count,
           Array(lastStablePrefix.prefix(complete.count)) == complete {
            newSentences = Array(complete.dropFirst(committedSentences.count))
            committedSentences = complete
        }
        lastStablePrefix = complete
        let remainder = sentences.dropFirst(min(committedSentences.count, sentences.count)).joined(separator: " ")
        return (newSentences, remainder)
    }

    /// 段落 final：返回还没定稿的句子（逐句），并清空状态等下一段。
    mutating func final(_ text: String) -> [String] {
        let sentences = Self.split(text)
        // final 可能改写了已提前定稿的句子；按句数跳过，不重复上屏
        let rest = Array(sentences.dropFirst(min(committedSentences.count, sentences.count)))
        committedSentences = []
        lastStablePrefix = []
        return rest
    }

    // 德语规则照搬 Python 版 text_rules：句末标点后跟空白、下一个词大写开头才算句尾；
    // 数字后的点是序数（"3. Oktober"），常见缩写的点不算。
    private static let abbreviations: Set<String> = ["z", "b", "dr", "nr", "bzw", "ca", "usw", "prof", "st", "vgl", "evtl", "inkl", "u", "a", "d", "h"]

    static func split(_ text: String) -> [String] {
        let chars = Array(text)
        var sentences: [String] = []
        var start = 0
        var i = 0
        while i < chars.count {
            let c = chars[i]
            if c == "." || c == "!" || c == "?" || c == "…" || c == "。" || c == "！" || c == "？" {
                var j = i + 1
                while j < chars.count, "\"“”»«)'".contains(chars[j]) { j += 1 }
                let atEnd = j >= chars.count
                let followedBySpace = !atEnd && chars[j].isWhitespace
                var k = j
                while k < chars.count, chars[k].isWhitespace { k += 1 }
                let nextUpper = k < chars.count && (chars[k].isUppercase || chars[k] == "\"" || chars[k] == "„")
                if atEnd || (followedBySpace && nextUpper && !isNonBoundary(chars, dotAt: i, mark: c)) {
                    let s = String(chars[start..<j]).trimmingCharacters(in: .whitespaces)
                    if !s.isEmpty { sentences.append(s) }
                    start = k
                    i = k
                    continue
                }
            }
            i += 1
        }
        if start < chars.count {
            let s = String(chars[start...]).trimmingCharacters(in: .whitespaces)
            if !s.isEmpty { sentences.append(s) }
        }
        // final 偶尔多出一个孤立的 "."：没有字母数字的不算句子，否则会上屏一行空句
        return sentences.filter { $0.contains(where: { $0.isLetter || $0.isNumber }) }
    }

    private static func isNonBoundary(_ chars: [Character], dotAt i: Int, mark: Character) -> Bool {
        guard mark == "." else { return false }
        var s = i
        while s > 0, !chars[s - 1].isWhitespace { s -= 1 }
        let token = String(chars[s..<i])
        if !token.isEmpty, token.allSatisfy(\.isNumber) { return true }
        return abbreviations.contains(token.lowercased().trimmingCharacters(in: CharacterSet(charactersIn: ".")))
    }
}

// MARK: - 句子时间（RFC P4）

/// 识别结果里一段带时间的文字：字符偏移（半开区间）+ 统一样本时钟上的起止秒数。
struct TimedSpan: Equatable {
    var start: Int
    var end: Int
    var t0: Double
    var t1: Double
}

enum SentenceTiming {
    /// 给每个句子算 [t0,t1]：句子在整段文字里的字符区间所碰到的所有 span 的并集。
    /// 句子由 SentenceCommitter.split 从同一段文字切出，按顺序出现，所以顺序查找即可；
    /// 找不到或没碰到任何 span 就给 nil（P4：拿不到时间的句子不参与替换）。
    static func times(
        for sentences: [String],
        in text: String,
        spans: [TimedSpan]
    ) -> [(t0: Double, t1: Double)?] {
        let chars = Array(text)
        var cursor = 0
        return sentences.map { sentence in
            let needle = Array(sentence)
            guard let a = find(needle, in: chars, from: cursor) else {
                return nil
            }
            let b = a + needle.count
            cursor = b
            let hit = spans.compactMap { clippedTime($0, toStart: a, end: b) }
            guard let t0 = hit.map(\.t0).min(), let t1 = hit.map(\.t1).max() else {
                return nil
            }
            return (t0, t1)
        }
    }

    private static func clippedTime(_ span: TimedSpan, toStart start: Int, end: Int) -> (t0: Double, t1: Double)? {
        let clippedStart = max(start, span.start)
        let clippedEnd = min(end, span.end)
        guard clippedEnd > clippedStart, span.end > span.start else {
            return nil
        }
        let ratio0 = Double(clippedStart - span.start) / Double(span.end - span.start)
        let ratio1 = Double(clippedEnd - span.start) / Double(span.end - span.start)
        let duration = span.t1 - span.t0
        let t0 = span.t0 + duration * ratio0
        let t1 = span.t0 + duration * ratio1
        guard t0.isFinite, t1.isFinite else {
            return nil
        }
        return (t0, t1)
    }

    fileprivate static func find(_ needle: [Character], in haystack: [Character], from: Int) -> Int? {
        guard !needle.isEmpty, haystack.count >= needle.count else {
            return nil
        }
        var i = from
        while i + needle.count <= haystack.count {
            if haystack[i] == needle[0], Array(haystack[i..<(i + needle.count)]) == needle {
                return i
            }
            i += 1
        }
        return nil
    }
}

// MARK: - 字幕行与混合替换（RFC P5）

enum LineSource: Equatable {
    case local
    case node(String)
}

struct SubtitleLine: Equatable {
    /// 行的唯一键：本机行用 Pipeline 的句子 id，节点行由 LineStore 分配（负数，避免撞车）。
    var key: Int
    var t0: Double?
    var t1: Double?
    var source: LineSource
    var srcText: String
    var dstText: String?
}

/// 字幕历史。纯逻辑，不碰界面，方便脱离 AppKit 测试。
/// 界面只显示末尾几行；已经滚出屏幕的行也留在这里（P5：只更新内存里的历史，不回滚画面）。
struct LineStore {
    private(set) var lines: [SubtitleLine] = []
    private var nextNodeKey = -1
    private let capacity: Int

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

    /// 本机行一律按到达顺序追加（Pipeline 自己保证顺序，且可能没有时间）；
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

    /// 静态自检：返回失败描述，全部通过返回空数组。入口（--selftest）由 TK-005 接线。
    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }
        func keys(_ s: LineStore) -> [Int] { s.lines.map(\.key) }

        // TK-009：Speech 的 audioTimeRange 可能覆盖从段首到当前句尾的宽区间；
        // 句子时间必须取与句子字符区间相交后的时间，而不是直接沿用宽 span 的段首 t0。
        do {
            let text = "Erster Satz. Zweiter Satz. Dritter Satz."
            let sentences = SentenceCommitter.split(text)
            let chars = Array(text)
            if let a0 = SentenceTiming.find(Array(sentences[0]), in: chars, from: 0),
               let a1 = SentenceTiming.find(Array(sentences[1]), in: chars, from: a0 + sentences[0].count),
               let a2 = SentenceTiming.find(Array(sentences[2]), in: chars, from: a1 + sentences[1].count) {
                let b0 = a0 + sentences[0].count
                let b1 = a1 + sentences[1].count
                let b2 = a2 + sentences[2].count
                let times = SentenceTiming.times(for: sentences, in: text, spans: [
                    TimedSpan(start: 0, end: b0, t0: 79.38, t1: 82.70),
                    TimedSpan(start: 0, end: b1, t0: 79.38, t1: 87.50),
                    TimedSpan(start: 0, end: b2, t0: 79.38, t1: 90.40),
                ])
                check(times.count == 3 && times.allSatisfy { $0 != nil }, "TK-009 宽 span 应能算出三句时间")
                if times.count == 3, let t0 = times[0], let t1 = times[1], let t2 = times[2] {
                    check(t1.t0 > t0.t0 && t2.t0 > t1.t0, "TK-009 后续句 t0 应随句子前进")
                    check(t0.t0 < t0.t1 && t1.t0 < t1.t1 && t2.t0 < t2.t1, "TK-009 每句时间应保持正时长")
                }
            } else {
                check(false, "TK-009 复现文本应能定位三句")
            }
        }

        // 刚好 50%：本机 [0,10]，节点 [5,20] 重叠 5 = 50% -> 替换
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 10, text: "a")
            let r = s.applyNode(nodeID: "n", t0: 5, t1: 20, srcText: "x", dstText: nil)
            check(r == [1], "刚好50%应替换")
            check(s.lines.count == 1 && s.lines[0].key < 0, "刚好50%后只剩节点行")
        }
        // 略低于 50%：节点 [5.01,20] 重叠 4.99 -> 不替换，节点行插在本机行之后
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 10, text: "a")
            let r = s.applyNode(nodeID: "n", t0: 5.01, t1: 20, srcText: "x", dstText: nil)
            check(r.isEmpty, "略低于50%不应替换")
            check(s.lines.count == 2 && s.lines[0].key == 1 && s.lines[1].key < 0, "略低于50%应并存且节点在后")
        }
        // 一行替换多行：节点 [0,30] 覆盖三条本机行
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 8, text: "a")
            s.addLocal(key: 2, t0: 9, t1: 18, text: "b")
            s.addLocal(key: 3, t0: 19, t1: 29, text: "c")
            s.addLocal(key: 4, t0: 40, t1: 50, text: "d")
            let r = s.applyNode(nodeID: "n", t0: 0, t1: 30, srcText: "x", dstText: "y")
            check(Set(r) == [1, 2, 3], "一行替换多行：被替换集合")
            check(s.lines.count == 2 && s.lines[0].key < 0 && s.lines[1].key == 4, "一行替换多行：剩余顺序")
        }
        // 无匹配时按 t0 插入到两条有时间行之间，且避开 nil 行
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 5, text: "a")
            s.addLocal(key: 2, t0: nil, t1: nil, text: "n")
            s.addLocal(key: 3, t0: 30, t1: 35, text: "c")
            s.applyNode(nodeID: "n", t0: 10, t1: 12, srcText: "x", dstText: nil)
            check(keys(s).count == 4 && keys(s)[0] == 1 && keys(s)[1] == 2 && keys(s)[2] < 0 && keys(s)[3] == 3,
                  "无匹配插入应在有时间行之间且不被 nil 行打乱")
        }
        // 无匹配且比所有有时间行都晚：插在最后一条有时间行之后（nil 行之前）
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 5, text: "a")
            s.addLocal(key: 2, t0: nil, t1: nil, text: "n")
            s.applyNode(nodeID: "n", t0: 50, t1: 52, srcText: "x", dstText: nil)
            check(keys(s).count == 3 && keys(s)[0] == 1 && keys(s)[1] < 0 && keys(s)[2] == 2,
                  "最晚节点行应紧跟最后一条有时间行")
        }
        // 本机行按到达顺序追加，即使 t0 更小
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 10, t1: 12, text: "a")
            s.addLocal(key: 2, t0: 3, t1: 4, text: "b")
            check(keys(s) == [1, 2], "本机行应按到达顺序追加")
        }
        // 节点行不会被二次替换
        do {
            var s = LineStore()
            s.applyNode(nodeID: "n1", t0: 0, t1: 10, srcText: "x", dstText: nil)
            let first = keys(s)
            let r = s.applyNode(nodeID: "n2", t0: 0, t1: 10, srcText: "y", dstText: nil)
            check(r.isEmpty && s.lines.count == 2 && s.lines.contains { $0.key == first[0] }, "节点行不应被二次替换")
        }
        // 无时间的本机行不会被替换
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: nil, t1: nil, text: "a")
            let r = s.applyNode(nodeID: "n", t0: 0, t1: 10, srcText: "x", dstText: nil)
            check(r.isEmpty, "无时间本机行不应被替换")
        }
        // 节点先到、本机后到：覆盖 >=50% 的本机行丢弃；不覆盖/无时间的照常追加
        do {
            var s = LineStore()
            s.applyNode(nodeID: "n", t0: 5, t1: 20, srcText: "x", dstText: nil)
            s.addLocal(key: 1, t0: 0, t1: 10, text: "a") // 重叠 5 = 50% -> 丢弃
            check(s.lines.count == 1 && s.lines[0].key < 0, "节点先到：被覆盖的本机行应丢弃")
            s.addLocal(key: 2, t0: 5.01 - 10, t1: 5.01, text: "b") // 重叠 0.01 -> 保留
            s.addLocal(key: 3, t0: nil, t1: nil, text: "c") // 无时间 -> 保留
            check(s.lines.count == 3, "节点先到：未被覆盖/无时间的本机行应保留")
        }
        // TK-010：节点把一句本机句拆成两句；单条都不足 50%，累计覆盖达标后替换本机句。
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 10, text: "local")
            let first = s.applyNode(nodeID: "n", t0: 0, t1: 3.4, srcText: "part1", dstText: nil)
            check(first.isEmpty && s.lines.contains { $0.key == 1 }, "TK-010 第一条拆分节点不足50%不应替换")
            let second = s.applyNode(nodeID: "n", t0: 4.2, t1: 9.0, srcText: "part2", dstText: nil)
            check(second == [1], "TK-010 第二条到达后累计覆盖应替换本机句")
            check(!s.lines.contains { $0.key == 1 } && s.lines.filter {
                if case .node = $0.source { return true }
                return false
            }.count == 2, "TK-010 替换后应保留两条节点句")
        }
        // TK-010：累计仍低于 50% 时不替换。
        do {
            var s = LineStore()
            s.addLocal(key: 1, t0: 0, t1: 10, text: "local")
            s.applyNode(nodeID: "n", t0: 0, t1: 2.0, srcText: "part1", dstText: nil)
            let second = s.applyNode(nodeID: "n", t0: 3.0, t1: 4.9, srcText: "part2", dstText: nil)
            check(second.isEmpty && s.lines.contains { $0.key == 1 }, "TK-010 累计低于50%不应替换")
        }
        // TK-010：节点先到时，后到本机句也按节点并集覆盖判定丢弃。
        do {
            var s = LineStore()
            s.applyNode(nodeID: "n", t0: 0, t1: 3.4, srcText: "part1", dstText: nil)
            s.applyNode(nodeID: "n", t0: 4.2, t1: 9.0, srcText: "part2", dstText: nil)
            s.addLocal(key: 1, t0: 0, t1: 10, text: "local")
            check(!s.lines.contains { $0.key == 1 }, "TK-010 节点先到累计覆盖时本机句应丢弃")
        }
        return failures
    }
}
