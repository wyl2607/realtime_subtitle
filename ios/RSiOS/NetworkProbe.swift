import Foundation
import Network
import Observation

/// 诊断页的双网对比：分别经 Wi-Fi 和蜂窝连一次 Groq（TCP+TLS 握手），量建连耗时。
///
/// 只在诊断页可见时每 10 秒测一次：握手几 KB 流量，不发任何音频和 key。
@MainActor
@Observable
final class NetworkProbe {
    struct Reading: Equatable {
        var available: Bool
        var millis: Int?
    }

    private(set) var wifi = Reading(available: false, millis: nil)
    private(set) var cellular = Reading(available: false, millis: nil)
    private(set) var cellularConstrained = false
    private(set) var proxyConfigured = false

    func run() async {
        while !Task.isCancelled {
            let paths = NetworkPaths.shared.snapshot
            cellularConstrained = paths.cellularConstrained
            proxyConfigured = CellularFallback.systemProxyConfigured()
            async let wifiMs = paths.wifi ? Self.handshakeMillis(over: .wifi) : nil
            async let cellularMs = paths.cellular ? Self.handshakeMillis(over: .cellular) : nil
            let (w, c) = await (wifiMs, cellularMs)
            wifi = Reading(available: paths.wifi, millis: w)
            cellular = Reading(available: paths.cellular, millis: c)
            try? await Task.sleep(for: .seconds(10))
        }
    }

    nonisolated static func handshakeMillis(over interface: NWInterface.InterfaceType) async -> Int? {
        let parameters = NWParameters(tls: NWProtocolTLS.Options(), tcp: NWProtocolTCP.Options())
        parameters.requiredInterfaceType = interface
        let connection = NWConnection(host: "api.groq.com", port: .https, using: parameters)
        let once = ProbeOnce()
        let started = ContinuousClock.now
        return await withCheckedContinuation { (continuation: CheckedContinuation<Int?, Never>) in
            let finish: @Sendable (Int?) -> Void = { value in
                guard once.claim() else { return }
                connection.stateUpdateHandler = nil
                connection.cancel()
                continuation.resume(returning: value)
            }
            connection.stateUpdateHandler = { state in
                switch state {
                case .ready:
                    let elapsed = ContinuousClock.now - started
                    finish(Int(elapsed.components.seconds * 1000 + elapsed.components.attoseconds / 1_000_000_000_000_000))
                case .waiting, .failed, .cancelled:
                    finish(nil)
                default:
                    break
                }
            }
            connection.start(queue: .global(qos: .utility))
            DispatchQueue.global(qos: .utility).asyncAfter(deadline: .now() + 5) { finish(nil) }
        }
    }
}

private final class ProbeOnce: @unchecked Sendable {
    private let lock = NSLock()
    private var claimed = false
    func claim() -> Bool {
        lock.withLock {
            defer { claimed = true }
            return !claimed
        }
    }
}
