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
| 4 | 回退：模拟 429 | ✅ | 10-19：plan.md 的「先打探针再回放」打不到（Groq retry-after 只有 3s，rslite 第 12 秒才发首段）。改成回放第 9 秒并发 24 发探针：rslite 首段真收到 **429**，那段由本机句（4.3–13.7s）承接，冷却中下一段不上传，16.7s 自动回到「云端·Groq」；problems=[]，WER 2.63%，key 不在输出里，真实配置没动。小缺口（不阻塞）：冷却期间 stderr 和状态行都没写明「限流冷却」，只能看到 `status=429` |
| 4 | 回退：额度满 | ✅ | 10-19：fakehome 账本 6500s（90.3%）→ 全程没走云端，直接混合·yzwdmsnlmm；problems=[]，WER 0.88% |
| 4 | 回退：拔网 | 👤🔑 | 用户在场时手动关 Wi-Fi |
| 1 | 有 key 默认走云端，悬浮窗显示「云端」 | ✅ headless / 👤 悬浮窗 | 10-19 run_mac_key.sh（key 取自 ~/.config/groq/env，只写 fakehome）：首个模式 status = 「模式：云端·Groq」；真实 ~/.config/rslite 快照未变。悬浮窗目测见 `acceptance/manual-v320.md` 第 7 步 |
| 2 | FLEURS 60 WER ≤ 4.5%；concat5 ≤ 2.63% | ✅ concat5 / ⏳ FLEURS | concat5 经 rslite 走 Groq：final WER **1.75%**，problems=[]（`rs-mac-native-data/accept-v320-key/20261019-140900`）。FLEURS 60 仍无 rslite 入口，佐证用 10-09 Groq 直测 3.62%； concat5 部分已有脚本（run_mac_key.sh cloud）；FLEURS 60 没有经 rslite 走 Groq 的评测入口（asr_bench.py 只测本机模型），可以拿 10-09 的 Groq 实测 3.62% 当佐证，或者另写入口 |
| 3 | 段尾 → 上屏中位数 ≤ 2s | ✅ | 10-19 942d954 之后两次：中位 **1.03s / 1.13s**（8 个云端 final），WER 1.75%。之前测的 3.16s 是测试回放源的问题：`FileSource` 每块相对 sleep，60s 时样本时钟落后墙钟 2.74s；9eb096a 埋点显示 send→recv 只有 0.35–1.0s。942d954 改成绝对节拍（60s 处 0.003s）。真实 `tap` 采集不走 FileSource，生产路径不受影响。|
| 5 | 故障消失后在静音点切回 Groq | ✅ | 10-19 run_mac_key.sh recover：第 15 秒 --cloud on 后出现「模式：云端·Groq」，problems=[]，key 没进 events/stderr。 脚本已写好（run_mac_key.sh recover：第 15 秒 cloud on） |
| 6 | 1 小时长测：0 次 429，记账一致 | ✅ | 10-19 mini2（`accept-v320-mini2/20261019-141426`）：**0 次 429**；N_local=390、S_local=4124.4s；逐段 billed_s == max(10, dur_s) 全部成立；390 条 remaining_requests 逐次恰好减 1（1998→1609）。脚本原来报 Delta_R=389≠390，是首个 remaining 在第一次请求扣完后才读到的 off-by-one，已在 2099ee9 修。|

Mac 自动验收记录：`~/projects/rs-mac-native-data/accept-v320/fallback2/`（仓库外）。跑法：`bash .governance/sprints-002-cloud-asr-first/acceptance/run_mac_auto.sh [--only gates|docs|fallback]`。

附带发现：
- **精修中位延迟 4.4–5.1s**，超过 Sprint 001 混合档 RFC 的 ≤ 4s（hybrid_score 的 `精修中位延迟 ≤ 4s` = false）。这一条不属于 v3.2.0 的 Done 4，但发布说明里不要再写「≤ 4s」。
- **验收隔离 bug 已修（ec5be35）**：`CloudConfigStore` 原来不认 `RSLITE_CONFIG_DIR` / `$HOME`。第一次跑时，假 key 和 `cloud off` 写进了真实的 `~/.config/rslite`，auto 模式也不允许协调者还原。**用户要手动执行**：`macos-native/.build/release/rslite --clear-groq-key && macos-native/.build/release/rslite --cloud on`（`quota-ledger.json` 里只多了一条 10 秒记录，可以删）。

## 10-19 合并与节点

- 用户合了 realtime_subtitle#75；master 已合进 feat（ad4fe98，requirements.txt 冲突：留 opencc，取 torch>=2.14.1）。合并后 Mac pytest 1402 过 / 33 跳过、ruff 0.17.1 绿；Windows rs-git 1169 过 / 0 失败（整套跑时 3 个语言对用例失败，是为真机测试拷进 rs-git 的 config_local.py 造成的，移走后 149/149 过）。
- mini2、yzwdmsnlmm 两个节点都已 `cluster.py update --force` 到 ad4fe98，`doctor` 显示版本、`status` 都在线；Mac 的 ~/.config/rslite/nodes.json 更新前后一致。

## Windows（yzwdmsnlmm）

2026-10-18 19:2x 检查时离线，**19:5x 重新上线**（rtt 42ms，Ollama 0.40.5，翻译在线）。`cluster.py doctor` 显示代码版本「—」，未开自动登录。10-13 那次重装失败回滚过，节点很可能还是旧代码：`cluster.py update --force yzwdmsnlmm` 属于装机，要先问用户。
开机后先跑 `cluster.py update --force yzwdmsnlmm`（安装预检要 Ollama 在跑，先确认它在），再跑 `cluster.py doctor`。

**10-19 节点已更新到新代码**。第一次 `update --force` 在第 7 步换名失败并回滚，回滚后节点没起来（已手动 `schtasks /run` 恢复）。根因：旧节点（10-12 代码）的 Ollama 看门狗用 `cwd=rs-node` 拉起了 `ollama serve`（PID 18092，父进程已退），Windows 上作为工作目录的文件夹不能改名——10-13 已在 `ollama_watchdog.default_spawn` 修成 `cwd=exe.parent`，但旧代码拉起的那个进程还在。处理：停 gateway → 按 PID 结束这个 Ollama → 用 WMI 以 Ollama 自己的目录作工作目录重新拉起 → 再 `update --force` rc=0。之后又用 `--client` 重装一次（Done 13）、用 5e0da6f 重装一次，三次都成功。**其他老节点第一次更新到新版时可能碰到同样的问题**，处理方法相同。
5e0da6f 修了 `cluster.py doctor` 代码版本永远显示「—」（安装脚本从没写过 `.rs_commit`）；yzwdmsnlmm 现在显示 09e390d。

| Done | 项 | 状态 | 说明 |
|---|---|---|---|
| 14 | 全量 pytest（Windows）0 failed | ✅ | **10-19 在 09e390d 的真 git 克隆（%USERPROFILE%\rs-git，PATH 加 scoop ffmpeg 9.0.2）：1169 passed / 267 skipped / 0 failed，ruff 全绿**。（rs-test 副本仍是 1165 passed / 4 环境失败，原因同下。）以下为 10-18 记录： 015d7fa 在 rs-test 副本上：1165 passed / 267 skipped / 4 failed。剩下 4 个和 beta 时相同，都是环境问题：副本不是 git 仓库（test_repo_root_inventory ×2），ssh 会话里 ffprobe 不可达（test_real_ffprobe_* ×2）。新增的 5 个平台假设失败已由 015d7fa 修掉，`test_default_spawn_windows_flags` 在 Windows 上单跑通过 |
| 14 | 单机冒烟（启动 → 出字 → 停止脚本干净退出） | 👤 | 步骤见 `acceptance/manual-v320.md` 第 3 步（测试副本 %USERPROFILE%\rs-git） |
| 9 | 混合档：WER ≤ 纯本机、无重复、首字 ±0.2s | 👤（前置已备好） | 没有命令行回放入口，只能真机放音频人工计时 |
| 10 | 停节点后字幕不断，恢复后 ≤ 60s 回混合 | 👤 | 10-19 已把 mini2 条目单独下发到 Windows 客户端（source install_node.sh 调 push_windows_client，**没重装 mini2**），Windows 上 prepare(mini2) 鉴权通过。步骤见 manual 第 5 步 |
| 11 | 本机当节点时跳过自己（日志有理由） | ✅ 数据 / 👤 日志 | 10-19 真机：客户端 nodes.json 的 node_id 与 %LOCALAPPDATA%\rs-node\node_id 一致，`is_self_node` = True。运行时日志要开界面，见 manual 第 2 步 |
| 12 | 新装 / 老用户更新后节点开关默认关；关时无 8791 连接；拒绝非 Tailscale 地址 | ✅ 拒连 / 👤 其余 | 10-19 真机 `validate_and_resolve_node_host`：192.168.1.50、8.8.8.8、localhost 都抛 BadURLError，tailnet 名解析到 100.116.234.34 放行。默认关 + netstat 见 manual 第 1 步 |
| 13 | `--client` 下发后免配置；token ACL = 当前用户 + SYSTEM | ✅ 文件 / 👤 界面 | 10-19 `install_node_windows.sh yzwdmsnlmm --client yzwdmsnlmm` rc=0：nodes.json 一条（无 token），tokens\yzwdmsnlmm.token 的 ACL 只有 SYSTEM:(F) + yzwdm:(F)。界面免配置见 manual 第 1–2 步 |
| — | 高 DPI 下点「未翻译 ⟳」、灰色文字可读性 | 👤 | 10-11 留下的真机项 |
| — | bootstrap.cmd 引导安装器 | 不进 v3.2.0 | v3.3 |

## 发布动作（每一步都要用户点头）

1. Windows 和 🔑 项做完，把本表和发布说明「测试」一节填上实际数字。
2. ✅ 10-18 已推 a210da6（用户批准）。之后的 33ea32a（需 key 的验收脚本）和本清单的更新只在本地。
3. 合入 master：先合依赖集成 PR realtime_subtitle#70（deps-batch 会话建议），再把 master 同步进 feat。requirements.txt 会有一行冲突：保留 `opencc-python-reimplemented`，取 `torch>=2.14.1,<3.0.0`；ci.yml 能自动合并（ruff 0.17.0）。同步完重跑 Mac + Windows pytest，再快进 master。
4. 打 `v3.2.0` tag，发 GitHub Release（只发源码），正文用 `release-notes-v3.2.0.md`。
5. 更新各节点：`cluster.py update`。
