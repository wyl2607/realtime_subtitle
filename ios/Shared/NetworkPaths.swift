import Foundation
import Network

/// Wi-Fi 和蜂窝两条路各自是否可用。
///
/// 为什么两条分开盯：系统默认路径只告诉你「现在走哪条」，看不出另一条还在不在。
/// 云端上传在 Wi-Fi 上失败时要换蜂窝重发，前提是知道蜂窝此刻可用（用户可能关了蜂窝数据）。
final class NetworkPaths: @unchecked Sendable {
    static let shared = NetworkPaths()
    static let didChangeNotification = Notification.Name("rs.networkPathsChanged")

    private let lock = NSLock()
    private var state = NetworkPathSnapshot()
    private let wifiMonitor = NWPathMonitor(requiredInterfaceType: .wifi)
    private let cellularMonitor = NWPathMonitor(requiredInterfaceType: .cellular)
    private let queue = DispatchQueue(label: "rs.network-paths")

    private init() {
        wifiMonitor.pathUpdateHandler = { [weak self] path in
            self?.update { $0.wifi = path.status == .satisfied }
        }
        cellularMonitor.pathUpdateHandler = { [weak self] path in
            self?.update {
                $0.cellular = path.status == .satisfied
                $0.cellularConstrained = path.isConstrained
            }
        }
        wifiMonitor.start(queue: queue)
        cellularMonitor.start(queue: queue)
    }

    var snapshot: NetworkPathSnapshot { lock.withLock { state } }

    private func update(_ mutate: (inout NetworkPathSnapshot) -> Void) {
        let changed = lock.withLock { () -> Bool in
            let before = state
            mutate(&state)
            return state != before
        }
        guard changed else { return }
        DispatchQueue.main.async { NotificationCenter.default.post(name: Self.didChangeNotification, object: nil) }
    }
}

struct NetworkPathSnapshot: Equatable, Sendable {
    var wifi = false
    var cellular = false
    /// 低数据模式：照样能重发，只在诊断页提示。
    var cellularConstrained = false
}

/// 云端上传要不要换蜂窝重发一次。纯函数，自测直接验。
enum CellularFallback {
    static func shouldRetry(after error: CloudClientError, paths: NetworkPathSnapshot, systemProxyConfigured: Bool) -> Bool {
        // 只有「连不上/超时」才换路：HTTP 4xx/5xx 是 Groq 说了话，换网卡也一样。
        guard case .network(let type) = error, type != "cancelled" else { return false }
        // 两条都在才有意义：没 Wi-Fi 时原请求本来就走的蜂窝。
        // 开了系统代理不重发：直连会绕过代理，而有的用户只能经代理到 Groq（CLAUDE.md 第 4 节第 47 条）。
        return paths.wifi && paths.cellular && !systemProxyConfigured
    }

    static func systemProxyConfigured() -> Bool {
        guard let settings = CFNetworkCopySystemProxySettings()?.takeRetainedValue() as? [String: Any] else {
            return false
        }
        return ["HTTPEnable", "HTTPSEnable", "ProxyAutoConfigEnable", "SOCKSEnable"].contains {
            (settings[$0] as? NSNumber)?.boolValue == true
        }
    }
}
