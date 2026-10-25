# P1 py-node-connect — Python 桌面端 5 秒就放弃节点连接（冷启动续等完全不可达）

> 仓库：realtime_subtitle，分支 lane/p1-py-node-connect（基于 lane/rs010-int3）。

## 问题 1【严重，已由协调者确认】
`realtime_subtitle/app.py` 约 697 行 `self._run_on_loop(client.connect())`，而 `_run_on_loop(self, coro, timeout=5)`（约 799 行）默认 5 秒就 `future.result(timeout=5)` 抛 TimeoutError。于是 `node_client.connect()` 里 15s 基础等待、loading 续期到 60s 全都到不了——Windows/Mac 桌面端 5 秒就放弃节点。
**修法**：connect 这一处用 `READY_LOADING_MAX_SECONDS`（`realtime_subtitle/remote/node_client.py`）加一点连接余量作为外层超时（常量引用，不写死 60），其余 `_run_on_loop` 调用的超时不变。connect 自己的截止逻辑仍是唯一真相：外层只是兜底，不能比它先到期。
**测试**：用假 client 让 connect 耗时 >5s 但 <60s（时间注入或把常量 monkeypatch 成很小的值等比缩放），断言不再被外层 5s 打断。

## 问题 2、3（评审提出，**先核实再修**；不成立就在报告里写清依据，不要改）
- `realtime_subtitle/node/gateway.py` 约 831 行：第二个会话（或超时后的重连）复用「进程已起但尚未 ready」的 worker 时不发 loading——按协议，只要 worker 尚未 ready，新会话也应收到 loading。
- `realtime_subtitle/app.py` 约 704–706 行：`finally` 在 `current_node_id` 更新前就重新播报路由，状态行短暂显示错误节点名。

## write_scope
`realtime_subtitle/app.py`、`realtime_subtitle/node/gateway.py`、`tests/*.py`。

## Done criteria
- [ ] 问题 1 的测试在修复前会失败（写明怎么确认）、修复后通过
- [ ] 问题 2、3 各自「已修 + 测试」或「不成立 + 文件:行号依据」
- [ ] `TMPDIR=/tmp pytest -q` 全量 0 failed，`ruff check .` 绿

## 验证命令
```bash
TMPDIR=/tmp ~/projects/rs-mac-native/.venv/bin/python -m pytest -q
~/projects/rs-mac-native/.venv/bin/ruff check .
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
