import Foundation

/// 定稿分句的纯逻辑自测：切得太碎会把一句拆成两条字幕，切得太少又回到
/// 「十五秒一条」的老毛病，所以德语里的常见反例都要钉住。
@main
struct SentenceSplitterSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        // 三句拼在一条定稿里：每句都要单独出来。
        let three = SentenceSplitter.split(
            "Guten Abend. Wie geht es Ihnen? Mir geht es sehr gut!"
        )
        check(three == ["Guten Abend.", "Wie geht es Ihnen?", "Mir geht es sehr gut!"],
              "三句拼一条切成三句")

        // 小数、日期、序数：数字后面的点不是句末。
        let decimal = SentenceSplitter.split(
            "Der Anteil liegt bei 3.5 Prozent. Das ist viel."
        )
        check(decimal == ["Der Anteil liegt bei 3.5 Prozent.", "Das ist viel."],
              "3.5 Prozent 不切")

        let date = SentenceSplitter.split(
            "Am 31.10. hat es geregnet. Danach war es sonnig."
        )
        check(date == ["Am 31.10. hat es geregnet.", "Danach war es sonnig."],
              "am 31.10. hat 不切")

        let club = SentenceSplitter.split(
            "Der 1. FC Köln gewann. Danach feierten alle."
        )
        check(club == ["Der 1. FC Köln gewann.", "Danach feierten alle."],
              "der 1. FC Köln 不切")

        // 常见缩写。
        let example = SentenceSplitter.split(
            "Das ist z. B. heute so. Morgen ist es anders."
        )
        check(example == ["Das ist z. B. heute so.", "Morgen ist es anders."],
              "z. B. heute 不切")

        let doctor = SentenceSplitter.split(
            "Dr. Müller sagte das. Es war wichtig."
        )
        check(doctor == ["Dr. Müller sagte das.", "Es war wichtig."],
              "Dr. Müller sagte 不切")

        let otherAbbreviations = SentenceSplitter.split(
            "Wir treffen uns bzw. später. Das ist ca. richtig."
        )
        check(otherAbbreviations == ["Wir treffen uns bzw. später.", "Das ist ca. richtig."],
              "bzw. / ca. 不切")

        // 问号、感叹号同样是句末。
        let marks = SentenceSplitter.split("Wirklich? Ja! Dann los.")
        check(marks == ["Wirklich?", "Ja!", "Dann los."], "问号/感叹号切句")

        // 末尾没有标点的残句必须保留为最后一句。
        let trailing = SentenceSplitter.split("Erster Satz. Zweiter Teil ohne Punkt")
        check(trailing == ["Erster Satz.", "Zweiter Teil ohne Punkt"], "末尾残句保留")

        // 首尾空白去掉、空片段丢弃。
        let spacing = SentenceSplitter.split("  Hallo Welt.   Danach.  ")
        check(spacing == ["Hallo Welt.", "Danach."], "首尾空白去掉、空片段丢弃")

        // 区间按字符占比切分：首句从原起点开始，末句在原终点结束，且首尾相接。
        let ranges = SentenceSplitter.splitRange(
            start: 10,
            end: 110,
            characterCounts: [10, 20, 10]
        )
        check(ranges.count == 3, "区间数量与句子数一致")
        if ranges.count == 3 {
            check(ranges[0].start == 10, "第一句从原区间起点开始")
            check(ranges[0].end == 35, "第一句按字符占比结束")
            check(ranges[1].start == 35 && ranges[1].end == 85, "第二句区间按占比")
            check(ranges[2].start == 85, "最后一句与上一句首尾相接")
            check(ranges[2].end == 110, "最后一句在原区间终点结束")
        }

        let single = SentenceSplitter.splitRange(start: 5, end: 9, characterCounts: [42])
        check(single.count == 1 && single[0].start == 5 && single[0].end == 9,
              "只有一句时区间原样返回")

        if failures.isEmpty {
            print("SENTENCE SPLIT SELFTEST OK")
        } else {
            for failure in failures { print("FAIL: \(failure)") }
            exit(1)
        }
    }
}
