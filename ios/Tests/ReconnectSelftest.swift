import Foundation

/// ReconnectPolicy 自测。在 `ios/Tests/NodeSelftest.swift` 入口末尾调用 `runReconnectSelftest()`。
func runReconnectSelftest(_ check: (Bool, String) -> Void) {
    // 退避序列 1,2,4,8,16,30,30（以 1006 为例）
    var policy = ReconnectPolicy()
    let expectedDelays: [TimeInterval] = [1, 2, 4, 8, 16, 30, 30]
    for (i, expected) in expectedDelays.enumerated() {
        let delay = policy.next(closeCode: 1006)
        check(delay == expected, "1006 第 \(i+1) 次退避应为 \(expected)s（实际 \(delay ?? -1)）")
    }

    // reset 后从 1 开始
    policy.reset()
    let afterReset = policy.next(closeCode: 1006)
    check(afterReset == 1, "reset 后首次退避应为 1s（实际 \(afterReset ?? -1)）")

    // 1013 → 固定 10 秒
    var policy1013 = ReconnectPolicy()
    let busyDelay1 = policy1013.next(closeCode: 1013)
    let busyDelay2 = policy1013.next(closeCode: 1013)
    check(busyDelay1 == 10, "1013 首次应为 10s（实际 \(busyDelay1 ?? -1)）")
    check(busyDelay2 == 10, "1013 二次应为 10s（实际 \(busyDelay2 ?? -1)）")

    // 1008/1003/1009 → nil（不重连）
    var policyFatal = ReconnectPolicy()
    check(policyFatal.next(closeCode: 1008) == nil, "1008 不重连")
    check(policyFatal.next(closeCode: 1003) == nil, "1003 不重连")
    check(policyFatal.next(closeCode: 1009) == nil, "1009 不重连")
}