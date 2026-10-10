# TK-007 第 6 条：整机功耗对比（2026-10-09 23:28–23:32 CEST）
命令：sudo bash scripts/bench/power_compare.sh ~/projects/rs-mac-native-data/concat5.wav（b869487，v2 参数）
rslite 二进制 22:11 构建（不含 TK-005c；TK-005c 只是清理，不影响功耗路径）；MacBook Air M2；每段 75s、37 个采样（powermetrics cpu+gpu+ane Combined Power）

| 段 | 平均 mW | 相对空闲 |
|---|---|---|
| idle | 5036 | — |
| local（--mode local） | 9400 | +4364 |
| remote（--mode hybrid → mini2，rtt 317ms） | 7151 | +2115 |

结论：外包到 mini2 后字幕增量功耗 −52%，整机 −24%。
注意：测量时后台有 grok 评审进程（主要网络等待），idle 基线可能略高；三段条件一致。
原始数据在 root 的 mktemp 目录（用户终端 tab 2 输出），未拷入仓库。
