# v3.2.0 Groq key 验收判据与源码溯源

这份说明只列当前源码可证明的行为。无法从本次允许收集的 artifacts 验证的项目明确标为 `NOT_EVALUABLE`，不补造日志或字段。

| 验收判据 | 脚本 | 源码依据 |
|---|---|---|
| `--set-groq-key`、`--clear-groq-key`、`--cloud on/off` 是有效 CLI；key 从 stdin 读取 | `run_mac_key.sh`、`run_mini2_hour.sh` | `macos-native/Sources/rslite/main.swift:96-177` |
| fakehome 的 `RSLITE_CONFIG_DIR` 同时隔离 key、开关与账本 | 两个脚本均以 `HOME=... RSLITE_CONFIG_DIR=...` 前缀运行 | `macos-native/Sources/rslite/CloudConfig.swift:22-43`; `macos-native/Sources/rslite/QuotaLedger.swift:101-103` |
| headless 的事件包含 `volatile`、`final`、`translation`、`status` 与 P5 `replace`；模式 status 文字为 `模式：<label>` | `run_mac_key.sh` cloud/recover | `macos-native/Sources/rslite/main.swift:203-247` |
| 云端模式显示 `云端·Groq` | cloud 首个模式 status 必须为 `模式：云端·Groq` | `macos-native/Sources/rslite/HybridEngine.swift:22-40`; `macos-native/Sources/rslite/main.swift:230-232` |
| concat5 质量判据：`hybrid_score.py` 的 `problems == []` 且实际 `final_wer <= 0.0263` | `run_mac_key.sh` cloud | `scripts/bench/hybrid_score.py:220-250,269-277`（输出字段/评分）；阈值来自题设与 `.governance/sprints-002-cloud-asr-first/acceptance/plan.md:53-60` |
| Done 5 开关恢复后云端状态重新出现，且没有重复/丢句 | `run_mac_key.sh` recover | `macos-native/Sources/rslite/CloudSegmenter.swift:809-830,1027-1063`; `scripts/bench/hybrid_score.py:192-250` |
| `--mode auto` 在 key 存在且开关开启时会建立云端调度 | `run_mini2_hour.sh` | `macos-native/Sources/rslite/HybridEngine.swift:42-46,753-772,797-799`; `macos-native/Sources/rslite/CloudSegmenter.swift:588-603` |
| 账本文件及字段 | `run_mini2_hour.sh` 收集 `quota-ledger.json`，统计 `events[].billed_s` | `macos-native/Sources/rslite/QuotaLedger.swift:19-36,101-113` |
| 每次实际发送前按 `max(10, segment.duration)` 记账 | `report.md` 说明公式与证据缺口 | `macos-native/Sources/rslite/CloudSegmenter.swift:646-675`; `macos-native/Sources/rslite/QuotaLedger.swift:105-113` |
| 成功响应会读取 `x-ratelimit-remaining-requests` 并校正账本 | `report.md`：有 start/end 值才计算 `Delta_R` | `macos-native/Sources/rslite/CloudClient.swift:161-178`; `macos-native/Sources/rslite/CloudSegmenter.swift:678-695`; `macos-native/Sources/rslite/QuotaLedger.swift:116-129,274-285` |
| 429 的真实错误文案与状态语义 | `report.md` 只数 `Groq HTTP 429` | `macos-native/Sources/rslite/CloudClient.swift:32-47,309-319`; `macos-native/Sources/rslite/Provider.swift:157-173` |

## 不可从现有事件/账本严格验收的项目

- Done 3 的 `t - t1` 不能被脚本作为严格延迟：`t` 是 `runHeadless` 在管线启动前建立的 `ContinuousClock` 墙钟相对秒（`macos-native/Sources/rslite/main.swift:203,336-345,347-359`）；`t1` 是回调带回的样本时间，而统一样本时钟从 `AudioFanout` 已送出的样本数推进（`macos-native/Sources/rslite/AudioFanout.swift:4-13,127-134`）。`FileSource` 确实按音频时长实时回放（`macos-native/Sources/rslite/FileSource.swift:45-50`），但 headless 事件没有记录第一样本相对 `startedAt` 的共同零点。脚本保存未对齐候选值，却以 `NOT_EVALUABLE` 退出，绝不把它与 2.0s 门槛硬比较。

- 云端 P5 的 `final` 事件恰好是 `id: null`：`main.swift` 的 `onNodeFinal` 在 `:234-237,239-241` 写入 `id: nil`。所以“云端 final 且 id 非空”的筛选条件没有源码依据；`id` 非空的 final 是 `onFinal` 路径（`:215-218`）。

- Done 6 的逐段等式 `billed_s == max(10, segment.duration)` 无法在运行后逐条重算：源码把 `segment.duration` 传到记账方法，但账本仅写 `ts` 和 `billed_s`（`QuotaLedger.swift:29-41,105-113`），headless events 也不输出上传段 duration。报告会验证账本 JSON 与 `billed_s >= 10` 的可见形状，并将逐段等式标为 `NOT_EVALUABLE`。

- `x-ratelimit-remaining-requests` 被读取后只作为内存中的 `remainingRequests`/账本 `used_requests` 校正，不由默认 `CloudClient` logger 写入 stderr（`CloudClient.swift:74-84,161-178`; `CloudSegmenter.swift:689-691`）。没有开始和结束两个实际记录值时，脚本不生成虚构的 `Delta_R`，也不声称 `N_local == Delta_R`。

- `scripts/bench/asr_bench.py` 是本地模型基准，直接加载 FLEURS parquet 并调用 MLX/faster-whisper（`scripts/bench/asr_bench.py:43-57,74-96,107-146`）；它不调用 `rslite` 或 Groq，不能作为本任务的云端 FLEURS 60 条评分器。
