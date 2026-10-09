# REVIEWS — sprints-001-remote-offload

> 状态枚举：`open | needs_fix | resolved | rejected | escalated`
> 每条 CR 的详情写在 `reviews/CR-NNN.md`，本表只做索引。

| CR | TK | rounds | status | last_action |
|----|----|--------|--------|-------------|
| CR-008 | TK-006 | 1 | open | 2026-10-09 16:40 CEST: round1 派出（codex default） |
| CR-007 | TK-002b | 1 | needs_fix | 2026-10-09 16:20 CEST: round1 default needs_fix（D2 accepted、D1 rejected）/ security approve（F01 nit accepted）→ Gemini flash 修 |
| CR-006 | TK-001c | 2 | resolved | 2026-10-09 12:00 CST: round2 approve_with_minor；R2-F1 coordinator-direct 0691d95；已合并；F3 文案 efa713c |
| CR-005 | TK-003 | 3 | resolved | 2026-10-09 14:35 CST: mini2 真机安装 EXIT=0；发现 TCP 监听 blocker 与自检漏洞，另开 TK（HANDOFF §11.2） |
| CR-004 | TK-001b | 3 | resolved | 2026-10-09 08:25 CST: round3 approve（端到端 rtf None→0.0101→0.037 滑动）；三轮收敛，已合并 |
| CR-003 | TK-002 | 4 | resolved | 2026-10-09 07:30 CST: round4 approve；四轮收敛（15 accepted / 6 rejected），已合并（PR #62） |
| CR-002 | TK-001 | 4 | resolved | 2026-10-09 05:55 CST: round4 approve；四轮收敛（14 accepted / 2 rejected），b2c697e 已合进 feat/macos-native |
| CR-001 | TK-004 | 3 | resolved | 2026-10-09 07:10 CST: Mac 构建 + concat5 回放 A/B 通过（t0/t1 与基线逐字节一致）→ 已合并进 feat/macos-native |

## 字段说明
- **rounds**：已完成的 review 轮数（initial = 1，第一次 recheck = 2，以此类推）
- **status**：open（还没开始 review）/ needs_fix（有已接受的 finding 待修）/ resolved（已收敛）/ rejected（finding 全部驳回且有记录）/ escalated（超过 5 轮，熔断）
