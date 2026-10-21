import Foundation

/// 路由夹具自测入口。iOS 侧没有 XCTest（构建校验用 swiftc 直接编译），
/// 所以这个文件是这批新用例的 @main；其余三个 Selftest 提供函数由它调用。
@main
struct RoutingSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        runRoutingSelftest(check)
        runLineStoreSelftest(check)
        runAutoFallbackSelftest(check)
        runTranscriptStoreSelftest(check)

        if failures.isEmpty {
            print("SELFTEST OK")
        } else {
            for failure in failures { print("FAIL: \(failure)") }
            exit(1)
        }
    }
}

enum SelftestFixture {
    static func load<T: Decodable>(_ name: String) throws -> T {
        let here = URL(fileURLWithPath: #filePath)
            .deletingLastPathComponent()
            .appendingPathComponent("fixtures/\(name)")
        let current = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
        let candidates = [
            here,
            current.appendingPathComponent("ios/Tests/fixtures/\(name)"),
            current.appendingPathComponent("Tests/fixtures/\(name)"),
        ]
        guard let url = candidates.first(where: { FileManager.default.fileExists(atPath: $0.path) }) else {
            throw FixtureError.missing(name)
        }
        let data = try Data(contentsOf: url)
        return try JSONDecoder().decode(T.self, from: data)
    }
}

enum FixtureError: Error, CustomStringConvertible {
    case missing(String)

    var description: String {
        switch self {
        case .missing(let name):
            return "missing fixture \(name)"
        }
    }
}

func runRoutingSelftest(_ check: @escaping (Bool, String) -> Void) {
    do {
        let fixture: RouteFixture = try SelftestFixture.load("route_cases.json")
        let failures = fixture.routeCases.flatMap { routeFailures($0) }
        check(failures.isEmpty, "route_cases 失败: \(failures.joined(separator: "; "))")
        check(fixture.routeCases.count == 16, "route_cases 数量应为 16（实际 \(fixture.routeCases.count)）")
        if failures.isEmpty {
            print("route_cases: \(fixture.routeCases.count)/\(fixture.routeCases.count)")
        }
    } catch {
        check(false, "route_cases 读取失败 \(error)")
    }
}

private func routeFailures(_ testCase: RouteCase) -> [String] {
    var state = CloudRouteContext(testCase.initial)
    var failures: [String] = []
    for (index, step) in testCase.steps.enumerated() {
        state.apply(step.event)
        let actual = state.snapshot(keeping: step.expect)
        if actual != step.expect {
            failures.append("\(testCase.name)#\(index + 1) expected \(step.expect) got \(actual)")
        }
    }
    return failures
}
