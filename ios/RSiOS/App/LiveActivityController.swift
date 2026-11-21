import ActivityKit
import Foundation

/// 熄屏后的字幕出口：锁屏 / 灵动岛实时活动。
///
/// 只在主 App 里管：广播扩展不能开实时活动。开始采集时（麦克风或广播，App 此刻在前台）
/// 开一次，之后跟着最新句对更新，采集结束就关掉。
@MainActor
final class LiveActivityController {
    private var activity: Activity<SubtitleActivityAttributes>?
    private var lastPushed: SubtitleActivityAttributes.ContentState?
    private var lastPushAt = Date.distantPast
    private var trailingUpdate: Task<Void, Never>?

    /// 系统给实时活动的更新预算有限：句子来得再快也最多每秒推一次，
    /// 中间的变化合并成最后一次（尾随更新），不丢最后一句。
    private static let minInterval: TimeInterval = 1

    func sync(capturing: Bool, sourceName: String, state: SubtitleActivityAttributes.ContentState) {
        guard capturing else {
            end(with: state)
            return
        }
        if activity == nil {
            start(sourceName: sourceName, state: state)
            return
        }
        guard state != lastPushed else { return }
        let wait = Self.minInterval - Date().timeIntervalSince(lastPushAt)
        trailingUpdate?.cancel()
        if wait <= 0 {
            push(state)
        } else {
            trailingUpdate = Task { [weak self] in
                try? await Task.sleep(for: .seconds(wait))
                guard !Task.isCancelled else { return }
                self?.push(state)
            }
        }
    }

    private func start(sourceName: String, state: SubtitleActivityAttributes.ContentState) {
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        do {
            activity = try Activity.request(
                attributes: SubtitleActivityAttributes(sourceName: sourceName),
                content: ActivityContent(state: state, staleDate: nil)
            )
            lastPushed = state
            lastPushAt = Date()
        } catch {
            // 用户在系统设置里关了实时活动、或 App 不在前台：没有锁屏字幕，采集照常继续。
            activity = nil
        }
    }

    private func push(_ state: SubtitleActivityAttributes.ContentState) {
        guard let activity else { return }
        lastPushed = state
        lastPushAt = Date()
        let handle = ActivityHandle(activity: activity)
        Task.detached { await handle.activity.update(ActivityContent(state: state, staleDate: nil)) }
    }

    private func end(with state: SubtitleActivityAttributes.ContentState) {
        trailingUpdate?.cancel()
        trailingUpdate = nil
        guard let activity else { return }
        self.activity = nil
        lastPushed = nil
        let handle = ActivityHandle(activity: activity)
        Task.detached { await handle.activity.end(ActivityContent(state: state, staleDate: nil), dismissalPolicy: .default) }
    }
}

/// Activity 没标 Sendable，但 update/end 是 ActivityKit 自己串行处理的系统调用；
/// 句柄只读、只传给这两个调用，可以 unchecked。
private struct ActivityHandle: @unchecked Sendable {
    let activity: Activity<SubtitleActivityAttributes>
}
