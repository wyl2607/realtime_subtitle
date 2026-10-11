import Darwin
import Foundation

/// 节点连接只许落在 Tailscale 地址（100.64.0.0/10）上，并且钉成 IP 字面量再连。
///
/// 为什么：nodes.json 里写的是主机名（ws://mini2:8791）。只按主机名连，换到一个陌生 Wi-Fi、
/// 它的 DNS 恰好应答 "mini2"，bearer token 和音频就会明文发给别人（ws:// 没有 TLS）。
/// 先解析、校验网段、再用 IP 连，和 Python 节点客户端的做法一致（CLAUDE.md 第 4 节第 47 条）。
/// 代理已由 directSessionConfiguration 的空代理字典关掉。
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

    static func selfTest() -> [String] {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) { if !ok { failures.append(name) } }
        check(isTailnetIPv4("100.64.0.1"), "100.64.0.1 属于 tailnet")
        check(isTailnetIPv4("100.127.255.254"), "100.127.255.254 属于 tailnet")
        check(!isTailnetIPv4("100.128.0.1"), "100.128.0.1 不属于 tailnet")
        check(!isTailnetIPv4("100.63.255.255"), "100.63.255.255 不属于 tailnet")
        check(!isTailnetIPv4("192.168.1.10"), "私网地址不属于 tailnet")
        check(!isTailnetIPv4("mini2"), "主机名不是 IPv4")
        let url = URL(string: "ws://mini2:8791/v2/session")!
        let ok = try? pinned(url, resolve: { _ in ["100.101.102.103"] })
        check(ok?.absoluteString == "ws://100.101.102.103:8791/v2/session", "tailnet 地址应钉成 IP 并保留端口路径")
        check((try? pinned(url, resolve: { _ in ["203.0.113.5"] })) == nil, "公网地址应拒绝")
        check((try? pinned(url, resolve: { _ in ["100.101.102.103", "192.168.0.2"] })) == nil, "混有非 tailnet 地址应拒绝")
        check((try? pinned(url, resolve: { _ in [] })) == nil, "解析失败应拒绝")
        check((try? pinned(URL(string: "http://100.70.1.2:8791/v1/info")!, resolve: { _ in [] }))?.host == "100.70.1.2", "tailnet IP 字面量直接通过")
        check((try? pinned(URL(string: "http://10.0.0.2:8791/v1/info")!, resolve: { _ in [] })) == nil, "非 tailnet IP 字面量拒绝")
        return failures
    }
}
