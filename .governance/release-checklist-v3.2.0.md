# v3.2.0 正式版发布清单

> 2026-10-18 协调者整理。基线：feat/macos-native（beta tag v3.2.0-beta = 69c9604，之后 92 个提交）。本地还有 3 个未推的提交：7649ec3 忽略 uv.lock、ec5be35 CloudConfig 认 RSLITE_CONFIG_DIR + 验收脚本、015d7fa Windows 测试适配，外加提交本清单的那一个。
> 判据来源：`sprints-002-cloud-asr-first/RFC.md` Done 1–15；执行细节：`acceptance/plan.md`（先读开头的「协调者核对」）。
> 自动部分：`acceptance/run_mac_auto.sh`（lane 写，协调者串行跑；rslite 有单实例锁，冒烟全局串行）。

图例：✅ 已过（附证据） ❌ 未过 ⏳ 未跑 👤 需用户真机/在场 🔑 需 Groq key

## Mac（MBA 本机）

| Done | 项 | 状态 | 证据 / 说明 |
|---|---|---|---|
| 7 | pytest / ruff / swift build / selftest / 设计令牌同步 | ✅ | 2026-10-18 19:3x 在 7649ec3 上：pytest 1402 passed / 33 skipped（166s，TMPDIR=/tmp），ruff 全绿，`swift build -c release` 成功，`SELFTEST OK`，`gen_tokens.py --check` rc=0 |
| 8 | 三份 README 写明开关、隐私、key 配置 | ✅ | run_mac_auto.sh --only docs：中/英/德三份都有 Groq、`--cloud on\|off`、隐私、数据保留、`--set-groq-key` |
| 15 | version.py = 3.2.0；CLAUDE.md / README / 手册同步 | ✅（部分） | version.py=3.2.0，README 首段写的是 v3.2.0；用户手册是否同步要人工扫一眼 |
| 4 | 回退：错 key | ✅ | 13s 内切到「云端 key 无效」，节点接管；不重复不丢句；最终 WER 0.88%；假 key 没进任何日志（fallback2/bad-key） |
| 4 | 回退：关开关 | ✅ | 没出现「云端·Groq」，走 mini2 混合；不重复不丢句；WER 0.88% |
| 4 | 回退：节点也离线 → 本机 | ✅ | `select local reason=no_available_node`，只有「本机」模式；不重复不丢句；WER 7.89%（本机 Apple 识别） |
| 4 | 回退：模拟 429 | 🔑 | 会消耗约 24 次真实额度，不能放进 Done 6 长测窗口 |
| 4 | 回退：额度满 | 🔑 | 构造账本；没有真实 key 时看不出与「无 key」的区别 |
| 4 | 回退：拔网 | 👤🔑 | 用户在场时手动关 Wi-Fi |
| 1 | 有 key 默认走云端，悬浮窗显示「云端」 | 👤🔑 | 本机目前**没有** groq.key |
| 2 | FLEURS 60 WER ≤ 4.5%；concat5 ≤ 2.63% | 🔑 | concat5 部分已有脚本（run_mac_key.sh cloud）；FLEURS 60 没有经 rslite 走 Groq 的评测入口（asr_bench.py 只测本机模型），可以拿 10-09 的 Groq 实测 3.62% 当佐证，或者另写入口 |
| 3 | 段尾 → 上屏中位数 ≤ 2s | 🔑 | 脚本：run_mac_key.sh latency。df3bf1d 加了 headless `clock` 事件（墙钟/样本时钟零点），有 key 就能严格算；回退冒烟 fallback3 确认事件正常输出、评分不受影响 |
| 5 | 故障消失后在静音点切回 Groq | 🔑 | 脚本已写好（run_mac_key.sh recover：第 15 秒 cloud on） |
| 6 | 1 小时长测：0 次 429，记账一致 | 🔑 | 用户已批准（只拷文件、跑测试，不改 mini2 的服务配置）。脚本：run_mini2_hour.sh，只用 mini2 的 ~/rs-accept-v320。df3bf1d 让账本多记 `dur_s`、stderr 多写 `rslite.cloud remaining_requests=N`，现在能严格核对 `billed_s == max(10, dur)` 和 `N_local == Delta_R`。**mini2 要先拷 df3bf1d 之后构建的 rslite** |

Mac 自动验收记录：`~/projects/rs-mac-native-data/accept-v320/fallback2/`（仓库外）。跑法：`bash .governance/sprints-002-cloud-asr-first/acceptance/run_mac_auto.sh [--only gates|docs|fallback]`。

附带发现：
- **精修中位延迟 4.4–5.1s**，超过 Sprint 001 混合档 RFC 的 ≤ 4s（hybrid_score 的 `精修中位延迟 ≤ 4s` = false）。这一条不属于 v3.2.0 的 Done 4，但发布说明里不要再写「≤ 4s」。
- **验收隔离 bug 已修（ec5be35）**：`CloudConfigStore` 原来不认 `RSLITE_CONFIG_DIR` / `$HOME`。第一次跑时，假 key 和 `cloud off` 写进了真实的 `~/.config/rslite`，auto 模式也不允许协调者还原。**用户要手动执行**：`macos-native/.build/release/rslite --clear-groq-key && macos-native/.build/release/rslite --cloud on`（`quota-ledger.json` 里只多了一条 10 秒记录，可以删）。

## Windows（yzwdmsnlmm）

2026-10-18 19:2x 检查时离线，**19:5x 重新上线**（rtt 42ms，Ollama 0.40.5，翻译在线）。`cluster.py doctor` 显示代码版本「—」，未开自动登录。10-13 那次重装失败回滚过，节点很可能还是旧代码：`cluster.py update --force yzwdmsnlmm` 属于装机，要先问用户。
开机后先跑 `cluster.py update --force yzwdmsnlmm`（安装预检要 Ollama 在跑，先确认它在），再跑 `cluster.py doctor`。

| Done | 项 | 状态 | 说明 |
|---|---|---|---|
| 14 | 全量 pytest（Windows）0 failed | ✅（除 4 个环境项） | 015d7fa 在 rs-test 副本上：1165 passed / 267 skipped / 4 failed。剩下 4 个和 beta 时相同，都是环境问题：副本不是 git 仓库（test_repo_root_inventory ×2），ssh 会话里 ffprobe 不可达（test_real_ffprobe_* ×2）。新增的 5 个平台假设失败已由 015d7fa 修掉，`test_default_spawn_windows_flags` 在 Windows 上单跑通过 |
| 14 | 单机冒烟（启动 → 出字 → 停止脚本干净退出） | 👤 | |
| 9 | 混合档：WER ≤ 纯本机、无重复、首字 ±0.2s | 👤 | 没有命令行回放入口，只能真机放音频人工计时 |
| 10 | 停节点后字幕不断，恢复后 ≤ 60s 回混合 | 👤 | 不能在 Done 6 期间停 mini2 |
| 11 | 本机当节点时跳过自己（日志有理由） | ⏳ | 可以经 ssh 看日志 |
| 12 | 新装 / 老用户更新后节点开关默认关；关时无 8791 连接；拒绝非 Tailscale 地址 | ⏳ | |
| 13 | `--client` 下发后免配置；token ACL = 当前用户 + SYSTEM | ⏳ | 远程写 Windows 配置，先问用户 |
| — | 高 DPI 下点「未翻译 ⟳」、灰色文字可读性 | 👤 | 10-11 留下的真机项 |
| — | bootstrap.cmd 引导安装器 | 不进 v3.2.0 | v3.3 |

## 发布动作（每一步都要用户点头）

1. Windows 和 🔑 项做完，把本表和发布说明「测试」一节填上实际数字。
2. ✅ 10-18 已推 a210da6（用户批准）。之后的 33ea32a（需 key 的验收脚本）和本清单的更新只在本地。
3. 合入 master：先合依赖集成 PR realtime_subtitle#70（deps-batch 会话建议），再把 master 同步进 feat。requirements.txt 会有一行冲突：保留 `opencc-python-reimplemented`，取 `torch>=2.14.1,<3.0.0`；ci.yml 能自动合并（ruff 0.17.0）。同步完重跑 Mac + Windows pytest，再快进 master。
4. 打 `v3.2.0` tag，发 GitHub Release（只发源码），正文用 `release-notes-v3.2.0.md`。
5. 更新各节点：`cluster.py update`。
