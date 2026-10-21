import Foundation

func runPairingSelftest(_ check: (Bool, String) -> Void) {
    let validLink = "rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=8791&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2"
    let validWithLang = "rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=8791&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2&src=de&dst=zh"

    if let config = PairingLink.parse(validLink) {
        check(config.id == "node123", "合法链接 id")
        check(config.host == "mini2.tailnet.ts.net", "合法链接 host")
        check(config.port == 8791, "合法链接 port")
        check(config.token == "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef", "合法链接 token")
        check(config.name == "Mini2", "合法链接 name")
        check(config.src == "de", "合法链接默认 src")
        check(config.dst == "zh", "合法链接默认 dst")
        check(config.sessionURL.absoluteString == "ws://mini2.tailnet.ts.net:8791/v2/session", "sessionURL 正确")
    } else {
        check(false, "合法链接应解析成功")
    }

    if let config = PairingLink.parse(validWithLang) {
        check(config.src == "de", "带语言参数 src")
        check(config.dst == "zh", "带语言参数 dst")
    } else {
        check(false, "带语言参数的合法链接应解析成功")
    }

    check(PairingLink.parse("rsnode://pair?v=2&id=node123&host=mini2.tailnet.ts.net&port=8791&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2") == nil, "v≠1 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=8791&name=Mini2") == nil, "缺 tok 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=8791&tok=abc&name=Mini2") == nil, "tok 非 64 位 hex 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=8791&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdeG&name=Mini2") == nil, "tok 含非 hex 字符 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=0&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2") == nil, "端口 0 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2.tailnet.ts.net&port=65536&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2") == nil, "端口 65536 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2@bad&port=8791&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2") == nil, "host 含非法字符 @ 应失败")
    check(PairingLink.parse("rsnode://pair?v=1&id=node123&host=mini2 bad&port=8791&tok=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef&name=Mini2") == nil, "host 含空格 应失败")

    if let config = PairingLink.parse(validLink) {
        let desc = config.displayName
        check(!desc.contains("0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"), "displayName 不含 token")
        let mirror = Mirror(reflecting: config)
        var hasTokenInMirror = false
        for child in mirror.children {
            if child.label == "token" {
                hasTokenInMirror = true
            }
        }
        check(hasTokenInMirror, "NodeConfig 含 token 字段（内部用）")
    }
}