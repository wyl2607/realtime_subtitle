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
