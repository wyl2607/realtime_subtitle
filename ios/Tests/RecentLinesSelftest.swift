import Foundation

/// I5：SampleHandler 抽出来的纯判定。独立 @main，不碰 ReplayKit / Keychain / 网络。
@main
struct RecentLinesSelftest {
    struct Line: SessionScopedLine {
        let session: String
        let id: Int
    }

    static func main() {
        var failures: [String] = []
        func check(_ ok: Bool, _ name: String) {
            if !ok { failures.append(name) }
        }

        // 问题 2：节点前后两个会话的第 1 句，译文只能回填到当前会话那一行。
        let lines = [Line(session: "nodeA", id: 1), Line(session: "local1", id: 1), Line(session: "nodeB", id: 1)]
        check(RecentLineLookup.index(in: lines, session: "nodeB", id: 1) == 2, "译文应回填当前节点会话的第 1 句，不是旧会话的")
        check(RecentLineLookup.index(in: lines, session: "local1", id: 1) == 1, "本机译文只匹配本机会话")
        check(RecentLineLookup.index(in: lines, session: "nodeC", id: 1) == nil, "当前会话没有这一句就不回填")

        // 问题 1：用户已经开始说下一句（句中）时不能切回节点，否则这句随本机引擎一起丢。
        check(!HandoffRules.canHandBackToNode(pendingSettles: [], sentenceActive: true), "句中不应切回节点")
        check(!HandoffRules.canHandBackToNode(pendingSettles: [3], sentenceActive: false), "译文还在路上不应切回节点")
        check(HandoffRules.canHandBackToNode(pendingSettles: [], sentenceActive: false), "静音点且都收尾了应切回节点")

        // 问题 3：广播已结束，迟到的回调不能把本机引擎拉起来。
        check(!HandoffRules.mayStartLocal(broadcastActive: false), "广播结束后不应启动本机")
        check(HandoffRules.mayStartLocal(broadcastActive: true), "广播进行中可以启动本机")

        // 问题 4：换 key 要轮换客户端；旧代次的结果作废。
        check(HandoffRules.keyRotated(clientRevision: 1, keyRevision: 2), "key 变了应轮换客户端")
        check(!HandoffRules.keyRotated(clientRevision: 2, keyRevision: 2), "key 没变不应轮换")
        check(!HandoffRules.keyRotated(clientRevision: nil, keyRevision: 2), "还没有客户端时不算轮换（直接建）")
        check(HandoffRules.isStaleCloudResponse(requestGeneration: 4, currentGeneration: 5), "换 key 后旧请求的结果应作废")
        check(!HandoffRules.isStaleCloudResponse(requestGeneration: 5, currentGeneration: 5), "当前代次的结果照常处理")

        // 节点 ready 之后状态行不能停在「正在加载模型…」（iPhone 真机：ready=1 但 last 还是加载中）。
        check(HandoffRules.statusAfterReady("mini2 正在加载模型…", loadingSuffix: " 正在加载模型…") == "ready", "ready 后加载中状态应清掉")
        check(HandoffRules.statusAfterReady("closed=1006", loadingSuffix: " 正在加载模型…") == "closed=1006", "ready 不覆盖别的状态码")
        check(HandoffRules.statusAfterReady("", loadingSuffix: " 正在加载模型…") == "", "空状态保持为空")

        if failures.isEmpty {
            print("SELFTEST OK")
        } else {
            for failure in failures { print("FAIL: \(failure)") }
            exit(1)
        }
    }
}
