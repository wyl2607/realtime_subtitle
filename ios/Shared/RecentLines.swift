import Foundation

// SampleHandler 里几条容易写错的判定抽成纯函数，selftest 才钉得住（spec 010 I5）。
// 广播扩展本身依赖 ReplayKit，没法在命令行里跑，所以只把「判定」搬到这里，接线留在原处。

/// 带会话代号的一行字幕。本机 / 节点 / 云端的句子 id 都按会话从 1 重新计数。
protocol SessionScopedLine {
    var session: String { get }
    var id: Int { get }
}

enum RecentLineLookup {
    /// 按「会话代号 + id」找行：只比 id 的话，节点前后两个会话（或本机与节点）的同号句会串行，
    /// 译文就写到别人的行上去了。
    static func index<L: SessionScopedLine>(in lines: [L], session: String, id: Int) -> Int? {
        lines.firstIndex { $0.session == session && $0.id == id }
    }
}

enum HandoffRules {
    /// 能不能把音频交还给节点。spec 010 C1 要求在静音点切回：
    /// 没有待收尾的句子（译文还在路上），并且用户不在说下一句。
    static func canHandBackToNode(pendingSettles: Set<Int>, sentenceActive: Bool) -> Bool {
        pendingSettles.isEmpty && !sentenceActive
    }

    /// 拉起本机引擎前确认广播仍在：节点 close / 解析失败的回调可能在广播结束之后才到。
    static func mayStartLocal(broadcastActive: Bool) -> Bool {
        broadcastActive
    }

    /// 节点 ready 之后状态行该显示什么：还停在「<节点> 正在加载模型…」就换成 ready，
    /// 别的状态码（断开、错误）不动。只有 loading 会被 ready 取代。
    static func statusAfterReady(_ last: String, loadingSuffix: String) -> String {
        last.hasSuffix(loadingSuffix) ? "ready" : last
    }

    /// 云端结果是不是作废的旧请求（换 key / 关开关 / 退出云端档都会推进代次）。
    /// 作废的结果不进路由状态机、不记额度、不上屏。
    static func isStaleCloudResponse(requestGeneration: UInt64, currentGeneration: UInt64) -> Bool {
        requestGeneration != currentGeneration
    }

    /// key 变了要不要换客户端。换的时候必须先取消旧客户端的在飞请求、推进代次，再建新的：
    /// 旧 key 的迟到 401 否则会把新 key 永久打成「云端 key 无效」。
    static func keyRotated(clientRevision: Int?, keyRevision: Int) -> Bool {
        guard let clientRevision else { return false }
        return clientRevision != keyRevision
    }
}
