# RFC — sprints-001-remote-offload

> 本文件由 `/sdd-rfc` 收敛 `.governance/specs/001-remote-offload-productization/` 下的 draft.md（147e561）和 draft.review.md（3486759，13 条 finding 全部接受）得出。
> Sprint 一启动本文件即冻结，之后的变更另开 RFC。

## 背景与动机

已经验证过的三条路线：

| 路线 | 实测 |
|---|---|
| B：本机系统识别 + 系统翻译 | WER 7.9%，延迟 2.2–4.5s |
| C：mini2 远程 Whisper 流式识别 | WER 2.6%，延迟 5.6–12.3s，换成系统翻译后为 7–11s |

两条路线在 MacBook Air 上的整机功耗都只比空闲多不到 0.1W。

用户要把它做成日常可用的形态：
- 一键安装服务端，并自动配置。
- 客户端自动选档：本机准确、混合（小模型本机 + 大模型远程）、本机省电。
- 服务端按需加载，不用时释放。
- 服务器那台机器自己当主力机时，不要远程调用自己。
- **多节点自动路由**：mini2 一直在线作保底；Windows 主机开机后通常更强，应该在说话的停顿处自动迁过去。
- 补齐功耗对比。

这一期只实现和验收 Mac；Windows 节点和 Windows 客户端放到下一期，但协议和路由要能接纳它们。

## 协议 / 接口决策

### P1. 节点信息 `GET /v1/info`

- 远程访问需要 `Authorization: Bearer <节点 token>`；走本机 UDS 不需要 token。
- 返回：

```json
{"v":2,"node_id":"<安装时随机生成>","hw_hash":"<sha256(IOPlatformUUID)前16位>",
 "asr":{"model":"whisper-large-v3-turbo","backend":"mlx","rtf":0.08},
 "translator":"apple|ollama:<model>","busy":false,"on_ac":true,"in_use":false,
 "worker":"cold|warm"}
```

- `in_use` 的含义是 `HIDIdleTime < 60s`，只给布尔值，不返回原始空闲秒数（S8）。
- `rtf` 在安装自检时测一次，之后每次会话结束都滑动更新。

### P2. 会话 `WS /v2/session`（取代 v1；v1 一并删除）

**客户端发给节点：**
- `hello`：`{"type":"hello","v":2,"src","dst","sample_rate":16000,"format":"s16le"}`
  - 握手完成后 5s 内必须收到，否则断开。
  - `sample_rate` 只接受 16000，`format` 只接受 s16le，其他一律拒绝（S4）。
- 音频：二进制帧，每帧约 100ms，单帧 ≤64KB，即 `max_size`（S4）。
- `{"type":"flush"}`：把正在进行的段立刻收尾。
- `{"type":"drain"}`：把现有的段全部收尾，发完对应结果后回 `drained`。迁移时使用。

**节点发给客户端：**
- `ready`：`{"ev":"ready","cold":bool,"load_s":float,"engine":{...}}`
- `final`：`{"ev":"final","id":int,"a0":float,"a1":float,"text":str}`
  - `a0`/`a1` 是从 hello 起算的音频秒数，按收到的样本数计算。
- `translation`：`{"ev":"translation","id":int,"text":str}`
- `status`：`{"ev":"status","code":str,"text":str}`
- `drained`：`{"ev":"drained"}`

**会话规则：**
- 节点**不发** volatile。服务端流式模式留作 v2 的扩展 `mode:"stream"`，下一期给 Windows 客户端用。
- 同时只服务一个会话，其他连接以 close `1013`（busy）关闭。
- 单个会话最长 4h；websockets 每 10s ping 一次，10s 无响应即判为断开。

### P3. gateway 与 worker 之间（stdin/stdout 管道，仅本机进程间）

- 上行：一行一个 JSON 控制消息（`hello` / `flush` / `drain`），或者长度前缀帧：4 字节大端长度，后面跟 PCM。
- 下行：一行一个 JSON 事件，格式与 P2 相同。
- worker 退出码：0 表示正常；非 0 时 gateway 记录错误码，并给客户端发 `status{code:"worker_crashed"}`。

### P4. 客户端带时间的句子契约（F01）

- `PipelineCallbacks.onFinal(id, text, t0, t1)`：`t0`/`t1` 是客户端统一样本时钟上的秒数。
  - 时钟由 AudioFanout 按送进 B 的样本数推进。
  - 时间取自 B 侧的 `attributeOptions:[.audioTimeRange]`。
  - 拿不到时间时值为 nil，这样的句子不参与替换。
- 节点下发的 `a0`/`a1` 要加上 `off` 换算到客户端时钟。`off` 是会话开始那一刻的客户端时钟值。
- Overlay 中每一行是 `{id, t0, t1, source: local|node(node_id), src_text, dst_text}`。

### P5. 替换规则

- 节点的 final 到达时，找出所有 `source=local` 且时间区间与它重叠 ≥ 各自时长 50% 的行，把这些行整体换成节点给的那一行。
- `source=node` 的行不会再被替换。
- 找不到可替换的行时，按 `t0` 的时间顺序插入。
- 已经滚出屏幕的行，只更新内存里的历史，不回滚画面。

### P6. 节点清单与运行状态（F03）

- `~/.config/rslite/nodes.json`（0600）：`[{"id","node_id","url":"ws://<MagicDNS名>:8791","token_file"}]`
  - 由安装脚本写入，按 `id` 去重。
- `~/.config/rslite/node-state.json`（0600，纯缓存，删掉也能自动重建）：每个节点最近一次的 info、rtf、`offline_until`。
- 写入一律先写临时文件再 `rename`，保证原子性。
- 本机节点不写进清单，靠本机 UDS 是否存在来发现。

### P7. 路由打分与迁移（F02）

**打分：**
- `score = quality + speed − penalties`
  - `quality = asr_rank + translator_rank`
  - `speed = −k·rtf`
  - 扣分项：`in_use`、`!on_ac`、RTT > 100ms、本机笔记本未达 P8 门槛
  - `busy` 或离线的节点直接排除。
- 权重集中在一张常量表里。每次做选择都打一行日志，写明各项得分，但**不写正文**。

**迁移到更优节点：**
- 触发条件：新节点比当前节点高出 margin，并且连续 2 次探测都如此（每 30s 探测一次）。
- 等到下一个静音点再动手：B 的 volatile 清空，或者本地 VAD 判断已静音 ≥0.6s。
- 先给旧会话发 `drain`，同时打开新会话，从静音点开始送音频。
- 等旧会话回 `drained`，最多等 3s，然后关闭旧会话。

**当前节点故障**：会话断开、ping 超时或收到 1013 时：
- 立即把该节点标为 `offline_until = now + 60s`。
- 按分数接上次优节点；所有节点都不可用时只用本机 B。
- 故障期间缺精修的句子，保留 B 的版本。
- 字幕始终不断、不重复。

**连接前校验**：建立会话前先核对 `/v1/info` 返回的 `node_id` 与清单是否一致，不一致就拒绝，并且不发送 token 和音频（S5）。

### P8. 本机准确档门槛

- 条件：`(芯片名含 Pro/Max/Ultra 或 hw.memsize ≥ 24GB) 且 插着电源`，或者是没有电池的台式机。
- 满足门槛时，本机节点按普通节点参与打分，另外加一项「无网络」加分。
- MacBook Air M2 16GB 不满足；mini2（M4 台式机）满足。

## 架构选择

1. **节点 = 轻网关 + 按需 worker 子进程**
   - gateway 由 launchd LaunchAgent 守护（KeepAlive），不 import numpy/mlx，常驻内存 <50MB。
   - 有会话时才 spawn worker；会话结束后 worker 保温 120s，然后 SIGTERM，3s 后 SIGKILL。进程一退出，模型就随之释放。
   - 用 LaunchAgent 是因为 Metal 和系统翻译都需要用户登录会话。节点因此依赖「用户已登录」；mini2 实测没开自动登录，是否开启由用户决定。
2. **服务端引擎 = VAD 分段整段识别**
   - Silero VAD（复用 `faster_whisper.vad`）切段：静音 ≥0.6s 视为段落结束，单段最长 15s，超过就在能量最低处强制切开。
   - 每段只调用一次 `AsrEngine`。本期用 MLX turbo，复用 `create_whisper_model` 和单线程执行器的修复；接口可插拔，Windows 版换成 faster-whisper。
   - 段内按 Whisper 的分句和词时间戳再细分成句子，每句带 `a0`/`a1`。
   - 复用 `text_rules` 的德语断句和幻觉黑名单。
   - **不复用** `WhisperQueueTranslator`，A 版因此完全不受影响。
3. **`Translator` 可插拔**
   - 默认用系统翻译（`AppleTranslator`/rstranslate）。
   - 本机语言包状态不是 installed 时，退回 Ollama。
4. **客户端 = 本机 B 常驻 + NodeClient 精修**
   - AudioFanout 用同一个样本时钟喂两路。
   - Overlay 按 P5 替换；本机界面显示「本机 / 混合·<节点> / 精修中」。
5. **单实例保护**：用 `~/Library/Application Support/rslite/lock` 加 `flock`，第二个实例启动时提示并退出。
6. **安装（F04）**
   - preflight：检查磁盘、uv、swift、Tailscale，并对 ssh 主机名做白名单校验。
   - 先部署到 `~/rs-node.new`，自检通过后原子换名为 `~/rs-node`，旧版本保留为 `~/rs-node.prev`。
   - launchd 启动或自检失败时，回滚到 `.prev` 并告警。
   - 旧的 `~/rs-remote` 只停掉进程，不删目录。
   - 脚本可以重复执行（幂等）；支持 `--local` 安装到本机。

## 安全决策

- **S1**：节点里的 Ollama 适配器在构造时必须先调用 `_assert_local_ollama()`，地址一律经 `ollama_url()` 获取，并补对应测试。
- **S2**：
  - plist 里不写死 IP。gateway 每次启动用 `tailscale ip -4` 查地址，再经 `validate_host` 校验。
  - bind 失败时指数退避重试，最长间隔 60s，并告警。
  - **永远不绑通配地址**。
- **S3**：UDS 只靠文件权限不够，还要：
  - 所在目录 `~/Library/Application Support/rs-node/` 权限 0700，属主必须是本人。
  - 启动时先 `lstat`，拒绝 symlink，再 unlink 残留的旧 socket。
  - 每次 accept 后用 `getpeereid` 校验对方 uid 等于本人。
- **S4**：所有资源上限都在 P2 里写死，另外音频入队有上限，超出时丢最旧的帧并告警。
- **S5**：连接前先核对 `node_id`；每个节点一份独立 token。token 只经 stdin 传递，权限 0600，不出现在命令行参数或日志里。
- **S6**：安装脚本的主机名必须匹配白名单 `^[A-Za-z0-9._@-]+$`，并在主机名前加 `--` 传给 ssh。
- **S7**：gateway、worker、LaunchAgent 的日志只记事件类型、时长、字节数和错误码，**不写转录或译文正文**，由测试断言保证。
- **S8**：`/v1/info` 只返回 `hw_hash` 和 `in_use`。
- **沿用的边界**：
  - 节点只监听 Tailscale IP 和本机 UDS。
  - 不把音频落盘，转录存档强制关闭。
  - 客户端界面要让用户看得出音频正在离开本机。

## 已被驳回的备选项

| 备选 | 驳回理由 |
|---|---|
| 服务端沿用 `WhisperQueueTranslator` 流式识别（现在的 C） | 时间戳在翻译层丢失，要拿到就得改 1900 行的核心文件；local agreement 重复识别浪费算力；实测延迟 5.6–12.3s |
| launchd socket activation，按连接拉起服务 | Python 要用 ctypes 调 `launch_activate_socket`；每次连接都冷启动，没有保温窗口；调试困难 |
| 常驻完整服务（现在的做法） | 违反「不用时不占资源」：模型会常驻 2GB 以上 |
| 用 mDNS/Bonjour 发现节点 | 不能可靠穿过 Tailscale，还会在局域网里暴露节点 |
| 扫描 `tailscale status` 里所有设备 | 会探测无关设备，而且每台都要有 token |
| 用文本模糊匹配做混合对齐 | B 和 Whisper 的断句、用词都不同，匹配不稳定 |
| 本机准确档直接嵌 A 版 Python 应用 | A 版就是导致过热的那个；它也是整套界面应用，没法当引擎嵌入 |
| 本期就上 WhisperKit（CoreML/ANE） | 要引入新的第三方依赖，还得单独跑基准，推迟到下一期评估 |

## 影响面

- **新增**
  - `realtime_subtitle/node/`：`gateway.py`、`worker.py`、`segmenter.py`、`engines.py`、`info.py`、`requirements.txt`
  - `scripts/node/`：`install_node.sh`、`com.realtimesubtitle.node.plist.template`
  - `macos-native/Sources/rslite/`：`AudioFanout.swift`、`NodeClient.swift`、`NodeRouter.swift`、`HybridEngine.swift`、`Capability.swift`、`SingleInstance.swift`
  - 测试：`tests/test_node_*.py`
- **修改**
  - `macos-native/Sources/rslite/`：`Pipeline.swift`（P4）、`Overlay.swift`（P5，以及界面上的模式显示）、`main.swift`（新增 `--mode auto|local|hybrid`，去掉 `--remote`）
  - `scripts/bench/power_compare.sh`：扩成五组对比（F05）
  - 文档：`CLAUDE.md` 第 7 节、`macos-native/README.md`、`docs/protocol-v2.md`
- **删除**
  - `realtime_subtitle/remote/`（含 `server.py`）、`tests/test_remote_server.py`、`macos-native/Sources/rslite/RemotePipeline.swift`
- **新增依赖**
  - 节点端只需要 `websockets`，VAD 复用 `faster_whisper`，两者都不写进根目录的 `requirements.txt`。
  - 客户端不新增任何依赖。
- **兼容性**
  - A 版（Windows 和 Mac）不受影响，`translator_queue.py` 一行都不改。
  - 协议 v1 废弃，它原本只有本仓库自己在用。
  - mini2 上的 `~/rs-remote` 只停用、不删除。

## Done criteria

与 requirement §4 对齐，并在 PLAN 的 checklist 里落实：

1. **一键安装**：在 MacBook Air 上执行一次 `scripts/node/install_node.sh mini2`，mini2 就开机自启节点，两端各有一份权限 0600 的 token，客户端写好 `nodes.json`。
   - 第二次执行是幂等的。
   - `--local` 也能用。
   - 人为制造一次自检失败，能够回滚。
2. **按需加载**
   - 没有会话时，mini2 上不存在 worker 进程，gateway 常驻内存 <50MB。
   - 会话结束 120s 后 worker 退出。
   - 客户端被 kill 或断网后，30s 内判定会话断开，之后照样回收 worker。
   - 冷启动后首条精修 ≤15s。
3. **自动选档**
   - MacBook Air（不满足门槛）连得上 mini2 时进混合档；mini2 的 gateway 被停掉时，自动退回本机 B，字幕不断。
   - mini2 本机运行 rslite 时走 UDS，不需要 token，也不经过网络。
4. **混合档**（用 67s 的 concat5 回放）
   - 首次出字时间不比 B 慢。
   - 「最终文本」的 WER ≤ C + 1 个百分点。最终文本的定义：被精修覆盖的部分取节点版本，没覆盖到的保留 B 版本。
   - 每段说完后，精修结果到达的中位数 ≤4s。
   - 不重复、不丢句。
5. **多节点路由**：用 mini2 加一个本机模拟的第二节点验收。
   - 更优节点上线后，在静音点完成迁移；下线后退回原节点。
   - 字幕不断、不重复。
   - 选择理由能在日志里看到。
6. **功耗**：`power_compare.sh` 测出空闲、B、C、A、混合五组数据，写进 `docs/`。
7. **质量门**
   - pytest 全量通过，ruff 通过，Swift release 编译通过。
   - P2 的各项上限、S3、S7 都有测试覆盖。
   - 每个 TK 至少评审 2 轮；涉及鉴权、生命周期、状态机的 TK 准备第 3 轮。
8. **文档**：`CLAUDE.md` 第 7 节新增避坑条目；README 说明用法；`docs/protocol-v2.md` 写清与 v1 的差异。
