import Foundation

/// 把一段德语定稿文本切成句子。
///
/// SpeechTranscriber 的定稿结果是「一口气十几秒」的整段文本，里面常常有好几句；
/// 原样当成一句既会让翻译超长，也会让字幕十几秒才跳一次。
///
/// 切分判据：句末标点（`.` `!` `?` `…`）后面紧跟空白 + 大写字母（或文本结束）。
/// 反例必须留下：小数/日期/序数里的点（`3.5`、`31.10.`、`1. FC`）和常见德语缩写
/// （`z. B.`、`d. h.`、`u. a.`、`bzw.`、`Dr.`、`Nr.`、`ca.`、`usw.`、`Mio.`、`Mrd.`、`St.`）。
enum SentenceSplitter {
    /// 缩写词的判定只看到点号前那一段字母；统一小写比较。
    private static let abbreviations: Set<String> = [
        "z", "d", "u", "bzw", "dr", "nr", "ca", "usw", "mio", "mrd", "st",
    ]

    static func split(_ text: String) -> [String] {
        let characters = Array(text)
        guard !characters.isEmpty else { return [] }

        var sentences: [String] = []
        var start = 0
        var index = 0

        while index < characters.count {
            let character = characters[index]
            guard isSentencePunctuation(character) else {
                index += 1
                continue
            }

            // 连续标点算一个句末（`?!`、`...`），后面才看空白和大写。
            var runEnd = index
            while runEnd + 1 < characters.count,
                  isSentencePunctuation(characters[runEnd + 1]) {
                runEnd += 1
            }

            var next = runEnd + 1
            while next < characters.count, characters[next].isWhitespace {
                next += 1
            }

            let atEnd = next >= characters.count
            let nextStartsUppercase = !atEnd && characters[next].isUppercase
            if (atEnd || nextStartsUppercase),
               !isFalseBoundary(characters: characters, punctuationIndex: runEnd) {
                appendSentence(String(characters[start...runEnd]), to: &sentences)
                start = next
                index = next
                continue
            }

            index = runEnd + 1
        }

        if start < characters.count {
            appendSentence(String(characters[start...]), to: &sentences)
        }
        return sentences
    }

    /// 按各句字符数把一整段时间区间切成首尾相接的子区间。
    ///
    /// 第一句从原区间起点开始、最后一句在原区间终点结束，中间按累计字符占比推进。
    /// 语音识别不给分句级时间，这是唯一不引入额外延迟的近似。
    static func splitRange(
        start: Double,
        end: Double,
        characterCounts: [Int]
    ) -> [(start: Double, end: Double)] {
        guard !characterCounts.isEmpty else { return [] }
        guard start.isFinite, end.isFinite, end > start else {
            return characterCounts.map { _ in (start, end) }
        }
        let total = characterCounts.reduce(0, +)
        guard total > 0 else {
            return characterCounts.map { _ in (start, end) }
        }

        var result: [(start: Double, end: Double)] = []
        var consumed = 0
        for (index, count) in characterCounts.enumerated() {
            let pieceStart = start + (end - start) * Double(consumed) / Double(total)
            consumed += count
            let pieceEnd = index == characterCounts.count - 1
                ? end
                : start + (end - start) * Double(consumed) / Double(total)
            result.append((pieceStart, pieceEnd))
        }
        return result
    }

    private static func isSentencePunctuation(_ character: Character) -> Bool {
        character == "." || character == "!" || character == "?" || character == "…"
    }

    private static func appendSentence(_ raw: String, to sentences: inout [String]) {
        let trimmed = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        if !trimmed.isEmpty {
            sentences.append(trimmed)
        }
    }

    /// 句末标点是真的句末吗？这里只拦「点号」这一类，问号/感叹号不用看。
    private static func isFalseBoundary(characters: [Character], punctuationIndex: Int) -> Bool {
        guard characters[punctuationIndex] == "." else { return false }

        // 数字后面的点：小数（3.5）、日期（31.10.）、序数（1. FC）。
        var previous = punctuationIndex - 1
        while previous >= 0, characters[previous].isWhitespace {
            previous -= 1
        }
        if previous >= 0, characters[previous].isNumber {
            return true
        }

        // 常见缩写：取点号前连续的字母。
        var wordStart = punctuationIndex - 1
        while wordStart >= 0, characters[wordStart].isLetter {
            wordStart -= 1
        }
        let word = String(characters[(wordStart + 1)..<punctuationIndex]).lowercased()
        if abbreviations.contains(word) {
            return true
        }

        // 两段式缩写（z. B. / d. h. / u. a.）：第二个字母单独成词，
        // 但它前面紧挨着另一个缩写点。
        if word.count == 1 {
            var before = wordStart
            while before >= 0, characters[before].isWhitespace {
                before -= 1
            }
            if before >= 0, characters[before] == "." {
                var previousWordEnd = before - 1
                while previousWordEnd >= 0, characters[previousWordEnd].isLetter {
                    previousWordEnd -= 1
                }
                let previousWord = String(
                    characters[(previousWordEnd + 1)..<before]
                ).lowercased()
                if ["z", "d", "u"].contains(previousWord) {
                    return true
                }
            }
        }

        return false
    }
}
