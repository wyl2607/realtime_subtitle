import Foundation

/// 硬约束 HC-U18：App Group 文件存储（SharedStore）的命令行自测。
///
/// 注入临时目录和内存旧 Keychain 闭包，不碰真实 Keychain 和真实 App Group 容器。
@main
struct SharedStoreSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }

        let tempDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("sharedstore-selftest-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: tempDir) }

        let store = SharedStore(
            rootURL: tempDir,
            legacyRead: { _ in nil },
            legacyDelete: { _ in true }
        )

        // 1. 缺文件返回 nil（以及包装读取器的默认值）
        check(store.readExtStatus() == nil, "缺文件时 readExtStatus 应返回 nil")
        check(store.readLatestLines() == nil, "缺文件时 readLatestLines 应返回 nil")
        check(store.readLatestDraft() == nil, "缺文件时 readLatestDraft 应返回 nil")
        check(store.readEngineMode() == "auto", "缺文件时 readEngineMode 应返回 auto")
        check(store.readCloudEnabled() == false, "缺文件时 readCloudEnabled 应返回 false")

        // 2. 往返读写
        let writeDate = Date(timeIntervalSince1970: 1_700_000_123.5)
        check(store.writeExtStatus("engine=node last=ready src=broadcast", at: writeDate), "writeExtStatus 应成功")
        check(store.readExtStatus() == "engine=node last=ready src=broadcast", "readExtStatus 应读回写入内容")
        let entry = store.readExtStatusEntry()
        check(entry?.status == "engine=node last=ready src=broadcast", "readExtStatusEntry 应读回状态文本")
        check(
            entry?.updatedAt.map { abs($0.timeIntervalSince(writeDate)) < 0.001 } == true,
            "HC-B05：readExtStatusEntry 应读回持久化时间戳"
        )

        let linesJSON = "[{\"id\":1,\"src\":\"Hallo\",\"dst\":\"你好\",\"session\":\"s1\"}]"
        check(store.writeLatestLines(linesJSON), "writeLatestLines 应成功")
        check(store.readLatestLines() == linesJSON, "readLatestLines 应读回写入内容")

        check(store.writeLatestDraft("Guten"), "writeLatestDraft 应成功")
        check(store.readLatestDraft() == "Guten", "readLatestDraft 应读回写入内容")

        check(store.writeEngineMode("local"), "writeEngineMode 应成功")
        check(store.readEngineMode() == "local", "readEngineMode 应读回 local")

        check(store.writeCloudEnabled(true), "writeCloudEnabled(true) 应成功")
        check(store.readCloudEnabled() == true, "readCloudEnabled 应读回 true")

        // 3. 覆盖写入与旧格式无时间戳兼容（HC-B05）
        check(store.writeExtStatus("finished src=broadcast"), "覆盖 writeExtStatus 应成功")
        check(store.readExtStatus() == "finished src=broadcast", "覆盖后应读回最新值")
        let legacyURL = tempDir.appendingPathComponent(SharedStore.Key.extStatus, isDirectory: false)
        try? Data("blocks=2 src=broadcast".utf8).write(to: legacyURL, options: .atomic)
        let legacyEntry = store.readExtStatusEntry()
        check(legacyEntry?.status == "blocks=2 src=broadcast", "HC-B05：旧格式纯文本 ext_status 应读回原状态文本")
        check(legacyEntry?.updatedAt == nil, "HC-B05：旧格式无时间戳时 updatedAt 应为 nil（未知新鲜度）")
        try? Data("{\"status\":\"blocks=3 src=broadcast\"}".utf8).write(to: legacyURL, options: .atomic)
        let missingTsEntry = store.readExtStatusEntry()
        check(missingTsEntry?.status == "blocks=3 src=broadcast", "HC-B05：缺 updated_at 字段的 JSON 仍应读回状态文本")
        check(missingTsEntry?.updatedAt == nil, "HC-B05：缺 updated_at 字段时 updatedAt 应为 nil")
        check(store.writeExtStatus("finished src=broadcast"), "还原 writeExtStatus 应成功")
        check(store.writeLatestDraft(""), "写空草稿应成功")
        check(store.readLatestDraft() == "", "空草稿应原样读回空串")
        check(store.writeCloudEnabled(false), "覆盖 writeCloudEnabled(false) 应成功")
        check(store.readCloudEnabled() == false, "覆盖后 cloudEnabled 应为 false")

        // 4. 写后文件保护属性键存在且为 completeUntilFirstUserAuthentication
        for key in [
            SharedStore.Key.extStatus,
            SharedStore.Key.latestLines,
            SharedStore.Key.latestDraft,
            SharedStore.Key.engineMode,
            SharedStore.Key.cloudEnabled,
        ] {
            let fileURL = tempDir.appendingPathComponent(key, isDirectory: false)
            let attrs = try? FileManager.default.attributesOfItem(atPath: fileURL.path)
            check(attrs?[.protectionKey] != nil, "写入 \(key) 后文件保护属性键必须存在")
            let protection = attrs?[.protectionKey] as? FileProtectionType
            check(
                protection == .completeUntilFirstUserAuthentication,
                "写入 \(key) 后文件保护级别应为 completeUntilFirstUserAuthentication，实际为 \(String(describing: protection))"
            )
        }

        // 5. 容器为 nil 时读返回 nil、写返回 false，不回退写 Keychain
        final class CallFlag: @unchecked Sendable {
            var readCalled = false
            var deleteCalled = false
        }
        let nilFlags = CallFlag()
        let nilContainer = SharedStore(
            rootURL: nil,
            legacyRead: { _ in
                nilFlags.readCalled = true
                return "node"
            },
            legacyDelete: { _ in
                nilFlags.deleteCalled = true
                return false
            }
        )
        check(nilContainer.writeExtStatus("x") == false, "容器为 nil 时写应返回 false")
        check(nilContainer.readExtStatus() == nil, "容器为 nil 时读应返回 nil")
        check(nilContainer.readEngineMode() == "auto", "容器为 nil 时 engineMode 应退回 auto")
        check(nilContainer.readCloudEnabled() == false, "容器为 nil 时 cloudEnabled 应退回 false")
        check(nilFlags.readCalled == false, "容器为 nil 时不应读旧 Keychain")
        check(nilFlags.deleteCalled == false, "容器为 nil 时不应删旧 Keychain")

        // 6. 「如何处理旧 Keychain」纯函数判定
        check(
            SharedStore.shouldMigrateFromKeychain(
                key: SharedStore.Key.engineMode, fileExists: false, legacyValue: "node"
            ) == .migrateValue,
            "engine_mode 文件不存在且 Keychain 有值时应迁移值"
        )
        check(
            SharedStore.shouldMigrateFromKeychain(
                key: SharedStore.Key.extStatus, fileExists: false, legacyValue: "old"
            ) == .deleteOnly,
            "ext_status 旧值只应删除，不应迁移"
        )
        check(
            SharedStore.shouldMigrateFromKeychain(
                key: SharedStore.Key.latestLines, fileExists: false, legacyValue: nil
            ) == .deleteOnly,
            "latest_lines 即使旧值为空也应只删除旧项"
        )
        check(
            SharedStore.shouldMigrateFromKeychain(
                key: SharedStore.Key.engineMode, fileExists: false, legacyValue: nil
            ) == .none,
            "设置键没有旧值时不应迁移或删除"
        )
        check(
            SharedStore.shouldMigrateFromKeychain(
                key: SharedStore.Key.cloudEnabled, fileExists: true, legacyValue: "1"
            ) == .none,
            "文件已存在时不应处理旧 Keychain"
        )
        check(
            SharedStore.shouldMigrateFromKeychain(
                key: "unknown", fileExists: false, legacyValue: "old"
            ) == .none,
            "未知键不应处理旧 Keychain"
        )

        // 7. 一次性迁移流程（注入内存字典模拟 Keychain）
        let migrateDir = FileManager.default.temporaryDirectory
            .appendingPathComponent("sharedstore-migrate-\(UUID().uuidString)", isDirectory: true)
        defer { try? FileManager.default.removeItem(at: migrateDir) }

        final class LegacyBox: @unchecked Sendable {
            var values: [String: String] = [
                SharedStore.Key.extStatus: "stale status",
                SharedStore.Key.latestLines: "stale lines",
                SharedStore.Key.latestDraft: "stale draft",
                SharedStore.Key.engineMode: "node",
                SharedStore.Key.cloudEnabled: "1",
            ]
            var readCount = 0
            var deletedKeys: [String] = []
        }
        let legacyBox = LegacyBox()
        let migratingStore = SharedStore(
            rootURL: migrateDir,
            legacyRead: { key in
                legacyBox.readCount += 1
                return legacyBox.values[key]
            },
            legacyDelete: { key in
                legacyBox.deletedKeys.append(key)
                legacyBox.values.removeValue(forKey: key)
                return true
            }
        )

        check(migratingStore.readExtStatus() == nil, "运行态 ext_status 旧值不应迁移")
        check(migratingStore.readLatestLines() == nil, "运行态 latest_lines 旧值不应迁移")
        check(migratingStore.readLatestDraft() == nil, "运行态 latest_draft 旧值不应迁移")
        check(
            Set(legacyBox.deletedKeys) == Set([
                SharedStore.Key.extStatus,
                SharedStore.Key.latestLines,
                SharedStore.Key.latestDraft,
            ]),
            "运行态旧 Keychain 项应删除且不落文件"
        )
        let readsAfterRuntime = legacyBox.readCount
        check(migratingStore.readExtStatus() == nil, "运行态键第二次读仍应返回 nil")
        check(legacyBox.readCount == readsAfterRuntime, "同一运行态键第二次读不应再查 Keychain")

        check(migratingStore.readEngineMode() == "node", "首次读缺失键应从旧 Keychain 迁出 node")
        check(legacyBox.deletedKeys.contains(SharedStore.Key.engineMode), "迁移后应删除 Keychain 旧项")
        check(
            FileManager.default.fileExists(atPath: migrateDir.appendingPathComponent(SharedStore.Key.engineMode).path),
            "迁移后 SharedStore 文件应落地"
        )
        let readsAfterFirst = legacyBox.readCount
        check(migratingStore.readEngineMode() == "node", "二次读取应直接读 SharedStore 文件")
        check(legacyBox.readCount == readsAfterFirst, "文件已存在时不应再读 Keychain")

        check(migratingStore.readCloudEnabled(), "首次读缺失键应从旧 Keychain 迁出 cloud_enabled")
        check(legacyBox.deletedKeys.contains(SharedStore.Key.cloudEnabled), "cloud_enabled 迁移后应删除旧项")

        if failures.isEmpty {
            print("SHARED STORE SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
