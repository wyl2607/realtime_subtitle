# TK-007 第 6 条：整机功耗（五组）

## 正式数据：2026-10-10 11:0x CEST（干净一轮）
命令：`sudo bash scripts/bench/power_compare.sh ~/projects/rs-mac-native-data/concat5.wav`（主线 368965c 的五组版）
MacBook Air M2；每组 75s、37 个采样（powermetrics cpu+gpu+ane Combined Power，2s 间隔）；组间冷却 30s；A 组先预热 30s。
测量期间协调者不跑任何 rslite/构建；另一项目的两条 opencode lane 在跑（CPU 占用很低）。

| 组 | 均值 mW | 标准差 | 标准误 | 相对空闲 |
|---|---|---|---|---|
| 空闲 idle | 940 | 1127 | 185 | — |
| B（rslite --mode local） | 1271 | 1383 | 227 | +331 |
| C（rslite --mode auto → mini2） | 1406 | 1832 | 301 | +466 |
| A（Python 桌面字幕 + afplay） | 7969 | 4330 | 712 | **+7029** |
| 混合（rslite --mode hybrid → mini2） | 336 | 446 | 73 | −604 |

C/混合两组路由日志确认选中 mini2（rtt 113/138ms）。原始数据：`tk007_power_20261010/`（*.combined.txt 为逐采样，*.events 为 rslite 输出）。

**结论**
- A 版（Python 桌面字幕）比空闲多约 **7 W**（≈10 倍标准误），rslite 任一模式都比它省 6.5–7.5 W——这是本条的主结论。
- B / C / 混合三组都在空闲 ±0.6 W 内（差值 1–3 倍标准误，混合低于空闲是空闲组后台尖峰所致，max 3534mW）：**单轮数据分不出三者高低**，与 RFC 背景「两条路线都只比空闲多不到 0.1W」一致。要排序需整套重复 ≥2 轮（脚本头注释已写明）。
- A 组用 afplay 外放、其它组 file: 源静音，A 含少量播放功耗。

## 作废的轮次（留档说明，不作结论）
- 10-09 23:28 三组旧版（idle 5036 / local 9400 / remote 7151）：旧版脚本只有三组，且机器忙（另一会话多条 lane 在跑），空闲基线 5W，不可比。
- 10-10 10:42 五组：空闲组期间协调者的第 2 条探针在跑 rslite，空闲高于 B/C/混合，作废。
- 10-10 10:5x 五组：A 组中途终端标签被关闭，脚本中断，作废。
