# TK-001 Groq 429 样本

状态：**待协调者真机测量**。执行者没有 key，也没有发请求。下表留空，只从 `groq-429-sample.json` 抄，并且先确认文件里没有 key 原文。

这一轮故意打满的是**每分钟请求数**（24 次、每段 1 秒、12 路并发），不是每小时音频秒。本地记账 `max(10, 时长)`，24 次大约记 **240 音频秒**（免费档每小时 7200 秒，这一轮本身打不满音频额度）。429 的原因预期是请求数。音频秒窗口只能从头里另一条 reset 看，不能从「被 429 了」反推。

| 问题 | 答案 |
|---|---|
| 有没有 `retry-after`，值是什么 | 待协调者真机测量 |
| 每个 `x-ratelimit-*` 的名字和值 | 待协调者真机测量 |
| 请求次数窗口：滚动还是整分 | 待协调者真机测量 |
| 音频秒窗口：滚动还是整点 | 待协调者真机测量 |

原始头（打码后粘贴）：待协调者真机测量。

## 怎么读脚本给的猜测

`request_window_guess` 只解释请求次数那条 reset：先看 `x-ratelimit-reset-requests`，没有再看 `retry-after`。容差 2 秒。取值：

- `clock-minute` / `clock-hour` / `clock-day`：剩余时间和「离整分 / 整点 / 整天还剩多少」对得上
- `rolling-minute` / `rolling-hour` / `rolling-day`：剩余时间和「60 / 3600 / 86400 减去本轮第一发到这条响应的时间」对得上
- `ambiguous`：两边都贴上。开在整分或整点时就会这样，**换一个时刻重跑**，不要挑一个写进结论
- `insufficient`：没有可解析的时长，或哪边都不贴。HTTP 日期形式的 `retry-after` 会落在这里

音频秒那一行看 `audio_header_names`，以及 `reset_guesses` 里名字带 audio / second 的那条。没有这样的字段就写「样本里没有这个字段」。不要把 `request_window_guess` 抄进音频秒那一格。

`reset_guesses` 是证据。和头对不上时以原始头为准，并在表下写一句为什么不采脚本的猜测。

## 协调者命令

key 只读 `~/.config/rslite/groq.key`。脚本不读环境变量。输出里 key 换成 `***`，`Authorization` 整段换成 `***`。不要把 key 贴进这页、终端回显或提交说明。

先在**不靠近整分、也不靠近整点**的时刻开跑（秒数大约 10–40，并且离开整点至少几分钟），否则滚动和整点分不开。

```bash
PY="$HOME/projects/rs-mac-native/venv/bin/python"
PROBE="$HOME/lanes/wt/rs-s2-tk001/.governance/sprints-002-cloud-asr-first/probes/groq_429_probe.py"

# 不读 key、不访问网络
"$PY" "$PROBE" --self-check

# 真跑。超时 30s（要等得及收回 429；产品超时 10s 不适用于这次取样）。
# 退出码：0 拿到 429，2 没有 429，1 用法错误 / 没有 key / 出现 401。
"$PY" "$PROBE" \
  --wav "$HOME/projects/rs-mac-native-data/concat5.wav" \
  --out "$HOME/lanes/wt/rs-s2-tk001/.governance/sprints-002-cloud-asr-first/probes/groq-429-sample.json"
```

401 表示 key 被拒。即使 JSON 里同时有 429，也先核对 key 文件，再决定要不要采用。没有 429 就不要填这张表。

脚本不会改本文件。

## 实测结果（协调者，2026-10-11 11:21 CEST，MBA，key 打码）

原始样本：`groq-429-sample.json`（已核对不含 key）。24 次 × 1s 音频、12 路并发 → 200×20、429×4。

| 问题 | 结论 | 证据 |
|---|---|---|
| 429 带不带 `retry-after` | **带**，本次 `3`（秒，整数） | `has_retry_after=true` |
| 打满的是哪个窗口 | **每分钟请求数**（RPM 20） | body：`on requests per minute (RPM): Limit 20, Used 20` |
| `x-ratelimit-*` 指什么 | 只有 requests 三项：`limit=2000`、`remaining=1980`、`reset=14m24s` → 是**每日请求数**，不是每分钟 | 2000 = 控制台每日上限 |
| 每日请求窗口语义 | **令牌桶式持续回补**，不是自然日清零：用掉 20 次，reset=864s = 20 × 43.2s（86400/2000） | `x-ratelimit-reset-requests` |
| 音频秒窗口 | **响应头不提供**（`audio_header_names=[]`），只能本地记账；滚动/整点无法从响应判断 | — |

### 对实现的影响（交给 TK-003 / TK-006 / TK-004）
1. 429 冷却用 `retry-after`（整数秒）；缺省 60s 的兜底保留（RFC 已定）。
2. `x-ratelimit-remaining-requests` 只用来校正**24 小时请求数**窗口，不能拿去校正 1 分钟窗口。
3. 1 分钟请求数（20）与 1 小时 / 24 小时音频秒（7200 / 28800）只能靠本地记账；本地用滚动窗口（保守，与令牌桶回补相比只会更早冷却，不会更晚）。
4. 验收 Done 6 的「音频秒偏差 ≤5%」按 requirement 写明的退回方式核对：本地记账 = Σ max(10, duration)。
