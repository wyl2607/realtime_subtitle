# REVIEWS — sprints-002-cloud-asr-first

> 状态枚举：`open | needs_fix | resolved | rejected | escalated`
> 每条 CR 的详情写在 `reviews/CR-NNN.md`，本表只做索引。CR 编号与 TK 编号一一对应（CR-001 ↔ TK-001），在该 TK 进入 review 时创建。

| CR | TK | rounds | status | last_action |
|----|----|--------|--------|-------------|
| CR-001 | TK-001 | 1 | resolved | 2026-10-11 17:37 CST: 协调者评审，1 条当场修复；已合并 |
| CR-002 | TK-002 | 2 | resolved | 2026-10-11 17:05 CST: round-2 recheck clean，已合并 feat/macos-native |
| CR-003 | TK-003 | 2 | resolved | 2026-10-11 17:59 CST: round-2 recheck clean，已合并 |
| CR-006A | TK-006A | 2 | resolved | 2026-10-11 18:08 CST: round-2 recheck clean，已合并；全量 1100 passed |
| CR-005 | TK-005 | 1 | needs_fix | 2026-10-11 17:53 CST: initial 7 accepted → repair (lane-chain grok>agy) |

## 字段说明
- **rounds**：已完成 review 轮数（initial = 1，第一次 recheck = 2，以此类推）
- **status**：`open` / `needs_fix` / `resolved` / `rejected` / `escalated`（轮次 > 5 熔断）
- **last_action**：CST 时间 + 一句摘要
