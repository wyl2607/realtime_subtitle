import Foundation

/// 探测单次握手的状态闸门：保证 continuation 恰好恢复一次，且任务取消时立即释放连接并结束等待。
///
/// `withTaskCancellationHandler` 的 `onCancel` 可能在 `withCheckedContinuation` 绑定前触发，
/// 也可能与连接回调或超时并发到达；所有状态转换收进同一把锁，保证不漏恢复也不重复恢复。
final class NetworkProbeGate: @unchecked Sendable {
    private enum State {
        case pending
        case cancelledBeforeActivate
        case active(
            resume: @Sendable (Int?) -> Void,
            cancelConnection: @Sendable () -> Void,
            cancelTimeout: @Sendable () -> Void
        )
        case finished
    }

    private let lock = NSLock()
    private var state: State = .pending

    var isFinished: Bool {
        lock.withLock {
            if case .finished = state { return true }
            return false
        }
    }

    /// 绑定 continuation 与清理回调。若此前已取消，立即清理并以 nil 恢复，返回 false 阻止启动连接。
    func activate(
        resume: @escaping @Sendable (Int?) -> Void,
        cancelConnection: @escaping @Sendable () -> Void,
        cancelTimeout: @escaping @Sendable () -> Void
    ) -> Bool {
        enum Action {
            case started
            case cancelledImmediately
            case ignored
        }

        let action = lock.withLock { () -> Action in
            switch state {
            case .pending:
                state = .active(
                    resume: resume,
                    cancelConnection: cancelConnection,
                    cancelTimeout: cancelTimeout
                )
                return .started
            case .cancelledBeforeActivate:
                state = .finished
                return .cancelledImmediately
            case .active, .finished:
                return .ignored
            }
        }

        switch action {
        case .started:
            return true
        case .cancelledImmediately:
            cancelTimeout()
            cancelConnection()
            resume(nil)
            return false
        case .ignored:
            return false
        }
    }

    /// 连接就绪、失败或超时时调用；只有首次完成会执行清理并恢复 continuation。
    func finish(_ millis: Int?) {
        settle(returning: millis, allowBeforeActivate: false)
    }

    /// 任务取消时立即调用；若尚未 activate，记下取消状态以便 activate 立即结束。
    func cancel() {
        settle(returning: nil, allowBeforeActivate: true)
    }

    private func settle(returning millis: Int?, allowBeforeActivate: Bool) {
        let cleanup: (@Sendable () -> Void, @Sendable () -> Void, @Sendable (Int?) -> Void)? = lock.withLock {
            switch state {
            case .pending:
                if allowBeforeActivate {
                    state = .cancelledBeforeActivate
                }
                return nil
            case .active(let resume, let cancelConnection, let cancelTimeout):
                state = .finished
                return (cancelTimeout, cancelConnection, resume)
            case .cancelledBeforeActivate, .finished:
                return nil
            }
        }
        guard let (cancelTimeout, cancelConnection, resume) = cleanup else { return }
        cancelTimeout()
        cancelConnection()
        resume(millis)
    }
}
