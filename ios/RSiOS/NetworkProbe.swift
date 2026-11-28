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

    nonisolated static let handshakeTimeoutSeconds: TimeInterval = 5

    private(set) var wifi = Reading(available: false, millis: nil)
    private(set) var cellular = Reading(available: false, millis: nil)
    private(set) var cellularConstrained = false
    private(set) var proxyConfigured = false

    func run() async {
        while !Task.isCancelled {
            let paths = NetworkPaths.shared.snapshot
            cellularConstrained = paths.cellularConstrained
            proxyConfigured = CellularFallback.systemProxyConfigured()
            guard !Task.isCancelled else { break }
            async let wifiMs = paths.wifi ? Self.handshakeMillis(over: .wifi) : nil
            async let cellularMs = paths.cellular ? Self.handshakeMillis(over: .cellular) : nil
            let (w, c) = await (wifiMs, cellularMs)
            guard !Task.isCancelled else { break }
            wifi = Reading(available: paths.wifi, millis: w)
            cellular = Reading(available: paths.cellular, millis: c)
            do {
                try await Task.sleep(for: .seconds(10))
            } catch {
                break
            }
        }
    }

    nonisolated static func handshakeMillis(over interface: NWInterface.InterfaceType) async -> Int? {
        guard !Task.isCancelled else { return nil }
        let parameters = NWParameters(tls: NWProtocolTLS.Options(), tcp: NWProtocolTCP.Options())
        parameters.requiredInterfaceType = interface
        let connection = NWConnection(host: "api.groq.com", port: .https, using: parameters)
        let gate = NetworkProbeGate()
        let started = ContinuousClock.now
        return await withTaskCancellationHandler {
            await withCheckedContinuation { (continuation: CheckedContinuation<Int?, Never>) in
                let timeoutTask = Task { [weak gate] in
                    try? await Task.sleep(for: .seconds(handshakeTimeoutSeconds))
                    guard !Task.isCancelled else { return }
                    gate?.finish(nil)
                }
                guard gate.activate(
                    resume: { continuation.resume(returning: $0) },
                    cancelConnection: {
                        connection.stateUpdateHandler = nil
                        connection.cancel()
                    },
                    cancelTimeout: { timeoutTask.cancel() }
                ) else {
                    return
                }
                connection.stateUpdateHandler = { [weak gate] state in
                    switch state {
                    case .ready:
                        let elapsed = ContinuousClock.now - started
                        let millis = Int(
                            elapsed.components.seconds * 1000
                                + elapsed.components.attoseconds / 1_000_000_000_000_000
                        )
                        gate?.finish(millis)
                    case .waiting, .failed, .cancelled:
                        gate?.finish(nil)
                    default:
                        break
                    }
                }
                connection.start(queue: .global(qos: .utility))
                if gate.isFinished {
                    timeoutTask.cancel()
                    connection.stateUpdateHandler = nil
                    connection.cancel()
                }
            }
        } onCancel: {
            gate.cancel()
        }
    }
}
