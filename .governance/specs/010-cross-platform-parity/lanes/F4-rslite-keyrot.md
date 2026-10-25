# F4 rslite-keyrot — rslite 换 key 时作废旧 key 的在飞请求（与 Android A5 同一 bug）

> 仓库：realtime_subtitle，分支 lane/f4-rslite-keyrot（基于 lane/rs010-int3）。免费模型定向审计发现，协调者已对照代码确认。

## 问题
`macos-native/Sources/rslite/CloudSegmenter.swift` `refreshSwitchLocked()`（约 845–849 行）的 `key_modified` 分支只 apply 事件并换新 client，**没有作废代次、没有取消旧 client 的在飞请求**。上传前记了 `segmentEpoch[segment.id] = epoch`（约 678 行），失败回执只检查 `segmentEpoch[id] != epoch`（约 693、713 行）；代次没变，旧 key 的迟到 401 就通过了检查，把新 key 的路由打成 `disabled_invalid_key`。之后 key 不再变，`key_modified` 不会再触发，本会话永久停在「云端 key 无效」。

## 修法（照做）
在 `key_modified` 分支里，**换 client 之前**：`epoch += 1`、`probeID = nil`、`segmentBounds.removeAll()`、对**旧** client 调 `cancelInflightRequests()`；然后再 `client = makeClient(...)`。
注意：函数末尾已有的 `if cancel { ... client?.cancelInflightRequests() }` 是在 client 被替换之后执行的，**不能**简单地把 `cancel = true` 交给它——那会取消新 client 而不是旧的。

## 自测（加进 `CloudSegmenter.swift` 现有 selfTest，用现有的 FakeCloud / SwitchBox 写法）
旧 key 发出一段上传 → 切换 SwitchBox 的 key → 旧段以 401 失败回执 → 断言：模式不是 `cloudKeyInvalid`、路由状态不是 `disabled_invalid_key`；新 key 的下一段照常上传。另断言旧 client 收到了取消。

## write_scope
`macos-native/Sources/rslite/CloudSegmenter.swift`。

## Done criteria
- [ ] 新自测在修复前会失败（报告里写明怎么确认的）、修复后通过
- [ ] `swift build -c release && .build/release/rslite --selftest` 输出 SELFTEST OK
- [ ] `TMPDIR=/tmp pytest -q tests/test_p9_route_cases.py` 照旧通过

## 验证命令
```bash
(cd macos-native && swift build -c release && .build/release/rslite --selftest)
TMPDIR=/tmp ~/projects/rs-mac-native/.venv/bin/python -m pytest -q tests/test_p9_route_cases.py
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
