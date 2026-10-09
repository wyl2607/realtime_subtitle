# 002 起草前预备资料（给 /sdd-draft 用）

- 日期：2026-10-09（as-of `feat/macos-native` e9646d6；TK-005b 未合并，下面的代码位置合并后要复核）
- 用途：把起草时要查的事实先收齐。**这里只记事实和待定问题，不做方案决定**——决定在 draft.md 里做。

## 1. 开工前提（都满足才 /sdd-draft）

- [ ] Sprint 001 TK-005b 合并进 `feat/macos-native`（它在改 `NodeRouter.swift` / 新增 `UDSWebSocket.swift`，002 也要动 NodeRouter，先合再起草免得基于旧代码）
- [ ] Sprint 001 TK-007 验收出数：混合档 67 秒回放的最终文本 WER → 回填 requirement Done criteria 第 2 条
- [ ] requirement.md 已提交（本文件同批提交）

## 2. Groq 接口实测（2026-10-09，MacBook Air 经 Surge 代理，区域 `x-groq-region: fra`）

- 端点：`POST https://api.groq.com/openai/v1/audio/transcriptions`，multipart：`model=whisper-large-v3`、`file`、`language=de`、`response_format=verbose_json`。
- 响应体：`duration`（真实音频秒数，如 2.82，**不是**按 10 秒最低计费后的数）、`segments[]`（含 `start/end/avg_logprob/no_speech_prob/compression_ratio`）、`x_groq.id`（请求 ID，可写日志，不含正文）。
- 响应头只有请求次数配额：`x-ratelimit-limit-requests: 2000`、`x-ratelimit-remaining-requests`、`x-ratelimit-reset-requests`（实测是 `23m45.6s` 这种 → **滚动窗口逐步恢复，不是零点重置**）。
- **没有音频秒数配额头** → 音频秒数只能本地记账（`max(10, duration)`）；1 小时额度验收要以请求次数头为硬核对、控制台为软核对（已写进 requirement）。
- 待 draft 核实：429 响应带不带 `retry-after`、音频秒数的恢复窗口；可用一次刻意超分钟限额（20 次/分钟）的小测拿到真实 429 样本。
- 已有对比数据与脚本：`~/projects/rs-mac-native-data/cloud_asr.py`、`cloud_groq-whisper-large-v3.jsonl`。

## 3. 代码落点（001 现状）

| 关注点 | 位置 | 与 002 的关系 |
|---|---|---|
| 节点清单读写 | `macos-native/Sources/rslite/NodeRouter.swift:176-182`（`RSLITE_CONFIG_DIR`，默认 `~/.config/rslite/nodes.json`） | Groq 作为新节点类型进 P6；要决定是写进 nodes.json 还是内置 |
| 路由打分 / 迁移门 | `NodeRouter.swift`：`RouteWeights` / `RoutingDecider` / `MigrationGate` / `NodeRouter` | 回退顺序 Groq → mini2 → Windows → 本机 B 要落在这里（P7 新版本） |
| 远程会话客户端 | `NodeClient.swift`（WebSocket v2，`URLSessionConfiguration.ephemeral`，第 250 行） | Groq 是 HTTP 整段上传，不是 WS 流——需要一个和 NodeClient 平级的客户端，还是包一层适配，draft 定 |
| 混合档 / 句子替换 | `HybridEngine.swift`（`SubtitleEngine` / `SubtitleMode` / `RSLiteMode`）、`SentenceCommitter.swift` | P4/P5 带时间句子复用；Groq 段结果要映射回同一套时间轴 |
| VAD 切段 | 节点 worker 侧有（Python，TK-001）；**rslite 本机侧目前没有轻量 VAD** | 002 要在 MacBook 上做 VAD 整段切分（不能加载 Whisper），这是新增模块，draft 重点 |
| 单实例 | `SingleInstance.swift` | 不变 |

001 协议约定在 `.governance/sprints-001-remote-offload/RFC.md`：P4 第 73 行、P5 第 82 行、P6 第 89 行、P7 第 97 行、P8 第 121 行。002 RFC 写 P6'/P7' 新版本，不改原文。

## 4. 待 draft 决定的问题清单

1. **key 读取**：rslite 从 GUI/launchd 起时不继承 `~/.zshenv`。候选：直接解析 `~/.config/groq/env`（`GROQ_API_KEY=...` 一行）、或 Keychain。没 key → 节点视为不可用、静默。
2. **代理**：`URLSessionConfiguration` 默认走系统代理。Groq 要走 Surge（需要）；而 001 遗留问题是 `ws://*.ts.net` 到 mini2 **不该**走代理（TK-007 在确认）。两条路径的代理策略要分别写清。
3. **切段策略**：10 秒最低计费 vs 2 秒出字延迟目标的平衡——短句攒批（合并相邻短段到 ≥10 秒会拉高延迟）还是接受短段多计费；每分钟 20 次上限下，平均段长不能低于 3 秒。
4. **额度记账持久化**：落盘位置（`~/.config/rslite/` 下）、滚动窗口如何建模（分钟 / 小时 / 天三层）、进程重启后不清零。
5. **云端开关与界面标识**：开关存哪、Overlay 上「云端」标识怎么显示、关掉时立即断开 Groq 还是等静音点。
6. **故障判定**：429/5xx/超时/断网/key 错（401）各自的退避时长与「恢复后切回」的探测方式（复用 MigrationGate 的静音点切换）。
7. **日志**：只记请求 ID、时长、状态码、切换原因；不记音频、不记 key、不记识别正文（与 001 安全决策一致）。

## 5. 文档改动清单（Done criteria 第 8 条 + 隐私约束）

- `README.md:5`「识别与翻译均在本机完成，不向任何云端发送音频或文本」
- `README.md:95` macOS 段「音频与文本都在本机处理，不上传」
- `CLAUDE.md:9-11`「完全本地运行……不向任何云端发送音频或文本」
- `CLAUDE.md` 第 7 节第 14 条（节点隐私边界）——补一句 rslite 云端节点例外
- `macos-native/README.md`（rslite 自己的说明）——云端开关、key 配置
- 改写口径：**只有 rslite 默认上传给 Groq，A 版（Python 桌面字幕）不变**，以及怎么关。
