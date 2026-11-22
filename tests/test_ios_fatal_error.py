"""HC-I01：广播扩展的致命错误必须结束广播，并把中文原因交给用户。

为什么是源码扫描而不是行为测试：SubtitleEngine 依赖 ReplayKit / Speech /
Translation，pytest 环境里起不了真实广播；而这条约束真正怕的是「新增了一条
fatal 分支但忘了通知宿主」——那种漏改只能靠扫全部写入口拦下来（参考
`test_runtime_paths.py` 第一条用例的思路）。
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENGINE = ROOT / "ios" / "Engine" / "SubtitleEngine.swift"
HANDLER = ROOT / "ios" / "RSBroadcast" / "SampleHandler.swift"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_hc_i01_engine_reports_fatal_error_to_host():
    """HC-I01：onFatalError 必须至少有一个调用点。

    违反后果：fatalErrorOccurred 置位后引擎不再处理音频，而扩展不会收到任何
    信号，继续「广播」但所有用户都看不到字幕，也没有任何错误提示。
    必须怎么改：所有 fatal 分支统一走 markFatalError，由它在锁外调用 onFatalError。
    """
    src = _source(ENGINE)
    assert "var onFatalError" in src, "我们的硬约束要求引擎保留 onFatalError 回调"
    assert re.search(r"onFatalError\?\(", src), (
        "HC-I01 违反：SubtitleEngine 只声明了 onFatalError，没有任何调用点。"
        "致命错误必须交给宿主（广播扩展据此结束广播），否则用户只会看到没有字幕。"
    )


def test_hc_i01_every_fatal_assignment_goes_through_mark_fatal_error():
    """HC-I01：所有 fatal 置位都必须经过同一个通知入口。

    违反后果：以后新增 fatal 分支时只写 fatalErrorOccurred = true，又回到
    「扩展照常广播但不出字幕」；漏一处就够。
    必须怎么改：把置位和通知收进 markFatalError(code:)，不要在别处直接赋值；
    三条现有分支（local_needs_ios26 / 本机失败码 / 节点 fatal）都要走它。
    """
    src = _source(ENGINE)
    assignments = [
        line.strip()
        for line in src.splitlines()
        if "fatalErrorOccurred = true" in line
    ]
    assert assignments == ["fatalErrorOccurred = true"], (
        "HC-I01 违反：fatalErrorOccurred = true 只允许出现在 markFatalError 里，"
        f"实际有 {len(assignments)} 处。新增 fatal 分支必须走 markFatalError，"
        "否则不会通知宿主结束广播。"
    )
    assert src.count("markFatalError(code:") - src.count("func markFatalError(code:") >= 2, (
        "HC-I01 违反：两条 fatal 分支（本机失败码、节点 fatal；改版后只支持 iOS 26+，local_needs_ios26 已不再致命）"
        "都必须调用 markFatalError(code:)。"
    )


def test_hc_i01_fatal_message_reuses_design_tokens():
    """HC-I01：结束广播的原因必须复用既有中文文案，不能新写死一份。

    违反后果：四端共享 design/tokens.json，iOS 另写一句就会在令牌更新时漂移，
    而且 CI 的状态文案检查抓不到「全新写法」。
    必须怎么改：本机失败码走 LocalEngineStatus，节点失败走 DesignTokens.Text。
    """
    src = _source(ENGINE)
    assert "LocalEngineStatus.userMessage" in src, (
        "HC-I01 违反：本机失败码的广播结束原因必须复用 LocalEngineStatus 的既有文案。"
    )
    assert "DesignTokens.Text." in src, (
        "HC-I01 违反：节点 fatal 的广播结束原因必须复用 DesignTokens.Text，不能写死中文。"
    )


def test_hc_i01_auto_fallback_stays_non_fatal():
    """HC-I01：自动档的本机/节点失败只回退，不许当成 fatal 结束广播。

    违反后果：自动档本来承诺「本机坏了就交还节点」，当成 fatal 会直接掐掉
    广播，用户连回退后的字幕都看不到。
    必须怎么改：auto 分支继续走 applyAutoDecision，fatal 只留在显式档的 else 里。
    """
    src = _source(ENGINE)

    local_at = src.index("private func handleLocalStatus")
    auto_at = src.index('if engineMode == "auto" {', local_at)
    auto_branch = src[auto_at : src.index("} else {", auto_at)]
    assert "localAvailabilityChanged(false)" in auto_branch, (
        "HC-I01 违反：自动档本机失败必须回退节点（localAvailabilityChanged(false)）。"
    )
    assert "markFatalError" not in auto_branch, (
        "HC-I01 违反：自动档本机失败不算 fatal，不能调用 markFatalError。"
    )

    node_at = src.index("private func handleNodeEvent")
    node_auto_at = src.index('if engineMode == "auto" {', node_at)
    node_auto_branch = src[node_auto_at : src.index("} else {", node_auto_at)]
    assert "reconnectAbandoned" in node_auto_branch, (
        "HC-I01 违反：自动档节点致命错误只换档重连（reconnectAbandoned），不能结束广播。"
    )
    assert "markFatalError" not in node_auto_branch, (
        "HC-I01 违反：自动档节点致命错误不算 fatal，不能调用 markFatalError。"
    )


def test_hc_i01_sample_handler_finishes_broadcast():
    """HC-I01：SampleHandler 必须用 finishBroadcastWithError 结束广播。

    违反后果：即使引擎报告了致命错误，扩展不调用 ReplayKit 的结束接口，
    广播状态仍然是「正在广播」，音频采样继续进来却没人处理。
    必须怎么改：在 onFatalError 回调里调用 finishBroadcastWithError(_:)。
    """
    src = _source(HANDLER)
    assert "onFatalError" in src, (
        "HC-I01 违反：SampleHandler 没有接引擎的 onFatalError，死亡分支永远传不到 ReplayKit。"
    )
    assert "finishBroadcastWithError(" in src, (
        "HC-I01 违反：SampleHandler 必须用 finishBroadcastWithError(_:) 结束广播，"
        "否则用户看到的是「广播中」但永远没有字幕。"
    )
