# CURSOR — sprints-002-cloud-asr-first

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `execution`
- current_tk: `TK-008`
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
| TK-005 | Windows 客户端句子带时间 + 样本时钟连续性 | done |
| TK-006 | Python remote/：node_client + router + line_store + cloud_client + quota | done |
| TK-007 | Windows 面板 + 接线 + --client 下发 | executing |
| TK-008 | 文档 + v3.2.0 + 端到端验收 | executing |

## 最近动作
- 2026-10-11 19:19 CST: scheduler 批次 6 = TK-008 文档部分提前开工（行为已定、草稿与 Groq 政策就绪），lane-chain agy>grok；验收部分待 004/007 合并
- 2026-10-11 18:42 CST: scheduler 批次 5 = TK-007（安全类，单独成批），lane-chain grok>agy；与 TK-004 写入范围不重叠
- 2026-10-11 18:41 CST: delivery TK-005 / CR-005 resolved（7 条：协调者 3 + agy 4 → grok 修复 → 复验）；合并后全量 1173 passed
- 2026-10-11 18:36 CST: delivery TK-006B / CR-006B resolved（netrc 覆盖 Bearer、账本 24h、10s 墙钟）；父 TK-006 done（A/B 均 done，合并后全量 1161 passed）；TK-007 解锁
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
- 2026-10-11 13:38 CEST: TK-004 done（CR-004 R1 状态机 keyInvalid + R2 grok 修 T1/ARCH-01/02/04/05/F1/F2/F5；协调者复核 build+selftest OK、pytest 1060 passed、ruff 绿；merge 38dbf3c）。TK-008 文档部分 merge 53a169a（协调者改 4 处事实）。TK-007 评审中（agy default + grok security）
- 2026-10-11 14:21 CEST: TK-007 done（CR-007 R1 grok 修 10 条，各带回归；协调者复跑 1213 passed / 31 skipped、ruff 绿；merge 09a0cc4）。剩 TK-008 真机验收（明天）
- 2026-10-11 14:25 CEST: v3.2.0-beta 预发布（tag 69c9604，feat/macos-native，master 未动）。Windows 真机 pytest 1026 passed，修 3 个测试的 Windows 适配；PushClient 真机幂等通过。剩 TK-008 真机验收（明天）→ 正式 v3.2.0 合 master
- 2026-10-11 16:50 CEST: 真机直播验收（tagesschau24 → rslite tap，节点 yzwdmsnlmm）：稳定 23min；发现节点 Ollama 未运行致每句 translate_failed、info 不反映、UI 挂英文码 → 远程 WMI 启动 ollama serve（用户批准）后恢复（tx 0.18–0.35s）；新开 TK-009 两路派活
