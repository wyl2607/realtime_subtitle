import Foundation

struct NodeConfig: Codable {
    let id: String
    let host: String
    let port: Int
    let token: String
    let name: String
    let src: String
    let dst: String

    var sessionURL: URL {
        var components = URLComponents()
        components.scheme = "ws"
        components.host = host
        components.port = port
        components.path = "/v2/session"
        return components.url!
    }

    var displayName: String {
        "\(name) (\(host))"
    }
}

enum PairingLink {
    static let scheme = "rsnode"
    static let host = "pair"
    static let requiredVersion = "1"

    /// 扫码或点链接把 App 拉起来时，配对框里预填什么。只认 rsnode://pair，其余返回 nil。
    /// 只预填、不应用：token 写进钥匙串前要用户自己点「应用配对」（与 Android 一致）。
    static func prefillText(for url: URL) -> String? {
        guard url.scheme?.lowercased() == scheme,
              url.host?.lowercased() == host else {
            return nil
        }
        return url.absoluteString
    }

    static func parse(_ string: String) -> NodeConfig? {
        guard let url = URL(string: string),
              let components = URLComponents(url: url, resolvingAgainstBaseURL: false) else {
            return nil
        }

        guard components.scheme == scheme,
              components.host == host else {
            return nil
        }

        let queryItems = components.queryItems ?? []

        guard let v = queryItems.first(where: { $0.name == "v" })?.value,
              v == requiredVersion else {
            return nil
        }

        guard let id = queryItems.first(where: { $0.name == "id" })?.value,
              !id.isEmpty else {
            return nil
        }

        guard let host = queryItems.first(where: { $0.name == "host" })?.value,
              !host.isEmpty,
              isValidHost(host) else {
            return nil
        }

        guard let portStr = queryItems.first(where: { $0.name == "port" })?.value,
              let port = Int(portStr),
              (1...65535).contains(port) else {
            return nil
        }

        guard let token = queryItems.first(where: { $0.name == "tok" })?.value,
              isValidToken(token) else {
            return nil
        }

        guard let name = queryItems.first(where: { $0.name == "name" })?.value,
              !name.isEmpty else {
            return nil
        }

        let src = queryItems.first(where: { $0.name == "src" })?.value ?? "de"
        let dst = queryItems.first(where: { $0.name == "dst" })?.value ?? "zh"

        return NodeConfig(id: id, host: host, port: port, token: token, name: name, src: src, dst: dst)
    }

    private static func isValidHost(_ host: String) -> Bool {
        let pattern = "^[A-Za-z0-9.-]+$"
        return host.range(of: pattern, options: .regularExpression) != nil
    }

    private static func isValidToken(_ token: String) -> Bool {
        guard token.count == 64 else { return false }
        let pattern = "^[0-9a-fA-F]{64}$"
        return token.range(of: pattern, options: .regularExpression) != nil
    }
}