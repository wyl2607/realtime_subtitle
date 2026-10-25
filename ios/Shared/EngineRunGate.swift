import Foundation

/// 用单调递增的代号区分每次引擎启动，异步回调只能触碰自己所属的那一轮。
struct EngineRunGate {
    private(set) var generation = 0
    private var activeGeneration: Int?

    /// 新一轮会立即取代上一轮，避免上一轮排队的重连在后来复活。
    mutating func begin() -> Int {
        generation += 1
        activeGeneration = generation
        return generation
    }

    /// 结束后不保留当前轮次，让在途回调只能被丢弃。
    mutating func end() {
        activeGeneration = nil
    }

    func isCurrent(_ generation: Int) -> Bool {
        activeGeneration == generation
    }

    /// 本机档没有节点职责，未知值也按最保守的本机档处理。
    static func mayTouchNode(engineMode: String) -> Bool {
        engineMode == "node" || engineMode == "auto"
    }
}
