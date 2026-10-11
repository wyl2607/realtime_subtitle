import Foundation

/// 识别来源、语言、运行模式，以及可选的指定节点。
/// 缺字段、空白、坏 JSON 回默认。认不出的 mode 回默认 local；磁盘上显式写了 auto 的仍是 auto。
/// pinnedNode 缺失、null、空白或类型不对都视为不指定，不把其它字段一起丢掉。
struct AppSettings: Codable, Equatable {
    var source: String
    var src: String
    var dst: String
    var mode: String
    /// 节点 id。nil 表示不指定。
    var pinnedNode: String?

    static let defaultSource = "tap"
    static let defaultSrc = "de-DE"
    static let defaultDst = "zh-Hans"
    static let defaultMode = "local"
    static let modes: Set<String> = ["auto", "local", "hybrid"]

    static let `default` = AppSettings(
        source: defaultSource,
        src: defaultSrc,
        dst: defaultDst,
        mode: defaultMode,
        pinnedNode: nil
    )

    init(source: String, src: String, dst: String, mode: String, pinnedNode: String? = nil) {
        self.source = source
        self.src = src
        self.dst = dst
        self.mode = mode
        self.pinnedNode = pinnedNode
    }

    /// 空白 source/src/dst 回默认。mode 不是 auto、local、hybrid 时回默认 local。
    /// 空白 pinnedNode 视为不指定。
    static func normalized(_ settings: AppSettings) -> AppSettings {
        var copy = settings
        let source = copy.source.trimmingCharacters(in: .whitespacesAndNewlines)
        let src = copy.src.trimmingCharacters(in: .whitespacesAndNewlines)
        let dst = copy.dst.trimmingCharacters(in: .whitespacesAndNewlines)
        let mode = copy.mode.trimmingCharacters(in: .whitespacesAndNewlines)
        copy.source = source.isEmpty ? defaultSource : source
        copy.src = src.isEmpty ? defaultSrc : src
        copy.dst = dst.isEmpty ? defaultDst : dst
        copy.mode = modes.contains(mode) ? mode : defaultMode
        copy.pinnedNode = normalizedPin(copy.pinnedNode)
        return copy
    }

    /// 去掉两端空白。空字符串视为不指定。
    static func normalizedPin(_ value: String?) -> String? {
        guard let trimmed = value?.trimmingCharacters(in: .whitespacesAndNewlines), !trimmed.isEmpty else {
            return nil
        }
        return trimmed
    }

    static func fromJSON(_ data: Data) -> AppSettings {
        guard let partial = try? JSONDecoder().decode(AppSettingsPartial.self, from: data) else {
            return .default
        }
        return normalized(AppSettings(
            source: partial.source ?? defaultSource,
            src: partial.src ?? defaultSrc,
            dst: partial.dst ?? defaultDst,
            mode: partial.mode ?? defaultMode,
            pinnedNode: partial.pinnedNode
        ))
    }

    /// 命令行里显式给出的 `--source` / `--src` / `--dst` / `--mode` / `--node` 优先。
    /// 没给出的项用 settings。认不出的 `--mode` 不覆盖（`parse` 仍会因此退出）。
    /// 某个开关缺了取值就停在已解析的结果上，不崩溃。
    static func resolve(argv: [String], settings: AppSettings) -> AppSettings {
        var resolved = normalized(settings)
        var index = 0
        while index < argv.count {
            let key = argv[index]
            switch key {
            case "--source", "--src", "--dst", "--mode", "--node":
                guard index + 1 < argv.count else {
                    return resolved
                }
                let value = argv[index + 1]
                switch key {
                case "--source":
                    resolved.source = value
                case "--src":
                    resolved.src = value
                case "--dst":
                    resolved.dst = value
                case "--node":
                    resolved.pinnedNode = normalizedPin(value)
                default:
                    if modes.contains(value) {
                        resolved.mode = value
                    }
                }
                index += 2
            default:
                index += 1
            }
        }
        return resolved
    }

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }

        let defaults = AppSettings.default
        check(defaults.source == "tap", "默认 source tap")
        check(defaults.src == "de-DE", "默认 src de-DE")
        check(defaults.dst == "zh-Hans", "默认 dst zh-Hans")
        check(defaults.mode == "local", "默认 mode local")
        check(defaults.pinnedNode == nil, "默认不指定节点")
        check(RSLiteMode(rawValue: defaults.mode) == .local, "默认 mode 能被 RSLiteMode 识别")
        for mode in ["auto", "local", "hybrid"] {
            check(RSLiteMode(rawValue: mode) != nil, "mode \(mode) 与 RSLiteMode 一致")
        }

        let edited = AppSettings(source: "mic", src: "zh-CN", dst: "de-DE", mode: "local")
        do {
            let data = try JSONEncoder().encode(edited)
            let decoded = try JSONDecoder().decode(AppSettings.self, from: data)
            check(decoded == edited, "JSON 往返")
            check(fromJSON(data) == edited, "fromJSON 往返")
        } catch {
            failures.append("JSON 往返异常：\(error)")
        }

        check(fromJSON(Data("{}".utf8)) == defaults, "空对象回默认")
        check(fromJSON(Data()) == defaults, "空数据回默认")
        check(fromJSON(Data("{".utf8)) == defaults, "残缺 JSON 回默认")
        check(fromJSON(Data("[]".utf8)) == defaults, "数组 JSON 回默认")
        check(fromJSON(Data("null".utf8)) == defaults, "null 回默认")
        check(fromJSON(Data(#"{"mode":1,"source":"mic"}"#.utf8)) == defaults, "字段类型错误回默认")
        check(fromJSON(Data(#"{"source":"   ","src":"","dst":"","mode":" "}"#.utf8)) == defaults, "空白字段回默认")

        let extra = fromJSON(Data(#"{"source":"mic","extra":true,"token":"secret"}"#.utf8))
        check(extra.source == "mic" && extra.src == defaults.src && extra.dst == defaults.dst && extra.mode == defaults.mode && extra.pinnedNode == nil, "未知字段忽略")

        let partial = fromJSON(Data(#"{"dst":"de-DE"}"#.utf8))
        check(partial.dst == "de-DE", "缺字段保留已给的 dst")
        check(partial.source == "tap" && partial.src == "de-DE" && partial.mode == "local" && partial.pinnedNode == nil, "缺字段用默认")
        check(fromJSON(Data(#"{"mode":"auto"}"#.utf8)).mode == "auto", "显式 auto 保留")
        check(fromJSON(Data(#"{"mode":"auto","pinnedNode":null}"#.utf8)) == AppSettings(source: "tap", src: "de-DE", dst: "zh-Hans", mode: "auto", pinnedNode: nil), "显式 auto 且 pinnedNode 为 null")

        let unknown = fromJSON(Data(#"{"mode":"karaoke","source":"mic","src":"en-US","dst":"de-DE"}"#.utf8))
        check(unknown.mode == "local", "未知 mode 回默认")
        check(unknown.source == "mic" && unknown.src == "en-US" && unknown.dst == "de-DE", "未知 mode 保留其它字段")
        check(fromJSON(Data(#"{"mode":"Local"}"#.utf8)).mode == "local", "大小写不对的 mode 回默认")
        check(fromJSON(Data(#"{"mode":"hybrid"}"#.utf8)).mode == "hybrid", "合法 mode hybrid")
        check(fromJSON(Data(#"{"mode":"local"}"#.utf8)).mode == "local", "合法 mode local")

        let pinned = AppSettings(source: "mic", src: "zh-CN", dst: "de-DE", mode: "hybrid", pinnedNode: "mini2")
        do {
            let data = try JSONEncoder().encode(pinned)
            let decoded = try JSONDecoder().decode(AppSettings.self, from: data)
            check(decoded == pinned, "pinnedNode JSON 往返")
            check(fromJSON(data) == pinned, "pinnedNode fromJSON 往返")
        } catch {
            failures.append("pinnedNode JSON 往返异常：\(error)")
        }
        check(fromJSON(Data(#"{"mode":"hybrid","pinnedNode":"  mini2  "}"#.utf8)).pinnedNode == "mini2", "pinnedNode 去掉两端空白")
        check(fromJSON(Data(#"{"pinnedNode":"   "}"#.utf8)).pinnedNode == nil, "空白 pinnedNode 视为不指定")
        check(fromJSON(Data(#"{"pinnedNode":""}"#.utf8)).pinnedNode == nil, "空 pinnedNode 视为不指定")
        let badPin = fromJSON(Data(#"{"source":"mic","mode":"auto","pinnedNode":1,"src":"en-US"}"#.utf8))
        check(badPin.source == "mic" && badPin.mode == "auto" && badPin.src == "en-US" && badPin.pinnedNode == nil, "pinnedNode 坏值丢弃且保留其它字段")
        check(normalized(AppSettings(source: "tap", src: "de-DE", dst: "zh-Hans", mode: "hybrid", pinnedNode: " ")).pinnedNode == nil, "normalized 空白 pinnedNode 视为不指定")

        let custom = AppSettings(source: "mic", src: "en-US", dst: "de-DE", mode: "hybrid")
        check(resolve(argv: [], settings: custom) == custom, "无参数沿用 AppSettings")
        let sourceOnly = resolve(argv: ["--source", "tap"], settings: custom)
        check(sourceOnly.source == "tap", "命令行 --source 优先")
        check(sourceOnly.src == "en-US" && sourceOnly.dst == "de-DE" && sourceOnly.mode == "hybrid", "未给出的参数保持 AppSettings")
        let languages = resolve(argv: ["--src", "zh-CN", "--dst", "de-DE"], settings: custom)
        check(languages.src == "zh-CN" && languages.dst == "de-DE" && languages.source == "mic" && languages.mode == "hybrid", "命令行 --src --dst 优先")
        check(resolve(argv: ["--mode", "local"], settings: custom).mode == "local", "命令行 --mode 优先")
        check(resolve(argv: ["--mode", "auto"], settings: custom).mode == "auto", "命令行 --mode auto 覆盖 hybrid")
        check(resolve(argv: ["--source", "tap", "--source", "file:/tmp/a.wav"], settings: custom).source == "file:/tmp/a.wav", "重复参数以后者为准")
        check(resolve(argv: ["--mode", "karaoke"], settings: custom).mode == "hybrid", "未知 CLI mode 不覆盖")
        check(resolve(argv: ["--source"], settings: custom) == custom, "缺少取值不崩溃")
        check(resolve(argv: ["--node"], settings: custom) == custom, "--node 缺少取值不崩溃")
        check(resolve(argv: ["--headless", "--selftest"], settings: custom) == custom, "--headless 不改变识别参数")
        check(resolve(argv: ["--source", "file:/tmp/a.wav"], settings: defaults).source == "file:/tmp/a.wav", "命令行 file: 覆盖 source")
        let normalizedMode = resolve(
            argv: [],
            settings: AppSettings(source: "mic", src: "en-US", dst: "de-DE", mode: "nope")
        )
        check(normalizedMode.mode == "local" && normalizedMode.source == "mic", "resolve 未知 mode 回默认")
        let afterCloud = resolve(argv: ["--cloud", "on", "--source", "mic"], settings: defaults)
        check(afterCloud.source == "mic" && afterCloud.src == "de-DE" && afterCloud.mode == "local", "其它带值参数不挡住 --source")
        let pinnedSettings = AppSettings(source: "mic", src: "en-US", dst: "de-DE", mode: "hybrid", pinnedNode: "desk")
        check(resolve(argv: [], settings: pinnedSettings).pinnedNode == "desk", "无参数沿用 pinnedNode")
        check(resolve(argv: ["--source", "tap"], settings: pinnedSettings).pinnedNode == "desk", "--node 未给出时保持设置")
        let nodeOverride = resolve(argv: ["--node", "mini2"], settings: pinnedSettings)
        check(nodeOverride.pinnedNode == "mini2" && nodeOverride.mode == "hybrid", "命令行 --node 优先于设置")
        let nodeAndMode = resolve(argv: ["--mode", "hybrid", "--node", "win"], settings: defaults)
        check(nodeAndMode.mode == "hybrid" && nodeAndMode.pinnedNode == "win" && nodeAndMode.source == "tap", "命令行 --mode 与 --node 同时生效")
        let modeKeepsPin = resolve(argv: ["--mode", "local"], settings: pinnedSettings)
        check(modeKeepsPin.mode == "local" && modeKeepsPin.pinnedNode == "desk", "--mode 不清除 pinnedNode")
        check(resolve(argv: ["--node", "  mini2  "], settings: defaults).pinnedNode == "mini2", "命令行 --node 去掉空白")
        check(resolve(argv: ["--node", " "], settings: pinnedSettings).pinnedNode == nil, "命令行空白 --node 视为不指定")
        check(resolve(argv: ["--node", "mini2", "--source", "mic"], settings: defaults).source == "mic", "--node 不挡住 --source")
        let mixed = resolve(argv: ["--headless", "--mode", "local", "--source", "mic"], settings: custom)
        check(mixed.mode == "local" && mixed.source == "mic" && mixed.src == "en-US" && mixed.dst == "de-DE", "混合参数只覆盖显式项")

        let dir = URL(fileURLWithPath: NSTemporaryDirectory())
            .appendingPathComponent("rslite-appsettings-\(UUID().uuidString)")
        defer { try? FileManager.default.removeItem(at: dir) }
        let missing = dir.appendingPathComponent("missing")
        check(AppSettingsStore.load(directory: missing) == defaults, "没有文件回默认")
        check(
            !FileManager.default.fileExists(atPath: AppSettingsStore.fileURL(directory: missing).path),
            "没有文件不创建 settings.json"
        )
        do {
            try AppSettingsStore.save(edited, directory: dir)
            check(AppSettingsStore.load(directory: dir) == edited, "磁盘往返")
            let perm = (try FileManager.default.attributesOfItem(
                atPath: AppSettingsStore.fileURL(directory: dir).path
            )[.posixPermissions] as? NSNumber)?.intValue ?? 0
            check(perm & 0o777 == 0o600, "settings.json 权限 0600")
            let url = AppSettingsStore.fileURL(directory: dir)
            try Data(#"{"source":"mic"}"#.utf8).write(to: url)
            let kept = AppSettingsStore.load(directory: dir)
            check(kept.source == "mic" && kept.src == "de-DE" && kept.dst == "zh-Hans" && kept.mode == "local" && kept.pinnedNode == nil, "磁盘缺字段用默认")
            try Data(#"{"mode":"karaoke","src":"en-US"}"#.utf8).write(to: url)
            let modeKept = AppSettingsStore.load(directory: dir)
            check(modeKept.mode == "local" && modeKept.src == "en-US" && modeKept.source == "tap", "磁盘未知 mode 回默认")
            try Data(#"{"mode":"auto","pinnedNode":"desk"}"#.utf8).write(to: url)
            let keptAuto = AppSettingsStore.load(directory: dir)
            check(keptAuto.mode == "auto" && keptAuto.pinnedNode == "desk", "磁盘显式 auto 与 pinnedNode 保留")
            try Data(#"{"mode":"auto","pinnedNode":1,"src":"en-US"}"#.utf8).write(to: url)
            let badPinOnDisk = AppSettingsStore.load(directory: dir)
            check(badPinOnDisk.mode == "auto" && badPinOnDisk.pinnedNode == nil && badPinOnDisk.src == "en-US", "磁盘 pinnedNode 坏值丢弃且保留其它字段")
            try Data(#"{"pinnedNode":"  mini2  "}"#.utf8).write(to: url)
            let trimmedPin = AppSettingsStore.load(directory: dir)
            check(trimmedPin.pinnedNode == "mini2" && trimmedPin.mode == "local", "磁盘 pinnedNode 去空白，缺 mode 为 local")
            try AppSettingsStore.save(pinned, directory: dir)
            check(AppSettingsStore.load(directory: dir) == pinned, "pinnedNode 磁盘往返")
            try Data("%%%".utf8).write(to: url)
            check(AppSettingsStore.load(directory: dir) == defaults, "磁盘坏 JSON 回默认")
        } catch {
            failures.append("AppSettings 磁盘往返异常：\(error)")
        }

        return failures
    }
}

private struct AppSettingsPartial: Decodable {
    var source: String?
    var src: String?
    var dst: String?
    var mode: String?
    var pinnedNode: String?

    enum CodingKeys: String, CodingKey {
        case source, src, dst, mode, pinnedNode
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        source = try container.decodeIfPresent(String.self, forKey: .source)
        src = try container.decodeIfPresent(String.self, forKey: .src)
        dst = try container.decodeIfPresent(String.self, forKey: .dst)
        mode = try container.decodeIfPresent(String.self, forKey: .mode)
        if container.contains(.pinnedNode) {
            if try container.decodeNil(forKey: .pinnedNode) {
                pinnedNode = nil
            } else if let text = try? container.decode(String.self, forKey: .pinnedNode) {
                pinnedNode = text
            } else {
                pinnedNode = nil
            }
        } else {
            pinnedNode = nil
        }
    }
}

enum AppSettingsStore {
    static let fileName = "settings.json"

    static func fileURL(directory: URL = CloudConfigStore.defaultDirectory) -> URL {
        directory.appendingPathComponent(fileName)
    }

    static func load(directory: URL = CloudConfigStore.defaultDirectory) -> AppSettings {
        guard let data = try? Data(contentsOf: fileURL(directory: directory)) else {
            return .default
        }
        return AppSettings.fromJSON(data)
    }

    static func save(_ settings: AppSettings, directory: URL = CloudConfigStore.defaultDirectory) throws {
        let encoder = JSONEncoder()
        encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
        let data = try encoder.encode(AppSettings.normalized(settings))
        try CloudConfigStore.atomicWrite(data, to: fileURL(directory: directory), mode: 0o600)
    }
}
