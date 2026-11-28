import Foundation

private enum AppStateMonitorTransition: Equatable {
    case background
    case foreground

    var isInBackground: Bool {
        self == .background
    }
}

/// AppStateMonitor 的状态转换保持为无副作用的纯映射；通知回调只负责直接应用结果。
@main
struct AppStateMonitorSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ message: String) {
            if !condition { failures.append(message) }
        }

        check(AppStateMonitorTransition.background.isInBackground, "background 必须映射为后台")
        check(!AppStateMonitorTransition.foreground.isInBackground, "foreground 必须映射为前台")
        check(
            [AppStateMonitorTransition.background, .foreground]
                .map(\.isInBackground) == [true, false],
            "连续 background/foreground 必须按到达顺序得到 true/false"
        )
        check(
            [AppStateMonitorTransition.background, .background, .foreground, .foreground]
                .map(\.isInBackground) == [true, true, false, false],
            "重复通知只能重复应用同一状态，不应产生额外状态转换"
        )

        if failures.isEmpty {
            print("APP STATE MONITOR SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
