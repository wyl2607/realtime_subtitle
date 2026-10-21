import Foundation

/// P5 合并自测：同一份 `p5_cases.json` 在 mac rslite 和 iOS 上都要通过。
func runLineStoreSelftest(_ check: @escaping (Bool, String) -> Void) {
    do {
        let fixture: P5Fixture = try SelftestFixture.load("p5_cases.json")
        let failures = fixture.p5Cases.flatMap { p5Failures($0) }
        check(failures.isEmpty, "p5_cases 失败: \(failures.joined(separator: "; "))")
        check(fixture.p5Cases.count == 5, "p5_cases 数量应为 5（实际 \(fixture.p5Cases.count)）")
        if failures.isEmpty {
            print("p5_cases: \(fixture.p5Cases.count)/\(fixture.p5Cases.count)")
        }
    } catch {
        check(false, "p5_cases 读取失败 \(error)")
    }
}

private func p5Failures(_ testCase: P5Case) -> [String] {
    var store = LineStore()
    var failures: [String] = []
    for operation in testCase.operations {
        switch operation.type {
        case "add_local":
            store.addLocal(key: operation.key ?? 0, t0: operation.t0, t1: operation.t1, text: operation.src ?? "")
        case "apply_remote":
            let replaced = store.applyNode(
                nodeID: operation.nodeID ?? "cloud:groq",
                t0: operation.t0,
                t1: operation.t1,
                srcText: operation.src ?? "",
                dstText: operation.dst
            )
            if let expected = operation.expectReplaced, replaced != expected {
                failures.append("\(testCase.name) replaced expected \(expected) got \(replaced)")
            }
        default:
            failures.append("\(testCase.name) unknown operation \(operation.type)")
        }
    }
    if store.lines.count != testCase.expect.lines.count {
        failures.append("\(testCase.name) lines count expected \(testCase.expect.lines.count) got \(store.lines.count)")
    }
    let actualLines = filtered(store.lines, keeping: testCase.expect.lines)
    if actualLines != testCase.expect.lines {
        failures.append("\(testCase.name) lines expected \(testCase.expect.lines) got \(actualLines)")
    }
    let visibleLimit = max(testCase.visibleLimit ?? store.lines.count, 0)
    let visible = Array(store.lines.suffix(visibleLimit))
    if visible.count != testCase.expect.visible.count {
        failures.append("\(testCase.name) visible count expected \(testCase.expect.visible.count) got \(visible.count)")
    }
    let actualVisible = filtered(visible, keeping: testCase.expect.visible)
    if actualVisible != testCase.expect.visible {
        failures.append("\(testCase.name) visible expected \(testCase.expect.visible) got \(actualVisible)")
    }
    return failures
}

private func filtered(
    _ lines: [SubtitleLine],
    keeping expected: [[String: FixtureValue]]
) -> [[String: FixtureValue]] {
    zip(lines, expected).map { line, expectedLine in
        let full = serialise(line)
        return expectedLine.reduce(into: [:]) { result, item in
            result[item.key] = full[item.key] ?? .null
        }
    }
}

private func serialise(_ line: SubtitleLine) -> [String: FixtureValue] {
    let source: String
    switch line.source {
    case .local:
        source = "local"
    case .node:
        source = "remote"
    }
    return [
        "source": .string(source),
        "key": .number(Double(line.key)),
        "src": .string(line.srcText),
        "dst": line.dstText.map { .string($0) } ?? .null,
        "t0": line.t0.map { .number($0) } ?? .null,
        "t1": line.t1.map { .number($0) } ?? .null,
    ]
}

struct P5Fixture: Decodable {
    var version: Int
    var p5Cases: [P5Case]

    enum CodingKeys: String, CodingKey {
        case version
        case p5Cases = "p5_cases"
    }
}

struct P5Case: Decodable {
    var name: String
    var visibleLimit: Int?
    var operations: [P5Operation]
    var expect: P5Expect

    enum CodingKeys: String, CodingKey {
        case name
        case visibleLimit = "visible_limit"
        case operations
        case expect
    }
}

struct P5Operation: Decodable {
    var type: String
    var key: Int?
    var nodeID: String?
    var t0: Double?
    var t1: Double?
    var src: String?
    var dst: String?
    var expectReplaced: [Int]?

    enum CodingKeys: String, CodingKey {
        case type
        case key
        case nodeID = "node_id"
        case t0
        case t1
        case src
        case dst
        case expectReplaced = "expect_replaced"
    }
}

struct P5Expect: Decodable {
    var lines: [[String: FixtureValue]]
    var visible: [[String: FixtureValue]]
}
