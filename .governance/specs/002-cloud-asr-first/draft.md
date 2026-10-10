# 002 + 005 合并技术方案：云端识别 + 自动切换 + Windows 客户端接节点（v3.2.0）

- 日期：2026-10-11 · 作者：Claude（Coordinator）· 状态：draft，待 `/sdd-review`
- 需求来源：`specs/002-cloud-asr-first/requirement.md`（含 2026-10-11 用户改定）+ `specs/005-windows-client-nodes/requirement.md`
- 基线：`feat/macos-native` 4351e55（003 / 004 已合并；Windows 节点 yzwdmsnlmm 在线）
- 001 协议 v2（P1–P8）不改；本方案新增 P6'（云端配置）、P7'（分级路由）、P9（跨语言一致性夹具），在 002/005 的 RFC 里冻结。

## 0. 一句话

**云端是又一种「节点」**：两个客户端（Mac rslite / Windows 桌面程序）各自实现同一套「本机先出字幕 → 远端精修按时间替换 → 远端不可用就降级」的骨架，远端有两类——**云端 Groq（无状态、按段 HTTP 上传）**和**自己的节点（有状态 WS v2 流）**；路由按固定层级 **云端 → 自己的节点 → 本机** 选，层内沿用 001 打分。

## 1. 技术方案

### 1.1 分级路由（P7'）

```
Tier 0  cloud:groq   条件：配置里有 key && 云端开关开 && 未冷却 && 额度 < 90%
Tier 1  自己的节点   条件：节点开关开（Windows 默认关；rslite 读 nodes.json 即开）&& 探测健康
                    层内按 001 RoutingDecider 打分（rtf/rtt/in_use/on_ac/busy），不连自己
Tier 2  本机         永远可用；混合档下本机识别本来就一直在跑
```

- **层级是硬顺序，不进打分**：用户指定的顺序可预测；云端与节点的「质量/速度」量纲也不可比（见备选项 A4）。
- **层间切换都在静音点**：云端是逐段请求，段边界天然是静音点；从节点切回云端复用 001 `MigrationGate`（drain 旧节点会话、下一段起走云端）；从云端降到节点要先建 WS 会话（冷启动可达 5–11s，TK-008 修好后 ready 前音频不丢），建好之前的段落由本机字幕兜底。
- **任何一段远端失败都不丢字**：混合档里本机字幕先上屏，远端结果只是替换；某段云端请求失败，这段就保留本机版，路由随即降级，后续音频走下一层。不重发失败段（避免 10 秒计费和重复）。

### 1.2 云端客户端（两端各一份，行为一致）

| 项 | 决定 |
|---|---|
| 接口 | `POST https://api.groq.com/openai/v1/audio/transcriptions`，`model=whisper-large-v3`、`language=<源语言>`、`response_format=verbose_json`、WAV 16k mono s16le |
| 请求单位 | **VAD 整段**：静音 ≥0.6s 收尾、单段 ≤15s 强制切（与节点 segmenter 同规则）；短于 1.5s 的段与下一段合并（间隔 <1.5s 时），否则照发（按 10s 计费，记账照记） |
| 段时间 | a0/a1 用**客户端样本时钟**（与发给节点的同一个时钟），结果按 Groq `segments[].start/end + a0` 映射回时间轴，切句后走 P5（003 版：并集累计覆盖 ≥50% 替换） |
| 超时 | 连接 5s、总 10s；超时算一次失败 |
| 代理 | 云端 HTTPS **走系统/环境代理**（Mac URLSession 默认、Python requests 读环境）；节点 WS（`*.ts.net` / 100.64/10）**不走代理**（沿用 001 C-1） |
| 日志 | 只记 `x_groq.id`、音频时长、状态码、切换原因；不记音频、key、识别正文 |

**分段器——两端不同实现**（关键风险，见 §4-1）：
- **Windows（Python）**：直接复用 `realtime_subtitle/node/segmenter.py` + `SileroVad`（只依赖 numpy；Silero 走 faster-whisper 自带的 onnx，Windows 客户端本来就装了），客户端在采集线程旁跑。
- **Mac（rslite，Swift）**：rslite 没有 VAD，也不能装 Python/onnx。方案：**词时间驱动分段**——用本机 SpeechTranscriber 结果里 `audioTimeRange` 的词边界判定语音活动：最后一个已识别词的结束时间之后静音 ≥0.6s（按样本时钟）即收段，≤15s 强制切；音频从客户端环形缓冲按 [a0−0.2s, a1+0.2s] 截取。TK-009 已发现 volatile 结果的时间是「段首→当前」的宽区间，所以**开工第一个任务是探针（TK-S1）**：量 volatile/final 两种结果的词边界精度与延迟；不达标就退到「能量 VAD + final 结果校正」方案（备选 A3'）。

### 1.3 额度与故障（两端同一份规则，P7' 的一部分）

- **记账**：本地事件日志 `(ts, billed_s = max(10, duration))`，滚动窗口：1 分钟请求数、1 小时音频秒、24 小时请求数与音频秒（免费档 20/分、7200s/时、2000 次/天、28800s/天）。任一达到 **90%** 即云端「额度冷却」，降到 80% 以下恢复。持久化到配置目录（原子写，重启不清零；超 24h 的事件丢弃）。每次响应用 `x-ratelimit-remaining-requests` 校正请求数（以服务端为准）。
- **故障分类**：
  - 401/403 → 云端停用直到 key 被修改（状态栏「云端 key 无效」），不重试；
  - 429 → 冷却 `retry-after`（无则 60s），并按 §4-2 探针结果修正窗口模型；
  - 5xx / 超时 / 断网 → 指数退避 5s → 30s → 2min → 5min 封顶，冷却结束后下一段即试探；
  - 连续 3 次失败才判「云端不可用」并降级（单次失败只影响那一段）。
- **恢复**：冷却结束、开关重开、key 修改后，下一个段边界自动回到 Tier 0（若当前在节点上，经 MigrationGate 在静音点切）。

### 1.4 key 与开关（P6'）

| | Mac rslite | Windows 桌面程序 |
|---|---|---|
| key 存放 | `~/.config/rslite/groq.key`（0600） | `%APPDATA%\realtime_subtitle\groq.key`（icacls 仅当前用户） |
| 怎么填 | `rslite --set-groq-key`（从 stdin 读一行，校验形状后原子写）；`--clear-groq-key` | ⚙️ 面板「云端识别」输入框（密码框）→ 保存到上述文件；不写进 `config_local.py` |
| 读环境变量 | **否**（用户定） | **否** |
| 开关 | `~/.config/rslite/cloud.json` `{"enabled": true}`，有 key 时默认 true；菜单/命令行可关 | ⚙️ 面板勾选框，有 key 时默认开 |
| 界面标识 | 状态行「云端·Groq」/「混合·节点名」/「本机」，云端时加明显标记 | 同左，悬浮窗状态行 |

关开关立即生效：在飞的那一段请求让它完成或丢弃（不等静音点），之后不再发任何云端请求。

### 1.5 Windows 客户端接节点（005）

1. **本机句带时间（前置）**：`streaming_asr` 的已提交词本来就有 `(start, end, text)` 且已加 `buffer_time_offset`，但 `translator_queue._append_committed(committed_text)` 只收文本，时间在这里丢了。改为把词时间随文本传下去，句子 `t0 = 首词 start`、`t1 = 末词 end`，经 `on_pair(src, tgt, t0, t1, key)` 交给 UI。
   - **时间基准必须是客户端样本时钟**（采集起累计样本数 / 16000），和发给节点/云端的音频同一把尺子。`buffer_time_offset` 在 `clear_context`（语言切换）、暂停、设备热切换时的行为要核实并对齐（§4-3）。
2. **Python 节点客户端**：`realtime_subtitle/remote/node_client.py`（websockets 客户端，协议 v2：hello / s16le 帧 / ready / final / translation / status / drain；token 只读文件；URL 解析结果必须在 100.64.0.0/10，否则拒连——与 `_assert_local_ollama` 同思路）。ready 前音频进有界缓冲（与 TK-008 的 rslite 修法一致）。
3. **Python 路由**：`realtime_subtitle/remote/router.py`，移植 rslite 的分级 + 层内打分 + MigrationGate + 不连自己（本机 Tailscale IP / hostname 与节点 URL 比对）。
4. **字幕历史替换**：`subtitle_window` 的句对历史加 `t0/t1/source/key`，替换逻辑移植 LineStore（003 版并集累计覆盖），只替换内存历史，不回滚已滚出屏幕的画面（同 rslite P5）。
5. **翻译**：节点句用节点给的译文；云端句和节点没给译文的句子用本机 Ollama（沿用 `ollama_url()` 钉地址、`num_ctx` 统一、`_translate_timeout()`）。
6. **面板**：⚙️ 加「使用算力节点」（默认关；没有 nodes.json 时置灰并说明）、「云端识别」key 框 + 开关、当前路由状态。模式切换走 `_apply_mode` 单入口约束不变。
7. **下发**：`install_node.sh` / `install_node_windows.sh` 加 `--client <ssh 别名>`：把本节点的 nodes.json 条目 + token 合并写到该 Windows 客户端的 `%APPDATA%\realtime_subtitle\`（token 经 stdin、icacls 收紧，复用 node_setup.ps1 的 Lock-Down）。

### 1.6 跨语言一致性（P9）

两端是 Swift + Python 两份实现，最容易走散的是**路由决策**和**P5 替换**。新增 `tests/fixtures/route_cases.json`、`tests/fixtures/p5_cases.json`（输入事件序列 → 期望结果），Swift `--selftest` 和 pytest **读同一份夹具**跑。改规则必须先改夹具。

## 2. 备选项

| # | 方案 | 结论 | 理由 |
|---|---|---|---|
| A1 | 云端经自己的节点转发（节点调 Groq） | 否决 | 没有节点的用户用不了云端；多一跳；节点要持有 key |
| A2 | Mac 上也跑 Python 客户端/本机节点，两端共用一份 Python 实现 | 否决 | rslite 的卖点是不装 Python/Ollama、省电；MacBook 要装数 GB venv |
| A3 | rslite 用纯能量 VAD 分段 | 暂否（列为 A3' 兜底） | 直播常有背景音乐，能量阈值误判多；若 TK-S1 证明词时间分段不可用，再上「能量 VAD + final 校正」 |
| A4 | 把云端并入 001 的加权打分，不分层 | 否决 | 用户给了明确顺序；Groq 的 rtf/rtt 与节点不可比，权重难调且不可预测 |
| A5 | 云端只精修「本机已出的句子」（以本机句为请求单位） | 否决（保留为备用） | 实现最简单、替换最准，但延迟 = 本机 final 延迟（rslite 2.2–4.4s）+ 上传，违反 002「段尾到结果 ≤2s」 |
| A6 | 云端失败段重发给节点 | 否决 | 节点会话冷启动 5–11s，重发会乱序；本机字幕已兜底，代价不值 |
| A7 | key 读环境变量 `GROQ_API_KEY` | 否决 | 用户定：避免别人机器上碰巧有 key 就上传 |

## 3. 影响面

**修改**
- Mac：`macos-native/Sources/rslite/NodeRouter.swift`（分级）、`HybridEngine.swift`（云端结果接入 P5、状态标识）、`main.swift`（`--set-groq-key` 等参数）、`Overlay*`（状态行）。
- 新增 Mac：`CloudClient.swift`、`CloudSegmenter.swift`（环形缓冲 + 词时间分段）、`QuotaLedger.swift`。
- Windows/Python：`realtime_subtitle/asr/streaming_asr.py`、`translate/translator_queue.py`（词时间下传）、`ui/subtitle_window.py` + `subtitle_render.py`（历史带时间与替换）、`ui/settings_window.py`（面板）、`app.py`（接线）。
- 新增 Python：`realtime_subtitle/remote/{node_client,cloud_client,router,line_store,quota}.py`（`remote/` 目录 001 v1 删过，名字复用前查无残留）。
- 脚本：`scripts/node/install_node.sh`、`install_node_windows.sh`、`windows/node_setup.ps1`（`--client` 下发）。
- 文档：README 三语（隐私承诺改写 + 云端/节点使用）、CLAUDE.md 开头承诺与第 4/7 节、`docs/zh/user-guide-template.txt`、`docs/protocol-v2.md`（客户端侧补充，不改协议）、`version.py` 3.2.0。

**新增依赖**
- Python：`websockets`（已在 node/requirements.txt，需提进 `requirements.txt`；纯 Python，3.10–3.13 都有轮子，过 deps-resolve CI）。HTTP 用现有 `requests`。
- Swift：无（URLSession）。

**兼容性**
- 协议 v2 / 节点服务端不改；`nodes.json` 格式不变（云端不进 nodes.json）。
- Windows 老用户：两个开关默认都不生效（没 key、节点默认关）→ 行为与 3.1.0 完全一样。
- rslite 老用户：没 key 时只多一个「云端不可用」的静默判断，路由结果同 3.1.0。
- 隐私承诺文本改变（需求明确要求），三语同步。

## 4. 未解决问题

1. **rslite 词时间分段可行性**（最大风险）：SpeechTranscriber volatile 结果的 `audioTimeRange` 很粗（TK-009），final 又晚 2–4s。TK-S1 探针量化后决定走词时间分段还是 A3'。
2. **Groq 429 细节**：是否带 `retry-after`、音频秒数窗口是滚动还是整点——用一次刻意超分钟限额的小测拿真实 429 样本（requirement 已列）。
3. **Windows 客户端样本时钟**：`buffer_time_offset` 在语言切换 `clear_context`、`.paused` 暂停、设备热切换时是否连续；不连续时 t0/t1 与发给节点的时钟会错位。
4. **弱机器上的翻译速度**：没有 N 卡的 Windows 本机 Ollama 走 CPU，云端句的翻译可能比识别慢得多；是否需要「节点可用时云端句也交节点翻译」——不在需求里，先测再议。
5. **不连自己的判据**：Windows 上用 Tailscale IP 比对还是 node_id（本机若装了节点，可读 `%LOCALAPPDATA%\rs-node\node_id`）。倾向 node_id，更稳。
6. **rslite 填 key 的体验**：只有命令行是否够（rslite 目前没有设置界面）；需求里 Mac 端没要求 GUI 输入。
7. **README 承诺改写的措辞**：要同时覆盖「节点＝自己的机器」和「云端＝第三方 Groq」两种出本机路径，三语一致。
8. **Groq 数据保留政策**：README 是否要写明 Groq 对上传音频的保留/训练政策（需查官方条款，不凭记忆写）。

## 5. 任务草拆（RFC 再定）

| TK | 内容 | 依赖 | 建议 lane |
|---|---|---|---|
| S1 | 探针：rslite 词时间分段精度/延迟 + Groq 429 样本 | — | Claude/codex |
| 1 | P9 夹具（route_cases / p5_cases）+ Swift/Python 双端读夹具 | — | codex |
| 2 | rslite CloudClient + QuotaLedger + key/开关 | S1 | codex |
| 3 | rslite CloudSegmenter + 分级路由接入 HybridEngine | S1, 1, 2 | codex（high） |
| 4 | Python 本机句带时间（streaming_asr → translator_queue → UI） | — | codex |
| 5 | Python node_client + router + line_store（含 ready 前缓冲） | 1, 4 | codex |
| 6 | Python cloud_client + quota + segmenter 复用 | 1, 4 | 免费 lane + 协调者审 |
| 7 | Windows 面板 + 状态显示 + `--client` 下发 | 5, 6 | 免费 lane + 协调者审（安全部分自留） |
| 8 | 文档（README 三语、CLAUDE.md、用户手册）+ version 3.2.0 | 2–7 | inkling + 协调者审 |
| 9 | 端到端验收：002 Done 1–8 + 005 Done 1–7（Mac + Windows 真机） | 全部 | 协调者 |
