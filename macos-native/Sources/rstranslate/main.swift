import Foundation
import Translation

// 常驻翻译服务：stdin 每行一个 JSON 请求，stdout 每行一个 JSON 响应（严格串行）。
// 诊断信息不写 stdout，只写 stderr。

@main
struct RsTranslate {
    static func main() async {
        var sessions: [String: TranslationSession] = [:]
        writeLine(["ready": true])
        while let line = readLine(strippingNewline: true) {
            let trimmed = line.trimmingCharacters(in: .whitespacesAndNewlines)
            if trimmed.isEmpty {
                continue
            }
            let reply = await handle(trimmed, sessions: &sessions)
            writeLine(reply)
        }
    }

    static func handle(_ line: String, sessions: inout [String: TranslationSession]) async -> [String: Any] {
        guard let data = line.data(using: .utf8),
              let obj = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else {
            return errorReply(nil, "请求不是合法的 JSON 对象")
        }
        let id = obj["id"] as? Int
        guard let op = obj["op"] as? String else {
            return errorReply(id, "请求缺少 op 字段")
        }
        guard let src = obj["src"] as? String, let dst = obj["dst"] as? String else {
            return errorReply(id, "请求缺少 src 或 dst 字段")
        }
        let srcLang = Locale.Language(identifier: src)
        let dstLang = Locale.Language(identifier: dst)
        let key = "\(src)->\(dst)"

        switch op {
        case "status":
            let status = await LanguageAvailability().status(from: srcLang, to: dstLang)
            let name: String
            if status == .installed {
                name = "installed"
            } else if status == .supported {
                name = "supported"
            } else {
                name = "unsupported"
            }
            return reply(id, ["status": name])

        case "translate":
            guard let text = obj["text"] as? String else {
                return errorReply(id, "translate 请求缺少 text 字段")
            }
            if sessions[key] == nil {
                let status = await LanguageAvailability().status(from: srcLang, to: dstLang)
                guard status == .installed else {
                    return errorReply(id, "语言对 \(src)→\(dst) 未安装，请到「系统设置 → 通用 → 语言与地区 → 翻译语言」下载后重试")
                }
                let session = TranslationSession(installedSource: srcLang, target: dstLang)
                do {
                    try await session.prepareTranslation()
                } catch {
                    return errorReply(id, "语言对 \(src)→\(dst) 准备失败：\(error.localizedDescription)")
                }
                sessions[key] = session
            }
            guard let session = sessions[key] else {
                return errorReply(id, "翻译会话不存在")
            }
            do {
                let response = try await session.translate(text)
                var out = response.targetText.trimmingCharacters(in: .whitespacesAndNewlines)
                if isSimplifiedChinese(dstLang) {
                    out = toSimplified(out)
                }
                return reply(id, ["text": out])
            } catch {
                return errorReply(id, "翻译失败：\(error.localizedDescription)")
            }

        default:
            return errorReply(id, "未知 op：\(op)")
        }
    }

    static func isSimplifiedChinese(_ lang: Locale.Language) -> Bool {
        lang.languageCode?.identifier == "zh" && lang.script?.identifier == "Hans"
    }

    // 繁转简兜底：系统翻译偶尔输出繁体字，用 ICU 转换修正。
    static func toSimplified(_ s: String) -> String {
        s.applyingTransform(StringTransform("Hant-Hans"), reverse: false) ?? s
    }

    static func reply(_ id: Int?, _ fields: [String: Any]) -> [String: Any] {
        var r = fields
        setID(&r, id)
        return r
    }

    static func errorReply(_ id: Int?, _ message: String) -> [String: Any] {
        var r: [String: Any] = ["error": message]
        setID(&r, id)
        return r
    }

    static func setID(_ r: inout [String: Any], _ id: Int?) {
        if let id {
            r["id"] = id
        } else {
            r["id"] = NSNull()
        }
    }

    static func writeLine(_ obj: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: obj, options: [.sortedKeys]) else {
            return
        }
        var out = data
        out.append(0x0A)
        FileHandle.standardOutput.write(out)
    }
}
