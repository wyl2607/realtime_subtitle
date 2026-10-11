# CURSOR — sprints-002-cloud-asr-first

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `execution`
- current_tk: `TK-005,TK-006B,TK-004`
- current_cr: `—`
- current_round: `—`
- escalated: `false`

## 进度快照

| TK | 标题 | 状态 |
|----|------|------|
| TK-001 | 探针：rslite 词时间分段 + Groq 429 样本 | done |
| TK-002 | P9 夹具 + 云端状态机 + Provider 抽象 | done |
| TK-003 | rslite 云端：CloudClient + QuotaLedger + key/开关 | done |
| TK-004 | rslite CloudSegmenter + 分级路由接入 HybridEngine | executing |
| TK-005 | Windows 客户端句子带时间 + 样本时钟连续性 | executing |
| TK-006 | Python remote/：node_client + router + line_store + cloud_client + quota | split |
| TK-007 | Windows 面板 + 接线 + --client 下发 | planned |
| TK-008 | 文档 + v3.2.0 + 端到端验收 | planned |

## 最近动作
- 2026-10-11 18:08 CST: delivery TK-006A / CR-006A resolved（2 轮：grok+agy 评审 2 blocker（钉 IP、ALL_PROXY）→ grok 修复 → 复验绿）；合并后全量 1100 passed
- 2026-10-11 17:59 CST: scheduler 批次 4 = TK-004（依赖 001/002/003 已齐），lane-chain grok>agy；A3' rms 0.005
- 2026-10-11 17:59 CST: delivery TK-003 / CR-003 resolved（2 轮：agy+grok 评审 + 协调者 429 实测 C1 → grok 修复 → 复验绿）；已合并
- 2026-10-11 17:37 CST: delivery TK-001 / CR-001 resolved：rslite 分段走 A3'（能量 VAD rms 0.005 + final 校正）；Groq 429 带 retry-after，x-ratelimit 为每日请求令牌桶，无音频秒头；已合并
- 2026-10-11 17:23 CST: scheduler 批次 3：TK-006 split → 006A(agy) / 006B(grok)，写入范围互不覆盖且不碰 TK-005；codex 额度 14:14 前不可用
- 2026-10-11 17:05 CST: scheduler 批次 2 = TK-003 单独成批（安全类，互斥队列），codex；与批次 1 剩余 TK-001/005 写入范围不重叠
- 2026-10-11 17:05 CST: delivery TK-002 / CR-002 resolved（2 轮：agy 评审 2 accepted → agy 修复 → 复验绿），已合并 feat/macos-native；风险：无
- 2026-10-11 16:38 CST: scheduler → execution：批次 1 = TK-001(grok) / TK-002(codex) / TK-005(codex)，worktree ~/lanes/wt/rs-s2-tk00X
- 2026-10-11 16:37 CST: Sprint 002 initialized, awaiting TK-001

## 下一步
- /sprint-tick → Scheduler：第一批 ready = TK-001 / TK-002 / TK-005（写入范围互不覆盖），并行派给非 Claude lane。
