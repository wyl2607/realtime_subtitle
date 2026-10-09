# CURSOR — sprints-001-remote-offload

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `execution`
- current_tk: `TK-002b,TK-005,TK-006,TK-007(prep)`
- current_cr: `—`
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
| TK-002b | gateway：launchd 下 Tailscale CLI 修复 + 安装自检补 TCP | executing |
| TK-001c | worker：translator 写当前真实翻译器名 | done |
| TK-005 | rslite：NodeClient v2 + NodeRouter + 本机档门槛 | executing |
| TK-006 | 清理 v1 + 文档 | executing |
| TK-007 | 端到端验收 + 五组功耗 + 结果文档 | planned |

## 最近动作
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
