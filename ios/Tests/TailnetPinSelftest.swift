import Foundation

func runTailnetPinSelftest(_ check: (Bool, String) -> Void) {
    check(TailnetPin.isTailnetIPv4("100.64.0.0"), "100.64.0.0 属于 tailnet")
    check(TailnetPin.isTailnetIPv4("100.127.255.255"), "100.127.255.255 属于 tailnet")
    check(!TailnetPin.isTailnetIPv4("100.63.255.255"), "100.63.255.255 不属于 tailnet")
    check(!TailnetPin.isTailnetIPv4("100.128.0.0"), "100.128.0.0 不属于 tailnet")
    check(!TailnetPin.isTailnetIPv4("192.168.1.1"), "192.168.1.1 不属于 tailnet")
    check(!TailnetPin.isTailnetIPv4("mini2"), "主机名不是 IPv4")

    let url = URL(string: "ws://mini2.tailnet.ts.net:8791/v2/session?x=1#frag")!
    let pinned = try? TailnetPin.pinned(url, resolve: { host in
        check(host == "mini2.tailnet.ts.net", "解析闭包收到原始主机名")
        return ["100.101.102.103", "100.101.102.104"]
    })
    check(pinned?.absoluteString == "ws://100.101.102.103:8791/v2/session?x=1#frag", "tailnet 地址应钉成第一个 IP 并保留端口路径")

    let mixed = try? TailnetPin.pinned(url, resolve: { _ in ["100.101.102.103", "192.168.1.1"] })
    check(mixed == nil, "解析结果混有公网/私网地址应拒绝")

    let empty = try? TailnetPin.pinned(url, resolve: { _ in [] })
    check(empty == nil, "解析为空应拒绝")

    var literalResolved = false
    let literal = try? TailnetPin.pinned(URL(string: "ws://100.70.1.2:8791/v2/session")!, resolve: { _ in
        literalResolved = true
        return []
    })
    check(literal?.absoluteString == "ws://100.70.1.2:8791/v2/session", "tailnet IPv4 字面量应只校验不解析")
    check(!literalResolved, "IPv4 字面量不应调用解析闭包")

    let badLiteral = try? TailnetPin.pinned(URL(string: "ws://192.168.1.1:8791/v2/session")!, resolve: { _ in [] })
    check(badLiteral == nil, "非 tailnet IPv4 字面量应拒绝")
}
