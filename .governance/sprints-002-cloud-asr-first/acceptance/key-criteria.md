# v3.2.0 Groq key 验收判据与源码溯源

这份说明只列当前源码可证明的行为。v3.2.0 之前无法从 artifacts 验证的 Done 3 延迟与 Done 6 逐段计费/剩余请求数，本批次补齐了埋点（clock 事件、`dur_s`、stderr `remaining_requests`），脚本据此严格判；埋点缺失时仍退化为 `NOT_EVALUABLE`，不硬算。

| 验收判据 | 脚本 | 源码依据 |
|---|---|---|
| `--set-groq-key`、`--clear-groq-key`、`--cloud on/off` 是有效 CLI；key 从 stdin 读取 | `run_mac_key.sh`、`run_mini2_hour.sh` | `macos-native/Sources/rslite/main.swift:96-177` |
| fakehome 的 `RSLITE_CONFIG_DIR` 同时隔离 key、开关与账本 | 两个脚本均以 `HOME=... RSLITE_CONFIG_DIR=...` 前缀运行 | `macos-native/Sources/rslite/CloudConfig.swift:22-43`; `macos-native/Sources/rslite/QuotaLedger.swift:101-103` |
| headless 的事件包含 `volatile`、`final`、`translation`、`status` 与 P5 `replace`；模式 status 文字为 `模式：<label>` | `run_mac_key.sh` cloud/recover | `macos-native/Sources/rslite/main.swift:203-247` |
| 云端模式显示 `云端·Groq` | cloud 首个模式 status 必须为 `模式：云端·Groq` | `macos-native/Sources/rslite/HybridEngine.swift:22-40`; `macos-native/Sources/rslite/main.swift:230-232` |
| concat5 质量判据：`hybrid_score.py` 的 `problems == []` 且实际 `final_wer <= 0.0263` | `run_mac_key.sh` cloud | `scripts/bench/hybrid_score.py:220-250,269-277`（输出字段/评分）；阈值来自题设与 `.governance/sprints-002-cloud-asr-first/acceptance/plan.md:53-60` |
| Done 5 开关恢复后云端状态重新出现，且没有重复/丢句 | `run_mac_key.sh` recover | `macos-native/Sources/rslite/CloudSegmenter.swift:809-830,1027-1063`; `scripts/bench/hybrid_score.py:192-250` |
| `--mode auto` 在 key 存在且开关开启时会建立云端调度 | `run_mini2_hour.sh` | `macos-native/Sources/rslite/HybridEngine.swift:42-46,753-772,797-799`; `macos-native/Sources/rslite/CloudSegmenter.swift:588-603` |
| 账本文件及字段 | `run_mini2_hour.sh` 收集 `quota-ledger.json`，统计 `events[].billed_s` 与 `events[].dur_s` | `macos-native/Sources/rslite/QuotaLedger.swift:29-66,112-121` |
| 每次实际发送前按 `max(10, segment.duration)` 记账 | `report.md` 逐段重算 `billed_s == max(10, dur_s)` | `macos-native/Sources/rslite/CloudSegmenter.swift:646-675`; `macos-native/Sources/rslite/QuotaLedger.swift:112-121` |
| 成功响应会读取 `x-ratelimit-remaining-requests` 并校正账本；读到后往 stderr 写 `rslite.cloud remaining_requests=<n>` | `report.md`：stderr 首尾成对值才计算 `Delta_R` | `macos-native/Sources/rslite/CloudClient.swift:161-178`; `macos-native/Sources/rslite/CloudSegmenter.swift:678-695`; `macos-native/Sources/rslite/QuotaLedger.swift:123-136,281-295` |
| 429 的真实错误文案与状态语义 | `report.md` 只数 `Groq HTTP 429` | `macos-native/Sources/rslite/CloudClient.swift:32-47,309-319`; `macos-native/Sources/rslite/Provider.swift:157-173` |

## 不可从现有事件/账本严格验收的项目

本批次补齐了三处埋点，下面前三条从 `NOT_EVALUABLE` 改为可由脚本严格判：

- Done 3 的延迟：`main.swift` 的 `runHeadless` 现在在第一块样本送出时写一条 `{"ev":"clock","t":<相对 startedAt 的墙钟秒>,"t0":<该样本的样本时钟秒>}`（`AudioFanout.onFirstSample` 回调 → `main.swift` 的 `writeClockEvent`）。脚本据此算 `latency = t_final - (clock.t + (t1 - clock.t0))`，对云端 final（`id: null` 且 text 以 `[cloud:groq]` 开头）取中位数判 2.0s 门槛。没有 clock 事件仍是 `NOT_EVALUABLE`。

- 云端 P5 的 `final` 事件恰好是 `id: null`：`main.swift` 的 `onNodeFinal` 在 `:234-237,239-241` 写入 `id: nil`。「云端 final」判据是 `id` 为 null 且 text 以 `[cloud:groq]` 开头，不是 id 非空。

- Done 6 的逐段等式：`QuotaLedgerEvent` 现在额外写 `dur_s`（实际音频秒，round 3 位），`billed_s` 仍为 `max(10, duration)`。脚本逐段重算 `billed_s == max(10, dur_s)`（容差 0.001）。旧账本没有 `dur_s` 时照常读出、`dur_s` 为 nil，脚本退化为 `NOT_EVALUABLE`。

- `x-ratelimit-remaining-requests`：`CloudClient` 成功响应读到该头时往 stderr 写 `rslite.cloud remaining_requests=<n>`（只写整数，不写 key / URL / 正文 / 其它响应头）。脚本取 stderr 首尾两个成对值算 `Delta_R`，和 `N_local` 比对。记录不足 2 个仍是 `NOT_EVALUABLE`。

- `scripts/bench/asr_bench.py` 是本地模型基准，直接加载 FLEURS parquet 并调用 MLX/faster-whisper（`scripts/bench/asr_bench.py:43-57,74-96,107-146`）；它不调用 `rslite` 或 Groq，不能作为本任务的云端 FLEURS 60 条评分器。
