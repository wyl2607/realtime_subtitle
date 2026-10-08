# REVIEWS — sprints-001-remote-offload

> 状态枚举：`open | needs_fix | resolved | rejected | escalated`
> 每条 CR 的详情写在 `reviews/CR-NNN.md`，本表只做索引。

| CR | TK | rounds | status | last_action |
|----|----|--------|--------|-------------|
| CR-002 | TK-001 | 0 | open | 2026-10-09 04:20 CST: created；executor 3e87063（M2 冒烟 WER 0.88%、延迟中位 3.25s）|
| CR-001 | TK-004 | 1 | needs_fix | 2026-10-09 04:24 CST: repair 完成 c53ef12（F1–F4、F6），待 round2 复查 |

## 字段说明
- **rounds**：已完成的 review 轮数（initial = 1，第一次 recheck = 2，以此类推）
- **status**：open（还没开始 review）/ needs_fix（有已接受的 finding 待修）/ resolved（已收敛）/ rejected（finding 全部驳回且有记录）/ escalated（超过 5 轮，熔断）
