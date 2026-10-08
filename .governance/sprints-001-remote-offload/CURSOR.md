# CURSOR — sprints-001-remote-offload

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `review`
- current_tk: `TK-002`
- current_cr: `CR-003`
- current_round: `1`
- escalated: `false`

## 进度快照

| TK | 标题 | 状态 |
|----|------|------|
| TK-001 | 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译 | done |
| TK-002 | 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限 | recheck |
| TK-003 | 一键安装 install_node.sh | planned |
| TK-004 | rslite：带时间句子 + AudioFanout + 混合替换 + 单实例 | blocked（待 Mac 构建） |
| TK-005 | rslite：NodeClient v2 + NodeRouter + 本机档门槛 | planned |
| TK-006 | 清理 v1 + 文档 | planned |
| TK-007 | 端到端验收 + 五组功耗 + 结果文档 | planned |

## 最近动作
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
- 回收 CR-002 round1 两路 + CR-001 round2 → 分诊；TK-001 修复走 round2/3；TK-004 代码层收敛后标「待 Mac 构建」，不在云端合并
- TK-002 回报 → CR-003 评审（default=opus + security=sonnet）
