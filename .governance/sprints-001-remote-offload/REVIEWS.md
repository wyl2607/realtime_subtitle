# REVIEWS — sprints-001-remote-offload

> 状态枚举：`open | needs_fix | resolved | rejected | escalated`
> 每条 CR 的详情写在 `reviews/CR-NNN.md`，本表只做索引。

| CR | TK | rounds | status | last_action |
|----|----|--------|--------|-------------|
| CR-004 | TK-001b | 1 | needs_fix | 2026-10-09 08:00 CST: round1 REQUEST_CHANGES（F1 high 实测复现）；2 accepted / 1 rejected → repair round1 |
| CR-003 | TK-002 | 4 | resolved | 2026-10-09 07:30 CST: round4 approve；四轮收敛（15 accepted / 6 rejected），已合并（PR #62） |
| CR-002 | TK-001 | 4 | resolved | 2026-10-09 05:55 CST: round4 approve；四轮收敛（14 accepted / 2 rejected），b2c697e 已合进 feat/macos-native |
| CR-001 | TK-004 | 3 | resolved | 2026-10-09 07:10 CST: Mac 构建 + concat5 回放 A/B 通过（t0/t1 与基线逐字节一致）→ 已合并进 feat/macos-native |

## 字段说明
- **rounds**：已完成的 review 轮数（initial = 1，第一次 recheck = 2，以此类推）
- **status**：open（还没开始 review）/ needs_fix（有已接受的 finding 待修）/ resolved（已收敛）/ rejected（finding 全部驳回且有记录）/ escalated（超过 5 轮，熔断）
