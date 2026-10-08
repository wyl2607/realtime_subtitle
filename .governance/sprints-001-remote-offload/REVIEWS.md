# REVIEWS — sprints-001-remote-offload

> 状态枚举：`open | needs_fix | resolved | rejected | escalated`
> 每条 CR 的详情写在 `reviews/CR-NNN.md`，本表只做索引。

| CR | TK | rounds | status | last_action |
|----|----|--------|--------|-------------|
| CR-003 | TK-002 | 0 | open | 2026-10-09 05:22 CST: created；executor 2593886（50 gateway / 662 全量 passed）→ round1 派出 default=opus + security=sonnet |
| CR-002 | TK-001 | 2 | needs_fix | 2026-10-09 05:16 CST: round2 security approve / default needs_fix（R2-D1 accepted）→ repair round2 |
| CR-001 | TK-004 | 3 | resolved | 2026-10-09 05:05 CST: round3 approve（代码层）；M1/M2 rejected（理由见 CR-001.md）；**待 Mac 构建验证后才能合并** |

## 字段说明
- **rounds**：已完成的 review 轮数（initial = 1，第一次 recheck = 2，以此类推）
- **status**：open（还没开始 review）/ needs_fix（有已接受的 finding 待修）/ resolved（已收敛）/ rejected（finding 全部驳回且有记录）/ escalated（超过 5 轮，熔断）
