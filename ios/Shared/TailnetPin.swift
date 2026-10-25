import Darwin
import Foundation

/// 节点连接只许落在 Tailscale 地址（100.64.0.0/10）上，并且钉成 IP 字面量再连。
enum TailnetPin {
    enum PinError: Error, CustomStringConvertible {
        case unresolved(String)
        case notTailnet(String)

        var description: String {
            switch self {
            case .unresolved(let host): return "节点主机名解析失败：\(host)"
            case .notTailnet(let host): return "节点地址不在 Tailscale 网段（100.64.0.0/10），拒绝连接：\(host)"
            }
        }
    }

    static func isTailnetIPv4(_ text: String) -> Bool {
        var addr = in_addr()
        guard inet_pton(AF_INET, text, &addr) == 1 else { return false }
        let value = UInt32(bigEndian: addr.s_addr)
        return value & 0xFFC0_0000 == 0x6440_0000
    }

    /// 返回把 host 换成已校验 IPv4 字面量的 URL。解析出的任一地址不在 tailnet 网段都拒绝。
    static func pinned(_ url: URL, resolve: (String) -> [String] = resolveIPv4) throws -> URL {
        guard let host = url.host, !host.isEmpty, var components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            throw PinError.unresolved(url.absoluteString)
        }
        let addresses = isIPv4Literal(host) ? [host] : resolve(host)
        guard let first = addresses.first else {
            throw PinError.unresolved(host)
        }
        guard addresses.allSatisfy(isTailnetIPv4) else {
            throw PinError.notTailnet(host)
        }
        components.host = first
        guard let pinned = components.url else {
            throw PinError.unresolved(host)
        }
        return pinned
    }

    static func isIPv4Literal(_ text: String) -> Bool {
        var addr = in_addr()
        return inet_pton(AF_INET, text, &addr) == 1
    }

    static func resolveIPv4(_ host: String) -> [String] {
        var hints = addrinfo()
        hints.ai_family = AF_INET
        hints.ai_socktype = SOCK_STREAM
        var result: UnsafeMutablePointer<addrinfo>?
        guard getaddrinfo(host, nil, &hints, &result) == 0, let head = result else {
            return []
        }
        defer { freeaddrinfo(head) }
        var out: [String] = []
        var cursor: UnsafeMutablePointer<addrinfo>? = head
        while let info = cursor {
            if let sa = info.pointee.ai_addr, info.pointee.ai_family == AF_INET {
                var sin = sa.withMemoryRebound(to: sockaddr_in.self, capacity: 1) { $0.pointee }
                var buffer = [CChar](repeating: 0, count: Int(INET_ADDRSTRLEN))
                if inet_ntop(AF_INET, &sin.sin_addr, &buffer, socklen_t(INET_ADDRSTRLEN)) != nil {
                    let text = String(cString: buffer)
                    if !out.contains(text) { out.append(text) }
                }
            }
            cursor = info.pointee.ai_next
        }
        return out
    }
}
