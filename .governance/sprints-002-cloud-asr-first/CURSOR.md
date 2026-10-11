# CURSOR — sprints-002-cloud-asr-first

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `execution`
- current_tk: `TK-001,TK-002,TK-005`
- current_cr: `—`
- current_round: `—`
- escalated: `false`

## 进度快照

| TK | 标题 | 状态 |
|----|------|------|
| TK-001 | 探针：rslite 词时间分段 + Groq 429 样本 | executing |
| TK-002 | P9 夹具 + 云端状态机 + Provider 抽象 | done |
| TK-003 | rslite 云端：CloudClient + QuotaLedger + key/开关 | planned |
| TK-004 | rslite CloudSegmenter + 分级路由接入 HybridEngine | planned |
| TK-005 | Windows 客户端句子带时间 + 样本时钟连续性 | executing |
| TK-006 | Python remote/：node_client + router + line_store + cloud_client + quota | planned |
| TK-007 | Windows 面板 + 接线 + --client 下发 | planned |
| TK-008 | 文档 + v3.2.0 + 端到端验收 | planned |

## 最近动作
- 2026-10-11 17:05 CST: delivery TK-002 / CR-002 resolved（2 轮：agy 评审 2 accepted → agy 修复 → 复验绿），已合并 feat/macos-native；风险：无
- 2026-10-11 16:38 CST: scheduler → execution：批次 1 = TK-001(grok) / TK-002(codex) / TK-005(codex)，worktree ~/lanes/wt/rs-s2-tk00X
- 2026-10-11 16:37 CST: Sprint 002 initialized, awaiting TK-001

## 下一步
- /sprint-tick → Scheduler：第一批 ready = TK-001 / TK-002 / TK-005（写入范围互不覆盖），并行派给非 Claude lane。
