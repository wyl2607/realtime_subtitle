import Foundation

/// 硬约束 HC-B01 的行为面：NetworkProbeGate 保证 continuation 恰好恢复一次、取消即结束且立即清理连接。
@main
struct NetworkProbeSelftest {
    private final class Recorder: @unchecked Sendable {
        private let lock = NSLock()
        private var resumedValues: [Int?] = []
        private var connectionCancelCount = 0
        private var timeoutCancelCount = 0
        private var started = false

        var resumeCount: Int { lock.withLock { resumedValues.count } }
        var lastValue: Int?? { lock.withLock { resumedValues.last } }
        var cancelConnectionCount: Int { lock.withLock { connectionCancelCount } }
        var cancelTimeoutCount: Int { lock.withLock { timeoutCancelCount } }
        var didStart: Bool { lock.withLock { started } }

        func recordResume(_ value: Int?) {
            lock.withLock { resumedValues.append(value) }
        }

        func recordCancelConnection() {
            lock.withLock { connectionCancelCount += 1 }
        }

        func recordCancelTimeout() {
            lock.withLock { timeoutCancelCount += 1 }
        }

        func markStarted() {
            lock.withLock { started = true }
        }
    }

    private static func runAsyncProbe(
        timeoutSeconds: TimeInterval,
        recorder: Recorder,
        onActivated: (@Sendable (NetworkProbeGate) -> Void)? = nil
    ) async -> Int? {
        let gate = NetworkProbeGate()
        return await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Int?, Never>) in
                let timeoutTask = Task { [weak gate] in
                    try? await Task.sleep(for: .seconds(timeoutSeconds))
                    guard !Task.isCancelled else { return }
                    gate?.finish(nil)
                }
                guard gate.activate(
                    resume: { value in
                        recorder.recordResume(value)
                        continuation.resume(returning: value)
                    },
                    cancelConnection: { recorder.recordCancelConnection() },
                    cancelTimeout: {
                        recorder.recordCancelTimeout()
                        timeoutTask.cancel()
                    }
                ) else {
                    return
                }
                recorder.markStarted()
                onActivated?(gate)
            }
        } onCancel: {
            gate.cancel()
        }
    }

    static func main() async {
        var failures: [String] = []
        func check(_ condition: Bool, _ name: String) {
            if !condition { failures.append(name) }
        }

        // 1. 状态机：成功路径 + 后续重复 finish / cancel 不得二次恢复
        do {
            let gate = NetworkProbeGate()
            let rec = Recorder()
            let activated = gate.activate(
                resume: { rec.recordResume($0) },
                cancelConnection: { rec.recordCancelConnection() },
                cancelTimeout: { rec.recordCancelTimeout() }
            )
            check(activated, "未取消时 activate 应返回 true")
            gate.finish(42)
            gate.finish(99)
            gate.cancel()
            check(rec.resumeCount == 1, "成功路径 continuation 应恰好恢复 1 次（实际 \(rec.resumeCount)）")
            check(rec.lastValue == .some(42), "成功路径应返回耗时 42 ms")
            check(rec.cancelConnectionCount == 1, "成功路径应清理连接 1 次")
            check(rec.cancelTimeoutCount == 1, "成功路径应取消超时 1 次")
            check(gate.isFinished, "完成后 isFinished 应为 true")
        }

        // 2. 状态机：失败路径
        do {
            let gate = NetworkProbeGate()
            let rec = Recorder()
            _ = gate.activate(
                resume: { rec.recordResume($0) },
                cancelConnection: { rec.recordCancelConnection() },
                cancelTimeout: { rec.recordCancelTimeout() }
            )
            gate.finish(nil)
            gate.finish(10)
            check(rec.resumeCount == 1, "失败路径 continuation 应恰好恢复 1 次")
            check(rec.lastValue == .some(nil), "失败路径应返回 nil")
            check(rec.cancelConnectionCount == 1, "失败路径应清理连接 1 次")
            check(rec.cancelTimeoutCount == 1, "失败路径应取消超时 1 次")
        }

        // 3. 状态机：运行中取消路径
        do {
            let gate = NetworkProbeGate()
            let rec = Recorder()
            _ = gate.activate(
                resume: { rec.recordResume($0) },
                cancelConnection: { rec.recordCancelConnection() },
                cancelTimeout: { rec.recordCancelTimeout() }
            )
            gate.cancel()
            gate.finish(88)
            gate.cancel()
            check(rec.resumeCount == 1, "运行中取消 continuation 应恰好恢复 1 次（实际 \(rec.resumeCount)）")
            check(rec.lastValue == .some(nil), "运行中取消应返回 nil")
            check(rec.cancelConnectionCount == 1, "运行中取消应立即 cancel 连接 1 次")
            check(rec.cancelTimeoutCount == 1, "运行中取消应立即取消超时 1 次")
        }

        // 4. 状态机：activate 之前已取消，不得启动连接且立刻恢复 nil
        do {
            let gate = NetworkProbeGate()
            let rec = Recorder()
            gate.cancel()
            let activated = gate.activate(
                resume: { rec.recordResume($0) },
                cancelConnection: { rec.recordCancelConnection() },
                cancelTimeout: { rec.recordCancelTimeout() }
            )
            check(!activated, "先取消后 activate 应返回 false 阻止启动连接")
            check(rec.resumeCount == 1, "预取消路径 continuation 应恰好恢复 1 次")
            check(rec.lastValue == .some(nil), "预取消路径应返回 nil")
            check(rec.cancelConnectionCount == 1, "预取消路径应执行连接清理 1 次")
            check(rec.cancelTimeoutCount == 1, "预取消路径应执行超时清理 1 次")
            check(gate.isFinished, "预取消后 isFinished 应为 true")
        }

        // 5. 异步四路径（withTaskCancellationHandler + withCheckedContinuation）：成功
        do {
            let rec = Recorder()
            let result = await runAsyncProbe(timeoutSeconds: 5, recorder: rec) { gate in
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.01) {
                    gate.finish(37)
                }
            }
            check(result == 37, "异步成功路径应返回 37 ms")
            check(rec.didStart, "异步成功路径应启动连接")
            check(rec.resumeCount == 1, "异步成功路径 continuation 应恢复 1 次")
            check(rec.cancelConnectionCount == 1, "异步成功路径应清理连接 1 次")
        }

        // 6. 异步四路径：失败
        do {
            let rec = Recorder()
            let result = await runAsyncProbe(timeoutSeconds: 5, recorder: rec) { gate in
                DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 0.01) {
                    gate.finish(nil)
                }
            }
            check(result == nil, "异步失败路径应返回 nil")
            check(rec.resumeCount == 1, "异步失败路径 continuation 应恢复 1 次")
            check(rec.cancelConnectionCount == 1, "异步失败路径应清理连接 1 次")
        }

        // 7. 异步四路径：超时
        do {
            let rec = Recorder()
            let result = await runAsyncProbe(timeoutSeconds: 0.03, recorder: rec)
            check(result == nil, "异步超时路径应返回 nil")
            check(rec.resumeCount == 1, "异步超时路径 continuation 应恢复 1 次")
            check(rec.cancelConnectionCount == 1, "异步超时路径应清理连接 1 次")
        }

        // 8. 异步四路径：运行中取消（必须立即 cancel 连接、不再等 5 秒）
        do {
            let rec = Recorder()
            let startedAt = ContinuousClock.now
            let task = Task {
                await runAsyncProbe(timeoutSeconds: 5, recorder: rec)
            }
            while !rec.didStart {
                await Task.yield()
            }
            task.cancel()
            let result = await task.value
            let elapsed = ContinuousClock.now - startedAt
            check(result == nil, "异步取消路径应返回 nil")
            check(rec.didStart, "异步取消用例应确认在连接开始后触发取消")
            check(rec.resumeCount == 1, "异步取消路径 continuation 应恰好恢复 1 次（实际 \(rec.resumeCount)）")
            check(rec.cancelConnectionCount == 1, "异步取消路径应立即 cancel 连接 1 次（实际 \(rec.cancelConnectionCount)）")
            check(rec.cancelTimeoutCount == 1, "异步取消路径应取消 5 秒超时 1 次")
            check(elapsed < .seconds(1), "任务取消后必须立即结束，不应等 5 秒超时（实际 \(elapsed)）")
        }

        // 8b. 异步取消：任务在进入探针前已取消，不得启动连接
        do {
            let rec = Recorder()
            let task = Task {
                await Task.yield()
                return await runAsyncProbe(timeoutSeconds: 5, recorder: rec)
            }
            task.cancel()
            let result = await task.value
            check(result == nil, "预取消异步路径应返回 nil")
            check(!rec.didStart, "预取消异步路径不得启动连接")
            check(rec.resumeCount == 1, "预取消异步路径 continuation 应恰好恢复 1 次（实际 \(rec.resumeCount)）")
            check(rec.cancelConnectionCount == 1, "预取消异步路径应清理连接 1 次")
        }

        // 9. 并发竞态压测：finish 与 cancel 同时触发时，continuation 必须恰好恢复一次
        for i in 0..<200 {
            let gate = NetworkProbeGate()
            let rec = Recorder()
            _ = gate.activate(
                resume: { rec.recordResume($0) },
                cancelConnection: { rec.recordCancelConnection() },
                cancelTimeout: { rec.recordCancelTimeout() }
            )
            await withTaskGroup(of: Void.self) { group in
                group.addTask { gate.finish(i) }
                group.addTask { gate.cancel() }
            }
            if rec.resumeCount != 1 || rec.cancelConnectionCount != 1 || rec.cancelTimeoutCount != 1 {
                failures.append("并发竞态第 \(i) 轮失败：resume=\(rec.resumeCount) cancelConn=\(rec.cancelConnectionCount) cancelTimeout=\(rec.cancelTimeoutCount)")
                break
            }
        }

        if failures.isEmpty {
            print("NETWORK PROBE SELFTEST OK")
        } else {
            failures.forEach { print("FAIL: \($0)") }
            exit(1)
        }
    }
}
