# I5 ios-fixes2 — iOS 切换时机、跨会话译文回填、广播结束竞态（评审第二轮，3 条已确认）

> 仓库：realtime_subtitle，分支 lane/i5-ios-fixes2（基于 lane/i3-ios-cloud-wire（c6f91e5，已含云端接线））。在 mini2 上跑。问题来自免费模型评审，协调者已对照代码确认。

## 问题
1. **切回节点会截断正在说的下一句**（`ios/RSBroadcast/SampleHandler.swift` `handleLocalSettled`）：只看 `pendingLocalSettles.isEmpty`，没看 `localSentenceActive`（用户已经开始说下一句）。A 句译文一落地就切节点并停本机引擎，B 句音频已喂进本机 analyzer 但没 final，随引擎一起丢失。spec 010 C1 要求「恢复后在静音点切回」。修法：「可以切回」= 没有待收尾的句子 **且** 当前没有进行中的句子（静音点）；若有进行中的句子，等它 final 并收尾后再判断。
2. **译文按 id 回填会写错行**（`SampleHandler.swift` 里两处 `recentLines.firstIndex(where: { $0.id == id })`）：`recentLines` 会同时含本机与节点、或节点前后两个会话的行，而两边 id 都从 1 重新计数。修法：查找时同时匹配会话代号（I2 已给每行加了 `session`）；本机译文只匹配当前本机会话代号，节点译文只匹配当前节点会话代号。
3. **广播结束后仍可能启动本机引擎**（`startLocalEngine` 无条件 `broadcastActive = true`）：`handleNodeClose` / Tailnet 解析失败路径在锁外执行 `applyAutoDecision`，与 `broadcastFinished` 并发时，`startLocalEngine` 会把已置 false 的标志改回 true。修法：`startLocalEngine` 不再写 `broadcastActive`（只由 `broadcastStarted` 置真、`broadcastFinished` 置假）；`applyAutoDecision` 的 `.startLocal` 分支在锁内重新确认 `broadcastActive`，为 false 就什么都不做。

4. **换 key 时旧 key 的迟到 401 把新 key 打成无效**（Android、Mac rslite 已确认有同样 bug，I3 刚把云端接进 iOS，要一起防住）：每个上传请求记下发出时的 key 代号（换 key 时代号 +1，并取消旧 client 的在飞请求——**先取消旧的，再换新 client**）；回来的结果/错误代号不是当前代号就直接丢弃（不进路由状态机、不记额度、不上屏）。

5. **（评审提出，先核实再修）** `ios/Shared/CloudSegmenter.swift` 约 79 行：大缺口分支把 queued 里未 ready 的段留在旧时间轴上，断流后 flush 可能发出 PCM 为空的段（浪费一次计费请求、产出空译文）。成立就修并加自测（flush 不得产出空 PCM 段）；不成立就在报告里写清依据。write_scope 追加 `ios/Shared/CloudSegmenter.swift`、`ios/Tests/CloudSegmenterSelftest.swift`。

## 自测
能抽成纯逻辑的都要有断言：把「可以切回节点」的判定抽成纯函数（输入：待收尾集合、是否有进行中的句子）；把「按会话+id 找行」抽成纯函数；把「startLocal 前确认广播仍在」抽成可测的决策。写不成自测的部分在报告里说明。

## write_scope
`ios/RSBroadcast/SampleHandler.swift`、`ios/Shared/AutoFallback.swift`、`ios/Tests/AutoFallbackSelftest.swift`、`ios/Shared/RecentLines.swift`（如需新建纯逻辑文件）、`ios/Tests/RecentLinesSelftest.swift`（新）、`ios/Tests/run-selftest.sh`（只为加编译文件）。

## Done criteria
- [ ] 三条各有自测断言（或写明为什么写不成）
- [ ] `bash ios/Tests/run-selftest.sh` 全部 SELFTEST OK；`bash ios/Tests/build-check.sh` 通过；`python3 scripts/design/check_parity.py` PARITY OK

## 验证命令
```bash
export PATH=/opt/homebrew/bin:$PATH
bash ios/Tests/run-selftest.sh && bash ios/Tests/build-check.sh && python3 scripts/design/check_parity.py
```

## 通用规则（每条线都适用）
- 只改本卡 write_scope 里列出的文件；需要改别的文件就停下，写进 LANE_REPORT.md 交给协调者。
- 不 commit、不 push、不做 `git reset` / `git checkout --` / `git clean` 之类的破坏性操作；协调者复核后统一提交。
- 不连任何线上节点（mini2 / yzwdmsnlmm / mac-mini），不发真实网络请求；测试里只用假 key（如 `gsk_test_fake_key_...`），绝不读取 `GROQ_API_KEY` 或 `~/.config/groq`。
- 日志里不出现识别正文和 token/key。
- 注释用中文，写清「为什么」；不加卡片以外的功能、配置旋钮或回退逻辑。
- 移植时以参考实现的**行为**为准（常量、状态名、边界条件逐一对齐），不要"改进"规则——规则改了四端就不一致了。
- 结束前在工作目录根写 `LANE_REPORT.md`：改了哪些文件、每个文件做了什么；你实际运行过的验证命令及其**原样输出的最后 20 行**；没做完/没验证的项。**没运行过的命令不许写"通过"**——协调者会逐条重跑核对，编造会被整条作废。
- `_ref/` 目录（如果有）是协调者放的只读参考副本，不要修改，也不要在代码里引用它的路径。
