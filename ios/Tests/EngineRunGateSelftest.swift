import Foundation

/// 轮次闸门不依赖 UI 或网络，命令行必须能锁住旧回调不能穿越新一轮的语义。
@main
struct EngineRunGateSelftest {
    static func main() {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        var gate = EngineRunGate()
        let first = gate.begin()
        check(gate.isCurrent(first), "begin 后本轮代号应有效")

        gate.end()
        check(!gate.isCurrent(first), "end 后旧代号必须失效")

        let second = gate.begin()
        check(gate.isCurrent(second), "新一轮代号应有效")
        check(!gate.isCurrent(first), "新一轮不能让第一轮复活")

        let third = gate.begin()
        check(gate.isCurrent(third), "连续 begin 后最新代号应有效")
        check(!gate.isCurrent(second), "连续 begin 后上一轮代号必须失效")

        check(!EngineRunGate.mayTouchNode(engineMode: "local"), "local 档不能触碰节点")
        check(EngineRunGate.mayTouchNode(engineMode: "node"), "node 档允许节点")
        check(EngineRunGate.mayTouchNode(engineMode: "auto"), "auto 档允许节点")
        check(!EngineRunGate.mayTouchNode(engineMode: "unknown"), "未知档位不能触碰节点")

        if failures.isEmpty {
            print("ENGINE RUN GATE SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
