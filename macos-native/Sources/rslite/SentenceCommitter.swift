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
            let hit = spans.filter { $0.end > a && $0.start < b }
            guard let t0 = hit.map(\.t0).min(), let t1 = hit.map(\.t1).max() else {
                return nil
            }
            return (t0, t1)
        }
    }

    private static func find(_ needle: [Character], in haystack: [Character], from: Int) -> Int? {
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
        insert(SubtitleLine(key: key, t0: t0, t1: t1, source: .local, srcText: text, dstText: nil))
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
        let replaced = lines.filter { Self.shouldReplace($0, byNodeT0: t0, t1: t1) }.map(\.key)
        lines.removeAll { replaced.contains($0.key) }
        let key = nextNodeKey
        nextNodeKey -= 1
        insert(SubtitleLine(key: key, t0: t0, t1: t1, source: .node(nodeID), srcText: srcText, dstText: dstText))
        return replaced
    }

    /// 只有带时间的本机行才可能被替换；节点行永远不会被再次替换。
    static func shouldReplace(_ line: SubtitleLine, byNodeT0 n0: Double?, t1 n1: Double?) -> Bool {
        guard line.source == .local, let n0, let n1,
              let l0 = line.t0, let l1 = line.t1 else {
            return false
        }
        let duration = max(l1 - l0, 0)
        if duration <= epsilon {
            return l0 >= n0 - epsilon && l0 <= n1 + epsilon
        }
        let overlap = min(l1, n1) - max(l0, n0)
        return overlap >= duration * replaceRatio - epsilon
    }

    /// 按 t0 升序插入；同 t0 排在后面；没有时间的行放末尾（只会是本机行，按到达顺序）。
    private mutating func insert(_ line: SubtitleLine) {
        if let t0 = line.t0 {
            let index = lines.firstIndex { other in
                guard let o = other.t0 else { return true }
                return o > t0
            } ?? lines.count
            lines.insert(line, at: index)
        } else {
            lines.append(line)
        }
        if lines.count > capacity {
            lines.removeFirst(lines.count - capacity)
        }
    }
}
