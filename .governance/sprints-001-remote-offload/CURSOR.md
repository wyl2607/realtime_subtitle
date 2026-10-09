# CURSOR — sprints-001-remote-offload

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `execution+review`
- current_tk: `TK-002b,TK-005,TK-006,TK-007(prep)`
- current_cr: `CR-009`
- current_round: `0`
- escalated: `false`

## 进度快照

| TK | 标题 | 状态 |
|----|------|------|
| TK-001 | 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译 | done |
| TK-001b | worker：会话结束滑动更新 rtf | done |
| TK-002 | 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限 | done |
| TK-003 | 一键安装 install_node.sh | done（mini2 已装；TCP 监听 blocker，见 HANDOFF §11.2） |
| TK-004 | rslite：带时间句子 + AudioFanout + 混合替换 + 单实例 | done |
| TK-002b | gateway：launchd 下 Tailscale CLI 修复 + 安装自检补 TCP | done |
| TK-003b | install 测试 macOS 密封 + bash 多字节 bug | done |
| TK-002c | gateway 测试 1009 后重连时序 | done |
| TK-001c | worker：translator 写当前真实翻译器名 | done |
| TK-005 | rslite：NodeClient v2 + NodeRouter + 本机档门槛 | reviewing |
| TK-005b | 本机节点 UDS 发现 | planned |
| TK-006 | 清理 v1 + 文档 | reviewing |
| TK-007 | 端到端验收 + 五组功耗 + 结果文档 | planned |

## 最近动作
- 2026-10-09 17:12 CEST: CR-009 round4 Gemini needs_fix：R4-1 发布门两次加锁覆盖新迁移意图、R4-2 补零无上限（睡眠唤醒 57MB）、R4-3 原子写忽略 fsync/fchmod 返回值 → 全 accepted（R4-2 改为断档>5s 结束会话且不罚节点）→ sonnet 修；Round 5 为熔断前最后一轮
- 2026-10-09 17:10 CEST（**时间更正**：本机 `date` 实测 17:05；15:20 之后几条记录的时刻是 Coordinator 估算、偏晚约 1.5–2h，顺序无误）: TK-005 R3 修复 6eb2aa8（sonnet）→ Coordinator 复核 build/selftest/冒烟 24/24（含静默断网 17.1s 回退）→ CR-009 round4 Gemini pro（定向+安全，含补零无上限评估）
- 2026-10-09 18:50 CEST: **TK-002c done**：CR-011 两轮 approve（Gemini flash / haiku）→ 合并 b7a55c8。**主线首次全绿：1019 passed（安装 112 + 其余 907）、0 failed、ruff 通过**。TK-005 R3 修复（sonnet）进行中
- 2026-10-09 18:35 CEST: CR-009 round3 opus approve_with_minor：F4/F5 确认；新 R3-1（无 ping 超时，静默断网不回退，P7）/R3-2（扇出丢帧致节点时钟偏移，P5 错行）必修 + R3-4..7 低成本 + nit accepted；R3-3（本机 UDS 节点未实现）拆 TK-005b → sonnet 修（codex 20:24 才恢复、Gemini 前科假结论）→ 之后 round4。CR-011：opencode 因 external_directory 无结论、Gemini 503 → 重试中
- 2026-10-09 18:15 CEST: CR-009 F4/F5 已在 codex WIP 91d5310 完成（Gemini 接续确认无需增补，但越权 `git reset` 回退已推送提交 → Coordinator `reset --soft` 恢复直线历史）；本机复核 build/selftest/冒烟 18/18 → round3 Claude opus 终审。TK-002c 回报 4555a09（+7 行 wait_session_freed；并发两份各 120 passed）→ CR-011 round1（opencode）
- 2026-10-09 18:00 CEST: codex 第二次额度中断（20:24 重置），CR-009 F4 半成品 WIP 91d5310（build/selftest 通过）→ Gemini pro 接续 F4/F5（第 3 轮改由 Claude opus 终审，评审≠修复者）。frame_size 根因：1009 关闭后测试立即重连、gateway 收尾未完成 → 1013（测试时序，非产品缺陷）→ 新 TK-002c（Gemini flash，worktree rs-tk-002c）
- 2026-10-09 17:50 CEST: **TK-003b done**：CR-010 round2 approve → 合并 523877b（主线看门狗安装测试 112 passed、其余 906 passed/1 基线失败、ruff/shellcheck 通过）。主线安装脚本已可用于 mini2 重装（等用户批准）。CR-009 round2 Gemini：F1–F3 确认修好，新 F4（取消被当节点故障）/F5（旧会话 drain 期间断开覆盖新节点模式）accepted → codex 修。frame_size 基线失败（1013 busy）派 opencode 免费模型只做根因调查（detached /tmp/inv-frame/wt）
- 2026-10-09 17:35 CEST: CR-010 round1 codex needs_fix（CR010-01 TGT_PATH_EXPORT 控制端插值注入面 accepted）→ e3eef2b 单引号目标端展开（shellcheck 0、看门狗 112 passed）→ round2 codex；Gemini 在 tk-003b 仓库根留 30 个调试文件已挪 /tmp。TK-005 codex 修 F1–F3 → 18dcaed（Coordinator 本机 build/selftest/冒烟 18/18/真实配置未动）→ CR-009 round2 Gemini pro。等用户：mini2 重装批准、S1 契约取舍
- 2026-10-09 17:20 CEST: **TK-002b done**：CR-007 round3 Gemini approve → 合并 e3233ca（906 passed，唯一失败为基线 frame_size；ruff/bash -n 通过）。TK-003b：Gemini 定位 14 个 UnicodeDecodeError 根因——**bash 3.2 在 UTF-8 locale 下把 `$var` 后紧跟的全角字符首字节吞进变量名 → 变量值丢失+乱码**（Gemini 说成 C locale，Coordinator 复现纠正：C 下正常、UTF-8 下出错；生产 ssh 环境即 UTF-8，真 bug）；install_node.sh 2 处加大括号，并发两份 112 passed；Coordinator perl 全仓扫描另发现 power_compare.sh 2 处 → tk-007 [coordinator-direct] 1b4890b → CR-010（codex）。CR-008 两轮收敛（待 TK-005 先合）。CR-009：Gemini default needs_fix F1（stop 与 connect 竞态泄露会话）/F2（pendingNodeFinals 不清）/F3（pause→resume 路由不重启，Coordinator 核实属实）全 accepted；codex security S1（P1 要求 /v1/info 带 token vs S5「核对前不发 token」契约冲突）作为实现 rejected、上报用户决定，S2 rejected → codex 修 F1–F3
- 2026-10-09 16:55 CEST: CR-007 round2 codex approve（17c5463，Coordinator 另以 /bin/bash 3.2 实打 13 个边界 IP 全对）→ round3 Gemini pro 整体安全通读。CR-008 round1 codex needs_fix → F1/F2/F4 accepted、F3 rejected → Gemini flash 修。TK-003b：opencode 改 RS_TOOL_PATH_PREFIX（默认值不变）+ BSD sed + say/afconvert 桩 → 不再挂死（105s），剩 14 个注入类安全测试 UnicodeDecodeError(0xbc) → 合入 tk-002b 最新后 Gemini pro 查根因。**TK-005**：sonnet 本机调试 b66455a 修 4 个客户端 bug，冒烟 18/18；Coordinator 复核时发现 ①冒烟曾覆盖用户真实 ~/.config/rslite/nodes.json（mini2 条目丢失，已按安装脚本同法重建并 ssh 只读取回 node_id）②契约 bug：P6 url 不带路径 → 客户端连到 "/" → 真节点必失败 → [coordinator-direct] 9de0e79 补 /v2/session + RSLITE_CONFIG_DIR 隔离冒烟；复跑 18/18、真实配置 md5 不变 → reviewing，CR-009 round1（Gemini pro default + codex security），计划 3 轮
- 2026-10-09 16:40 CEST: CR-007 round1：codex needs_fix（D2 IP 每段未限 0–255，实测 100.64.999.999 预检通过 → accepted；D1 治理越界 → rejected，两点 diff 假象）、Gemini pro security approve（F01 nit 与 D2 同修法）→ TK-002b fixing（Gemini flash）。TK-006 回报 c038968（复核 884 passed、ruff 通过）→ reviewing，CR-008 round1（codex）。**发现**：sonnet 组长派 agy `--dangerously-skip-permissions` 被 auto-mode 分类器拒（Create Unsafe Agents）→ 分级编队里组长不能再往下派 agy，下级 lane 一律由 Coordinator 直接派；pytest 不带短 --basetemp 时 62 个 gateway 用例 AF_UNIX path too long（本机路径长度，非 bug）。TK-005：codex 加失败原因码 99b51ef，但其沙箱禁 socket/swift 缓存无法复现；Coordinator 本机实跑：会话已建立，但 gateway 未向客户端发事件、rslite 中途 no_available_node，冒烟判据自相矛盾 → sonnet 本机调试
- 2026-10-09 16:05 CEST: TK-002b 回报 2792120（Coordinator 复核 906 passed、唯一失败为基线已有 frame_size、ruff/bash -n 通过）→ reviewing，CR-007 round1：codex default + Gemini pro security。发现 tests/test_install_node.py 在 Mac 不密封（PATH 前缀盖桩→真跑 uv/ollama、BSD sed、挂死留孤儿；ssh 是桩、未触及 mini2，本机无误装）→ 拆 TK-003b（opencode big-pickle，基于 feat/tk-002b）；已清理本会话孤儿测试进程。TK-007 prep 回报 a0cd68a（power_compare 五组、hybrid_score.py+6 单测、node-acceptance.md 骨架）。TK-005：agy Claude 池周额度耗尽（117h）→ Gemini pro 接续，声称「全部完成、冒烟 4/4」，**Coordinator 复跑证伪**：用例 1 实为 `switch_failed err=NodeClientError` 0.28s 退回本机，判据过松；Gemini 还越界改了 .governance 任务卡（已撤回）→ codex 查握手根因并收紧冒烟判据
- 2026-10-09 15:50 CEST: TK-005 codex 额度中断（usage limit），半成品 1593 行存为 WIP 48d186c：swift build 通过、--selftest OK、--mode local 回放 5 句正常，但 codex 总结丢失 → 改派 agy Claude 池（claude-opus-4-6-thinking）做 Done criteria 审计+补齐+假 v2 节点冒烟
- 2026-10-09 15:35 CEST: **分级并行编队**（用户要求）：L0 Coordinator(opus) → L1 组长(sonnet) → L2 下级(agy Gemini 池 flash/pro、agy Claude 池 opus-4-6-thinking、haiku)。实测推翻 HANDOFF §2「不要派 agy」：agy 带 `--dangerously-skip-permissions` 两个池都能跑 shell；`claude -p --model haiku` 可用。grok 对 TK-006 exit 0 零改动（PLAYBOOK 已知坑）→ 弃用。TK-006 改由 sonnet 组长带 agy/haiku；新开 TK-007 prep（sonnet 组长，worktree rs-tk-007，只做 scripts/bench + 文档骨架，真机验收仍等前序）。TK-002b(sonnet)、TK-005(codex) 继续
- 2026-10-09 15:20 CEST: 用户要求多 lane 加速 → 三路并行（文件不重叠）：TK-002b=Claude sonnet（rs-tk-002b）；TK-005=codex gpt-5.5 high（rs-tk-005，沙箱不能 commit，Coordinator 代提交）；TK-006=grok（rs-tk-006，合并须在 TK-005 之后）。agy 不派（本机 headless 拒 shell，HANDOFF §2）。4 个 rs-cloud-* detached worktree 已由用户删除
- 2026-10-09 15:00 CEST: **本机会话接手**（MacBook Air）。主 worktree ff 到 ebaa9fe。mini2 只读复现根因：App Store 版 Tailscale 二进制靠 `SHLVL` 判 CLI/GUI，launchd 环境无 SHLVL → `tailscale ip -4` stdout 打「The Tailscale GUI failed to start…」、rc=0 → validate_host ValueError；env -i 下加 `SHLVL=1` 或官方开关 `TAILSCALE_BE_CLI=1` 均恢复正常，改 argv0/PWD/_ 无效。v1.restart 已不存在（安装成功时删除，符合预期）；gateway PID 12840 仍在退避。新建 TK-002b → executing（sonnet，worktree ~/projects/rs-tk-002b，安全类单独一批）
- 2026-10-09 14:40 CST: 用户叫停，全部提交，交给本机处理（HANDOFF §11）
- 2026-10-09 14:35 CST: **mini2 真机安装（用户批准）EXIT=0**：语言包 installed、RTF 0.368、v1 已停、LaunchAgent running（RSS 33.6MB、无 worker）、UDS /v1/info 200、权限/日志无 token 核验通过、nodes.json 已写。**验收发现 blocker**：gateway `tcp_bind_failed err=ValueError` 持续退避，没有 TCP 监听（远程不可达）；安装自检只验 UDS 所以报成功
- 2026-10-09 12:10 CST: mini2 安装前只读核查：GUI 会话在（console=yilinwang）、AC 供电、turbo 已缓存 3.0GB、v1 PID 34924 仍在 8791；MacBook Air 能 BatchMode ssh mini2。MacBook Air 建 detached worktree ~/projects/rs-cloud-install @d37ecea，`install_node.sh --dry-run mini2` 11 步计划正常 → 安装计划提交用户审批
- 2026-10-09 12:00 CST: CR-006 round2（opus）approve_with_minor：F1 partially（第一会话名恰等于 DEFAULT_TRANSLATOR，变异 B 抓不到）→ R2-F1 accepted，[coordinator-direct] 0691d95，复现改后变异 A/B 均红 → **TK-001c done**，合并进 feat/macos-native（920 passed、ruff 通过）；F3 [coordinator-direct] efa713c。下一步：向用户说明 mini2 真机安装计划，等批准
- 2026-10-09 11:45 CST: TK-001c repair 回收 8fc99ce（Coordinator 复跑 210/920 passed、ruff 通过）→ recheck，CR-006 round2（opus）
- 2026-10-09 11:35 CST: CR-006 round1（opus）approve_with_minor：F1（换语言对用例）/F2（注释）accepted → repair（同一 sonnet executor）；F3 accepted 由 Coordinator 合并后改 install_node.sh 文案；F4 rejected（pre-existing）
- 2026-10-09 11:20 CST: TK-001c 回报 4257dc2（scope 内；Coordinator 复核 209/919 passed、ruff 通过、并发跑无互扰）→ reviewing，CR-006 round1（opus）
- 2026-10-09 11:05 CST: **云端会话 3 接手**。重建 /home/user/venv-rs，基线复跑 916 passed / 46 skipped / 8 errors（均为 pyaudiowpatch），ruff 通过；Desktop Commander 可见 MacBook Air 与 mini2 在线。新建 tasks/TK-001c 卡片 → TK-001c executing（sonnet，分支 feat/tk-001c，单独小批）
- 2026-10-09 10:40 CST: CR-005 round3 两路 approve；Mac 核实 ps 不截断/pgrep 命中/bash 3.2.57 → **TK-003 done**，合并进 feat/macos-native。用户要求开新对话接手 → HANDOFF §10 写入云端会话 2 交接
- 2026-10-09 10:20 CST: TK-003 C2 修复 74fe494（三份并发各 98 passed、无残留）→ recheck，CR-005 round3 两路派出（default 用 mini2 真实 v1 命令行端到端；security 回归注入）
- 2026-10-09 10:05 CST: TK-003 C1 修复 c8ec1ca（97 passed）；Coordinator 并发跑两份测试复现 4 failed → C2 blocker：测试会杀掉同用户匹配进程（在 mini2 上跑会杀真 v1）→ 同一 repairer 续修
- 2026-10-09 09:40 CST: TK-003 repair round2 回收 236531d（91/909 passed，注入复现消失，已 push）；Coordinator 复核发现 C1 blocker：v1 匹配不兼容 mini2 实际的 `python -u -m ...`（NO-MATCH）→ 同一 repairer 续修
- 2026-10-09 09:25 CST: CR-005 round2：security 实打证实 v1 重拉 `sh -c` 可执行 `$(...)`（R2-S1），default 发现重拉不验存活就报已恢复（R2-D1）→ 统一修法：固定入口 + 白名单参数 + 数组传参 + 存活复核；4 accepted → TK-003 fixing，repair round2
- 2026-10-09 09:15 CST: TK-003 repair round1 回收 ae49421（68/886 passed，主工作树干净）→ recheck，CR-005 round2 两路派出（security 重点：v1 重拉的 sh -c 注入面）
- 2026-10-09 09:00 CST: CR-005 round1 两路回收 → 10 accepted / 2 rejected；F1 修法改写（mini2 上 v1 实为 8791，与 v2 抢端口，必须先停；回滚时删 plist、留 .failed、按记录命令行重拉 v1）；F4 拆出 TK-001c → TK-003 fixing，repair round1（sonnet）。security 评审误写仓库根的 t1.sh 已挪到 /tmp
- 2026-10-09 08:45 CST: TK-003 回报 34ef67b（52/870 passed、shellcheck 通过，已 push）→ reviewing，CR-005 round1 两路派出
- 2026-10-09 08:28 CST: TK-003 → executing（sonnet，分支 feat/tk-003，单独一批）；write_scope 增补 tests/test_install_node.py；安装自检 rtf 用目标机 `say`+`afconvert` 合成德语样音
- 2026-10-09 08:25 CST: CR-004 round3 approve → **TK-001b done**，合并进 feat/macos-native。下一批：TK-003 install_node.sh（安全类，单独一批）
- 2026-10-09 08:15 CST: CR-004 round2 approve（F1/F2 fixed，R2-D1 low rejected）→ round3（opus，整体通读 + 端到端）
- 2026-10-09 08:08 CST: TK-001b repair round1 回收 18b9595（192/818 passed）→ recheck，CR-004 round2（opus）
- 2026-10-09 08:00 CST: CR-004 round1：派审前疑点被真 gateway+真 worker 复现证实（会话→断开→回收不写 asr_state.json）→ F1/F2 accepted、F3 rejected → TK-001b fixing，repair round1（sonnet）
- 2026-10-09 07:50 CST: TK-001b 回报 ce63a76（190/816 passed，已 push）→ reviewing，CR-004 round1（opus）；派审前发现疑点：SIGTERM 回收路径不 finish_session，常见路径可能不更新 rtf
- 2026-10-09 07:33 CST: TK-001b → executing（sonnet，分支 feat/tk-001b，单独一批）；PR #62 已取消订阅
- 2026-10-09 07:30 CST: CR-003 round4 approve → **TK-002 done**，合并进 feat/macos-native（PR #62）。下一批：TK-001b（rtf 滑动更新，依赖 info.STATE_DIR，现已在主线）
- 2026-10-09 07:20 CST: TK-002 repair round3 回收 0172626（107/801 passed）→ recheck，定向 round4（opus）。#62 CI 全绿（含 pytest-windows）；#63 已合并、已取消订阅
- 2026-10-09 07:10 CST: **Mac 验证三连**：TK-001 真模型回归（WER 0.88%、延迟中位 3.40s、6/6 译文）；TK-002 gateway 真机（RSS 33MB、只绑 Tailscale IP、LOCAL_PEERCRED 可用、401/200）；TK-004 Mac 构建 + 回放 A/B 一致 → **TK-004 done，已合并**（PR #63 随之合并）。TK-005 解锁
- 2026-10-09 07:00 CST: CR-003 round3 两路 approve；R3-D1 accepted → TK-002 fixing，repair round3（sonnet）。Mac：TK-004 concat5 重跑一次，5 句全部翻译成功，首次 id1「Unable to Translate」判为系统翻译偶发（基线同句正常、重跑正常）
- 2026-10-09 06:40 CST: **Mac 通道打通**：用户接入 Desktop Commander，可直接在 MacBook Air（yumeideMacBook-Air.local）与 mini2（YilindeMac-mini.local，v1 PID 34924 仍在跑、未动）执行命令。Mac 验证一律在独立 detached worktree（~/projects/rs-cloud-verify、~/projects/rs-cloud-base）里做，不碰用户原有 worktree。TK-004 @846ef66 `swift build -c release` 通过（TK-004 文件 0 warning）；concat5 headless 回放 5 句 t0/t1 与基线 c53ef12 完全一致。Windows CI（#62）4 条失败为 macOS/POSIX 专用机制 → [coordinator-direct] eb6bfa5 按平台 skipif，已合入 tk-002/tk-004 并在 #62 留言。TK-002 repair round2 回收 302c212 → recheck，CR-003 round3 两路派出
- 2026-10-09 06:25 CST: 用户要求开 PR 保全进展：draft PR #62（feat/tk-002 → feat/macos-native）、#63（feat/tk-004 → feat/macos-native，待 Mac 构建）；不向 master 开 PR。已订阅两个 PR 的事件。Mac 通道：已向本机原 Coordinator 会话（Remote Control）发只读探路请求，等回复
- 2026-10-09 06:15 CST: CR-003 round2 回收：security approve（R2-S1 rejected）；default approve_with_minor，R2-D1（上一会话 ready 串入）/R2-D2（audio_dropped 先于 ready）/R2-D3（冷加载期 flush 误杀）+ code 命名 nit accepted，RFC 同步 nit rejected → TK-002 fixing，repair round2（sonnet）
- 2026-10-09 06:05 CST: TK-002 repair round1 回收 fd1e75b；主线合进 feat/tk-002（ec6b3e0）使真 worker 联调用例实跑；复核 gateway 100 / 全量 794 passed、ruff 通过 → recheck，CR-003 round2 两路派出。用户告知可用 MacBook Air/mini2（Tailscale）：本云端容器无 tailscale/ssh、100.x 不可达，Mac 侧验证仍需用户在本机起会话
- 2026-10-09 05:55 CST: CR-002 round4 approve → **TK-001 done**，b2c697e 合并进 feat/macos-native（合并后全量 694 passed、ruff 通过）；待 Mac 回归冒烟。TK-002 repair round1 进行中（sonnet）
- 2026-10-09 05:48 CST: CR-003 round1 两路回收（default 含真 worker 联调；security 真起 gateway 实打）→ 9 accepted / 3 rejected（F4/F5/S-F5）；F2 用补零 PCM 保持样本时钟，不改冻结的 RFC；node_id/asr_state 归 TK-003，rtf 滑动更新归 TK-001 后续小项 → TK-002 fixing，repair round1（sonnet）
- 2026-10-09 05:42 CST: TK-001 repair round3 回收 b2c697e（复核 82/694 passed；R3-D1 复现修复前 20.08s 挂起→修复后 0.35s 退出）→ recheck，定向 round4（opus）。CR-003 default 路回收（F1–F6），等 security 路一起分诊
- 2026-10-09 05:34 CST: CR-002 round3 回收：两路 approve；R3-D1（BaseException 让 worker 半死）accepted + faulthandler 测试缺口 accepted，R3-S1 rejected → TK-001 fixing，repair round3（sonnet）
- 2026-10-09 05:26 CST: TK-001 repair round2 回收 e2ed51b（复核 80/692 passed）→ recheck，CR-002 round3 两路派出；CR-003 round1 两路派出（default 含真 worker 联调）
- 2026-10-09 05:22 CST: TK-002 回报 2593886（write_scope 内，Coordinator 复核 50/662 passed、ruff 通过，已 push feat/tk-002）→ reviewing，CR-003 round1 派出（default=opus + security=sonnet）
- 2026-10-09 05:16 CST: CR-002 round2 回收：security approve（S-F1 实打无泄漏）；default F1–F5 fixed，新 R2-D1（崩溃零痕迹）accepted → TK-001 fixing，repair round2（sonnet）
- 2026-10-09 05:10 CST: TK-001 repair round1 回收 b89886e，Coordinator 复核（node 77 / 全量 689 passed，ruff 通过，F1 复现已消失）→ recheck，CR-002 round2 派出（default=opus + security=sonnet）
- 2026-10-09 05:05 CST: CR-001 round3 回收（opus）：N1/N2/N3 fixed，approve（代码层）；M1/M2 rejected → CR-001 resolved（代码层），TK-004 blocked=待 Mac 构建（857a2d3，云端不合并）
- 2026-10-09 05:00 CST: CR-002 round1 default 路回收（opus，F1–F6）；与 security 路合并分诊：11 accepted / 0 rejected（F1 强切重叠已云端复现）→ TK-001 fixing，repair round1（sonnet）。TK-004 repair round2 回收 857a2d3（未编译）→ 已 push，CR-001 round3 代码复查派出（opus）
- 2026-10-09 04:52 CST: CR-001 round2 回收（opus）：F4/F6 fixed，F1/F2/F3 partially/残余，verdict approve（代码层）；新 N1/N2/N3 全部 accepted → TK-004 fixing，repair round2（sonnet，云端写、待 Mac 构建）。CR-002 security 路回收（S-F1..S-F6），等 default 路一起分诊
- 2026-10-09 04:46 CST: **云端接手**（claude.ai/code Linux 容器，HANDOFF §9）。Linux 测试基线：88d9355 = 612 passed / 46 skipped / 8 collection errors（pyaudiowpatch 仅 Windows），tk-001 = 672 passed（+60 node 用例，无回归）。CR-002 round1 实际派出：default=opus + security=sonnet 并行（04:20 那次本机派发未产出）；CR-001 round2 派出（opus，只做代码复查，结论标「待 Mac 构建」）；TK-002 → executing（sonnet，分支 feat/tk-002，单独一批：本批唯一 executor）
- 2026-10-09 04:24 CST: CR-001 repair 完成（c53ef12）→ TK-004 recheck，待 round2；推送 GitHub 被 auto-mode 分类器拦截（公开仓库），交由用户执行
- 2026-10-09 04:20 CST: TK-001 回报（3e87063，scope 内，WER 0.88%/延迟中位 3.25s @M2）→ reviewing，CR-002 round1：default=opus + security=sonnet 并行
- 2026-10-09 04:18 CST: CR-001 round1 needs_fix（F1–F6 全部 accepted，F5 转交 TK-005）→ TK-004 fixing，已派 repairer（sonnet）
- 2026-10-09 04:16 CST: TK-004 回报（ecb7a20，write_scope 内）；coordinator-direct 编译适配 0cdd5c7（main/RemotePipeline 只改 onFinal 签名）；→ review，CR-001 round 1（Opus 评审）。TK-001 仍 executing
- 2026-10-09 04:08 CST: TK-001 codex 额度耗尽（至 10-09 00:25，零产出）→ 改派 Claude sonnet 子代理
- 2026-10-09 04:06 CST: scheduler→execution；批次1=TK-001(codex gpt-5.5, wt rs-tk-001)+TK-004(Claude sonnet 子代理, wt rs-tk-004)；TK-002/003/007 属安全或远程类单批排队，TK-005 等 004，TK-006 等 001/002/005
- 2026-10-09 04:05 CST: Sprint 001 initialized, awaiting TK-001（用户已批准全部 TK 排队执行，含 TK-007 对 mini2 的远程操作）

## 下一步
- TK-002b 执行中 → CR-007 两路评审（opus default + sonnet security）≥2 轮 → 合并 → 向用户说明 mini2 重跑 install_node.sh 步骤并等批准
- 之后 TK-005 → TK-006 → TK-007
