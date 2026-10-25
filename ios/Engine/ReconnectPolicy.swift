import Foundation

/// 重连策略：根据关闭码决定是否重连、等待多久。
/// 所有共享状态由调用方在 `stateQueue` 上保护，本类型自身无锁、无副作用。
struct ReconnectPolicy {
    private var attempt = 0
    private let maxBackoff: TimeInterval = 30

    /// 根据关闭码计算下一次重连延迟。
    /// - Parameter closeCode: WebSocket 关闭码（已由 `NodeEvent.closeReason` 还原 1005→真实码）。
    /// - Returns: 延迟秒数；`nil` 表示不重连。
    mutating func next(closeCode: Int) -> TimeInterval? {
        // 1008/1003/1009：协议或配置错误，不重连
        if closeCode == 1008 || closeCode == 1003 || closeCode == 1009 {
            return nil
        }

        // 1013：节点忙，固定 10 秒
        if closeCode == 1013 {
            attempt += 1
            return 10
        }

        // 其它（1000、1006、1011 等）：指数退避 1,2,4,8,16,30,30...
        let delays: [TimeInterval] = [1, 2, 4, 8, 16, 30]
        let delay = attempt < delays.count ? delays[attempt] : maxBackoff
        attempt += 1
        return delay
    }

    /// 收到 `ready` 事件后调用，重置退避计数。
    mutating func reset() {
        attempt = 0
    }
}