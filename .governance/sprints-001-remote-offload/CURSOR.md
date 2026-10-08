# CURSOR — sprints-001-remote-offload

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `review`
- current_tk: `TK-001,TK-004,TK-002`
- current_cr: `CR-002,CR-001`
- current_round: `1`
- escalated: `false`

## 进度快照

| TK | 标题 | 状态 |
|----|------|------|
| TK-001 | 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译 | fixing |
| TK-002 | 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限 | executing |
| TK-003 | 一键安装 install_node.sh | planned |
| TK-004 | rslite：带时间句子 + AudioFanout + 混合替换 + 单实例 | fixing |
| TK-005 | rslite：NodeClient v2 + NodeRouter + 本机档门槛 | planned |
| TK-006 | 清理 v1 + 文档 | planned |
| TK-007 | 端到端验收 + 五组功耗 + 结果文档 | planned |

## 最近动作
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
