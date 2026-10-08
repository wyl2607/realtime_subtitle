# REVIEWS — sprints-001-remote-offload

> 状态枚举：`open | needs_fix | resolved | rejected | escalated`
> 每条 CR 的详情写在 `reviews/CR-NNN.md`，本表只做索引。

| CR | TK | rounds | status | last_action |
|----|----|--------|--------|-------------|
| CR-003 | TK-002 | 1 | needs_fix | 2026-10-09 05:48 CST: round1 两路要求修改；9 accepted / 3 rejected → repair round1（sonnet） |
| CR-002 | TK-001 | 3 | needs_fix | 2026-10-09 05:42 CST: repair b2c697e 复核（复现 20.08s→0.35s）→ 定向 round4（opus） |
| CR-001 | TK-004 | 3 | resolved | 2026-10-09 05:05 CST: round3 approve（代码层）；M1/M2 rejected（理由见 CR-001.md）；**待 Mac 构建验证后才能合并** |

## 字段说明
- **rounds**：已完成的 review 轮数（initial = 1，第一次 recheck = 2，以此类推）
- **status**：open（还没开始 review）/ needs_fix（有已接受的 finding 待修）/ resolved（已收敛）/ rejected（finding 全部驳回且有记录）/ escalated（超过 5 轮，熔断）
