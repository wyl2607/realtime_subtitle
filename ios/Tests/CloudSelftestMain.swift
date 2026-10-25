import Foundation

/// 云端档纯逻辑的独立自测入口。单独编一个 @main 是为了不和 NodeSelftest / RoutingSelftest
/// 争入口，三个入口互不影响。全程不发网络请求。
@main
struct CloudSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        runCloudClientSelftest(check)
        runQuotaLedgerSelftest(check)
        runCloudSegmenterSelftest(check)

        if failures.isEmpty {
            print("SELFTEST OK")
        } else {
            for failure in failures { print("FAIL: \(failure)") }
            exit(1)
        }
    }
}
