# RFC — sprints-002-cloud-asr-first

> 由 `/sdd-rfc` 从 `.governance/specs/002-cloud-asr-first/draft.md` + `draft.review.md`（含 Review Decision）收敛而来，2026-10-11。
> 需求：`specs/002-cloud-asr-first/requirement.md`（含 10-11 用户改定）+ `specs/005-windows-client-nodes/requirement.md`。
> 一旦本 Sprint 启动，本文件冻结；后续变更新开 RFC。Sprint 001 的 P1–P8 不改，本 RFC 新增 P6' / P7' / P9 / P10。

## 背景与动机

- Groq whisper-large-v3 实测 WER 3.62%，全场最好（本机 MLX turbo 4.05%、rslite 本机 7.89%），整句每条 p50 0.52s。
- 用户 10-11 改定：**只有程序配置里填了 Groq key 才启用云端，不读环境变量**；Mac rslite 和 Windows 桌面程序都要做；002 与 005（Windows 客户端接节点）合并，随 v3.2.0 发布。
- 一句话：**两个客户端各自实现同一套骨架**：本机先出字幕；远端精修结果按时间替换本机字幕；远端不可用就降级。远端分两类：云端 Groq（无状态，按段 HTTP 上传）和自己的节点（有状态的 WS v2 流）。

## 协议 / 接口决策

### P7' 分级路由（硬顺序，不进打分）

```
Tier 0  cloud:groq   有 key && 开关开 && 状态 ∈ {可用, 试探}
Tier 1  自己的节点   节点开关开（Windows 默认关；rslite 有 nodes.json 即开）&& 探测健康；层内沿用 001 RoutingDecider 打分，不连自己
Tier 2  本机         永远可用；混合档下本机识别一直在跑
```

**云端状态机**（ARCH-01：以「连续 3 次」为准，取代 draft §1.1 的「失败即降级」）：

| 当前 | 事件 | 下一状态 | 动作 |
|---|---|---|---|
| 可用 | 单段失败（5xx/超时/断网），连续失败 < 3 | 可用 | 该段保留本机版，不重发 |
| 可用 | 连续第 3 次失败 | 冷却(退避) | 降到 Tier 1/2；退避 5s→30s→2min→5min 封顶 |
| 任意 | 401/403 | 停用(key 无效) | 状态栏「云端 key 无效」；直到 key 被修改 |
| 任意 | 429 | 冷却(限流) | 冷却 `retry-after`（无则 60s） |
| 任意 | 本地记账任一窗口 ≥ 90% | 冷却(额度) | 降到 < 80% 恢复 |
| 冷却 | 到期 | 试探 | 下一个段边界发一次 |
| 试探 | 成功 | 可用 | 经 MigrationGate 在静音点切回 Tier 0 |
| 试探 | 失败 | 冷却 | 退避升一级 |
| 任意 | 开关关 / key 清除 | 关闭 | **立即取消在飞请求**，响应丢弃（S1） |
| 停用/关闭 | key 修改 / 开关开 | 可用 | — |

层间切换都在静音点：云端→节点要先建 WS 会话（冷启动 5–11s，TK-008 后 ready 前不丢音频），建好前的段由本机字幕兜底；节点→云端复用 001 `MigrationGate`。

### P6' 云端配置（key 与开关）

| | Mac rslite | Windows 桌面程序 |
|---|---|---|
| key 文件 | `~/.config/rslite/groq.key`（0600） | `%APPDATA%\realtime_subtitle\groq.key`（icacls 仅当前用户） |
| 写入 | `rslite --set-groq-key`（stdin 一行，校验形状，原子写）/ `--clear-groq-key` | ⚙️ 面板密码框 → 写上述文件；**不写 config_local.py** |
| 环境变量 | **不读** | **不读** |
| 开关 | `~/.config/rslite/cloud.json` `{"enabled": bool}`，有 key 时默认 true | 面板勾选，有 key 时默认开 |
| 标识 | 状态行「云端·Groq」/「混合·节点名」/「本机」 | 悬浮窗状态行同左 |

### 云端请求（两端一致）

- `POST https://api.groq.com/openai/v1/audio/transcriptions`，`model=whisper-large-v3`、`language=<源语言>`、`response_format=verbose_json`，WAV 16k mono s16le。
- 请求单位 = VAD 整段：静音 ≥0.6s 收尾、≤15s 强制切；短于 1.5s 且与下一段间隔 <1.5s 时合并。
- 段时间用**客户端样本时钟**（采集起累计样本 / 16000），结果按 `segments[].start/end + a0` 映回，切句后走 P5（003 版并集累计覆盖 ≥50% 替换）。
- 超时：连接 5s、总 10s。代理：云端走系统 / 环境代理；节点 WS 不走代理（001 C-1）。
- **记账**：本地事件日志 `(ts, billed_s = max(10, duration))`；窗口 1 分钟请求数、1 小时音频秒、24 小时请求数与音频秒（免费档 20/分、7200s/时、2000 次/天、28800s/天）；原子写持久化，超过 24h 的事件丢弃；每次响应用 `x-ratelimit-remaining-requests` 校正请求数。

### P9 跨语言一致性夹具

`tests/fixtures/route_cases.json`（输入事件序列 → 期望 tier / 云端状态，覆盖上面状态表每一行）与 `tests/fixtures/p5_cases.json`（本机句 + 远端句 → 期望可见行）。Swift `--selftest` 和 pytest **读同一份夹具**。改规则必须先改夹具。

### P10 Windows 客户端句子时间（ARCH-03）

`streaming_asr.process_iter` 目前只返回 `(committed_text, unstable_text)`（streaming_asr.py:431-433），词时间在这一层就丢了。改为把已提交词的 `(start, end, text)` 一路往下传：句子 `t0` = 首词 start，`t1` = 末词 end，经 `on_pair(src, tgt, t0, t1, key)` 交给 UI。时间基准 = 客户端样本时钟；在语言切换 `clear_context`、`.paused` 暂停、设备热切换三种情况下保持连续，用测试证明。

## 架构选择

- **Provider 抽象（ARCH-02）**：「云端是又一种节点」只在概念上成立。实现上定义 `Provider`（`tier`、`available()`、产出 P5 句子）。节点 Provider 包住现有 NodeClient + RoutingDecider，云端 Provider 单独实现；两者只共享分级选择器和 P5 输出。云端**不进** nodes.json，也不复用 NodeConfig。
- **分段器两端不同**：
  - Windows 复用 `realtime_subtitle/node/segmenter.py` 的 `Segmenter` + `SileroVad`。
  - rslite 没有 VAD，先用 TK-001 探针评估「词时间驱动分段」：以 SpeechTranscriber `audioTimeRange` 词边界判断，末词之后静音 ≥0.6s 收段；音频从环形缓冲截取 [a0−0.2, a1+0.2]。
  - 探针不达标就走 A3'：能量 VAD + final 结果校正。
- **Windows 客户端接节点**：在 `realtime_subtitle/remote/` 下新建 `node_client`（websockets，协议 v2，token 只读文件，URL 必须解析到 100.64.0.0/10，ready 前有界缓冲）、`router`（分级 + 层内打分 + MigrationGate + 不连自己，判据用 node_id）、`line_store`（移植 003 版 P5）、`cloud_client`、`quota`。`remote/` 是 001 v1 删过的目录名，复用前先确认没有残留。
- **翻译**：节点句用节点给的译文；云端句，以及节点没给译文的句子，用本机 Ollama（`ollama_url()`、统一 `num_ctx`、`_translate_timeout()`）。rslite 继续用 Apple 翻译。
- **下发**：`install_node*.sh --client <ssh 别名>` 把 nodes.json 条目和 token 合并写到 Windows 客户端 `%APPDATA%\realtime_subtitle\`（token 经 stdin 传，icacls 收紧权限）。

## 安全决策

1. **key 只来自程序配置文件**，不读环境变量；文件权限 0600 或 icacls 仅当前用户；不进仓库、日志、崩溃报告。
2. **关开关 = 立即取消在飞请求并丢弃响应**（S1）。已发出的字节无法撤回，只能保证关掉之后不再发起新连接。
3. **TLS**（S2）：两端禁止关闭证书校验（Python 不许 `verify=False`，Swift 不实现放行任何证书的 delegate），不加自定义 CA；用源码扫描测试守住。用户代理（Surge）是否解密 HTTPS 由用户自己的信任链决定，README 写明「经代理时代理可见上传内容」。
4. **日志**（S3 驳回后的补充）：云端客户端只记 `x_groq.id`、音频时长、状态码、切换原因、异常类型；异常信息和 traceback 里不能出现请求头。测试：注入假 key 后触发各类失败，断言日志和异常文本里都没有这个 key。已有的 `SHOW_PERFORMANCE` 正文日志不在本 RFC 范围内。
5. **节点地址**：Windows 节点 URL 解析结果必须在 100.64.0.0/10，否则拒连；token 只经文件或 stdin 传，不进命令行。
6. **云端返回文本**只当字幕显示，也会作为翻译输入；不能当控制信号（同 CLAUDE.md §36）。

## 已被驳回的备选项

| # | 方案 | 理由 |
|---|---|---|
| A1 | 云端经节点转发 | 没有节点的用户用不了；多一跳；节点要持有 key |
| A2 | Mac 也跑 Python 客户端 | rslite 的卖点就是不装 Python/Ollama、省电 |
| A4 | 云端并入 001 加权打分 | 用户给了明确顺序；Groq 的 rtf/rtt 与节点不可比 |
| A5 | 以本机已出句为请求单位 | 延迟 = 本机 final 延迟 + 上传，违反 ≤2s |
| A6 | 云端失败段重发给节点 | 节点冷启动 5–11s，重发会乱序；本机字幕已经兜底 |
| A7 | 读 `GROQ_API_KEY` | 用户定：避免别人机器上碰巧有 key 就开始上传 |
| S2' | 客户端禁止代理 MITM | 客户端检测不了，也不该管用户自己的信任链 |

## 影响面

- 修改路径：
  - Mac：`macos-native/Sources/rslite/{NodeRouter,HybridEngine,main,Overlay}.swift`
  - 新增 Mac：`Cloud{Client,Segmenter}.swift`、`QuotaLedger.swift`、`Provider.swift`
  - Python：`realtime_subtitle/{asr/streaming_asr,translate/translator_queue,ui/subtitle_window,ui/subtitle_render,ui/settings_window,app}.py`；新增 `realtime_subtitle/remote/*`
  - 脚本：`scripts/node/install_node*.sh`、`scripts/windows/node_setup.ps1`
  - 文档：README 三语、CLAUDE.md、`docs/zh/user-guide-template.txt`、`docs/protocol-v2.md`、`version.py`
- 新增依赖：Python `websockets` 提进 `requirements.txt`（需过 deps-resolve CI）；Swift 无。
- 兼容性：协议 v2 与节点服务端不改；nodes.json 格式不变。Windows 老用户在没填 key、节点开关默认关时，行为与 3.1.0 完全一样；rslite 没有 key 时路由结果与 3.1.0 相同。

## Done criteria

002（requirement §4）：
1. 有 key 时 rslite 默认走 Groq，界面显示「云端」。
2. FLEURS 60 条 WER ≤ 4.5%；67 秒回放 WER ≤ 001 混合档最终 WER（2.63%）。
3. 段尾 → 识别上屏中位数 ≤ 2s（不含翻译）。
4. 以下回退场景都要测，字幕不断、不重复、不丢句，日志写明原因：拔网、错 key、模拟 429、额度满、关开关、节点也离线。
5. 故障消失后在下一个静音点自动切回 Groq。
6. 连续 1 小时：不触发被动 429；请求数记账与响应头完全一致；音频秒偏差 ≤ 5%（或按 requirement 写明的退回核对方式）。
7. pytest / swift build / ruff 全绿；单测覆盖切段与计费下限、记账、回退顺序、开关、TLS / key 不泄露。
8. README 写明开关、隐私、key 配置方法。

005（requirement §4）：
9. Windows 混合档：WER ≤ 纯本机，无重复，首字不慢于本机（±0.2s）。
10. 停节点后字幕不断；节点恢复后 ≤ 60s 回到混合档。
11. 在 yzwdmsnlmm 本机打开开关时跳过自己（日志可见理由）。
12. 新装和老用户更新后节点开关都是关；关时没有对 8791 的连接；非 Tailscale 地址被拒。
13. `--client` 下发后客户端不用手工配置。
14. 全量 pytest（含 Windows）0 failed；单机冒烟正常。
15. CLAUDE.md、README、用户手册同步；`version.py` = 3.2.0。
