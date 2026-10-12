# 节点协议 v2（外包算力节点）

本文是 RFC `sprints-001-remote-offload` 中 P1–P3 的落地说明。**以代码为准**：
每个字段和码值后面标了出处（`gateway.py:行号` 指 `realtime_subtitle/node/gateway.py`，
`worker.py`、`info.py` 同理）。代码改了，行号可能漂移，以符号名为准。

节点由两个进程组成：

- **gateway**：常驻，只做监听、鉴权、卡资源上限、按需拉起/回收 worker；不 import numpy/mlx（`gateway.py:5-8`）。
- **worker**：子进程，做识别（MLX Whisper）和翻译；无会话时不存在（`gateway.py:10-12`）。

```
客户端 ──WS/HTTP──▶ gateway ──stdin/stdout 管道──▶ worker
        (P1, P2)              (P3，仅本机进程间)
```

## P1 节点信息 `GET /v1/info`

- 路径常量 `INFO_PATH = "/v1/info"`（`gateway.py:54`）。注意路径前缀是 `/v1`，但响应里 `v` 字段是 2。
- 远程（TCP）必须带 `Authorization: Bearer <token>`；本机 UDS 不需要 token，改为校验对端 uid 等于 gateway 的 uid（`gateway.py:825-854`）。
- 响应由 `NodeInfo.collect` 生成（`info.py:143-156`），`Content-Type: application/json`（`gateway.py:866`）：

| 字段 | 类型 | 含义 |
|---|---|---|
| `v` | int | 恒为 2 |
| `node_id` | str | 安装时随机生成，读自 `rs-node/node_id`；读不到返回空串，客户端必然对不上清单，等于拒绝（`info.py:115-121`） |
| `hw_hash` | str | `sha256(IOPlatformUUID)` 的前 16 位（`HW_HASH_LEN = 16`，`info.py:30`）；探测失败为空串 |
| `asr` | object | `{model, backend, rtf}`；默认 `whisper-large-v3-turbo` / `mlx` / `null`（`info.py:39`），之后读 `asr_state.json` |
| `translator` | str | `apple` 或 `ollama:<model>`（正则 `info.py:42`）；默认 `apple` |
| `translator_ok` | bool | 翻译器当前是否可用。`ollama:<model>` 时 gateway 探测本机 `127.0.0.1:11434/api/version`（0.5s 超时、10s 缓存、不走系统代理），200 且 JSON 含 `version` 才为 `true`；其它翻译器恒为 `true`。只暴露布尔值，不暴露 URL 或错误文本 |
| `mem_pressure` | string \| null | 机器内存状态 `normal` / `warn` / `critical`。macOS 取 `sysctl kern.memorystatus_vm_pressure_level`（1/2/4），Windows 取 `GlobalMemoryStatusEx` 的内存负载（≤80 / ≤92 / 更高）。探测失败为 `null`；10s 缓存。客户端打分：warn +8、critical +25 罚分，`null` 不罚 |
| `swap_used_gb` | number \| null | 已用交换空间（GB，一位小数）。macOS 取 `sysctl -n vm.swapusage` 的 used，Windows 估算为已提交量超出物理已用的部分。>8 罚 6、>20 罚 15（2026-10-12：节点机上某程序内存泄漏导致交换 35GB、翻译慢一倍，客户端此前无从得知） |
| `busy` | bool | 当前会话数是否达到容量（`sessions >= capacity`，旧客户端兼容；`gateway.py:1165`） |
| `sessions` | int | 当前正在进行的会话数（`gateway.py:1163`） |
| `capacity` | int | 节点配置的会话容量，缺省 2，范围 1–4（`gateway.py:85-87, 1164`） |
| `on_ac` | bool | `pmset -g batt` 首行含 `AC Power` 为真；探测不出按 `False`（`info.py:71-78, 97-102`） |
| `in_use` | bool | `HIDIdleTime < 60s`（`IN_USE_IDLE_THRESHOLD_S = 60.0`，`info.py:29`）；探测不出按 `True`。**只给布尔值，不给原始空闲秒数** |
| `worker` | str | `cold`（无存活 worker）或 `warm`（`gateway.py:531-532`） |

「探测不出来」一律往对路由保守的方向取（`in_use=True`、`on_ac=False`）。`rtf` 由 worker 在会话结束时滑动更新：
`new = 0.7*old + 0.3*session`，会话被识别音频不足 5 秒不更新（`worker.py:77-84, 334-336, 359-360`）。

其它路径返回 404；TCP 无/错 token 返回 401 并带 `WWW-Authenticate: Bearer`；UDS 对端 uid 不符或取不到返回 403（`gateway.py:835-868`）。

## P2 会话 `WS /v2/session`

路径 `SESSION_PATH = "/v2/session"`（`gateway.py:55`）。监听两处：Tailscale IPv4 的 `8791`（`PORT`，`gateway.py:52`）和本机 UDS `rs-node/gw.sock`（`gateway.py:53`）。

### 客户端发给节点

| 消息 | 形状 | 规则 |
|---|---|---|
| `hello` | `{"type":"hello","v":2,"src","dst","sample_rate":16000,"format":"s16le"}` | 文本帧，必须是连接后的第一条；5s 内未到则断开（`HELLO_TIMEOUT_S`，`gateway.py:59, 892-898`）。`v` 必须是 2；`sample_rate` 只认 16000、`format` 只认 `s16le`；`src`/`dst` 须 fullmatch `^[a-z]{2,3}(-[A-Za-z]{2,4})?$`（`gateway.py:94, 913-925`） |
| 音频 | 二进制帧，s16le 小端 PCM | 字节数必须是偶数；单帧 ≤ 64KB（`MAX_MESSAGE_BYTES`，`gateway.py:58`，超限由 websockets 断开）；约 100ms 一帧；空帧忽略（`gateway.py:640-650`） |
| `flush` | `{"type":"flush"}` | 把正在进行的段立刻收尾（`worker.py:234-237`） |
| `drain` | `{"type":"drain"}` | 把现有的段全部收尾，发完对应结果后回 `drained`（迁移时使用）（`worker.py:238-239, 419-424`） |

gateway 不原样转发 hello，而是重建（只放行校验过的 `src`/`dst` 和固定的 `sample_rate`/`format`），客户端塞的多余字段到不了 worker（`gateway.py:554-559`）。

### 节点发给客户端

| 事件 | 形状 | 出处 |
|---|---|---|
| `ready` | `{"ev":"ready","cold":bool,"load_s":float,"engine":{...,"translator":str}}` | `worker.py:301-304`；`engine` 来自引擎 `info()`，再加 `translator`（翻译器不可用时为 `"none"`） |
| `final` | `{"ev":"final","id":int,"a0":float,"a1":float,"text":str}` | `worker.py:468-469`；`id` 每会话从 1 起；`a0`/`a1` 是从 hello 起算的音频秒数（按收到的样本数计，保留 3 位小数），夹在不含垫的段边界内（`worker.py:454-465`） |
| `translation` | `{"ev":"translation","id":int,"text":str}` | `worker.py:488`；`id` 对应某条 `final`，晚于 `final` 到达 |
| `status` | `{"ev":"status","code":str,"text":str}`；`translate_failed` 额外带 `id:int` | 见下表 |
| `drained` | `{"ev":"drained"}` | `worker.py:423` |

gateway 只向客户端放行这五种事件（`_CLIENT_EVENTS`，`gateway.py:103`），其它事件丢弃并记日志。节点**不发** volatile/draft；流式模式留作后续扩展。

### status 码值

| code | 来源 | 语义 |
|---|---|---|
| `audio_dropped` | gateway（`gateway.py:76, 522-523`） | 上行音频积压（队列 `AUDIO_QUEUE_MAX_FRAMES = 200` 帧，约 20s，`gateway.py:69`），最旧的帧被丢弃，**这段时间的识别结果可能缺内容**。被丢的帧原位换成等长的零 PCM（`gap`），所以 worker 的样本时钟不前移、`a0`/`a1` 仍与客户端一致（`gateway.py:196-201, 229, 617-623`）。ready 之前（冷加载期间）的丢帧只记账，ready 送达后合并通知一次；之后按 1s 合并（`gateway.py:663-669, 515-518`）。会话**不中断** |
| `backlog_dropped` | worker（`worker.py:413`） | worker 内部处理积压：asr 队列按「段」计上限 8（`MAX_ASR_BACKLOG`），tx 队列按「句」计上限 64（`MAX_TX_BACKLOG`），超限丢**最旧**的（`worker.py:74-75, 399-413`）。与 `audio_dropped` 的区别：`audio_dropped` 丢的是还没进 worker 的原始音频（时间轴用静音补齐），`backlog_dropped` 丢的是已分段待识别/待翻译的内容（时间轴上留空）。会话不中断 |
| `worker_crashed` | gateway（`gateway.py:586, 97-102`） | worker 在会话中退出：退出码非 0 且不是 2/3/4，**或退出码为 0**（会话进行中 worker 不该自己退出），或无法拉起/管道写失败后等不到退出码（记为 -1）。文案「识别进程异常退出」 |
| `protocol_error` | worker 自发 + gateway 映射 | worker 退出码 2（`worker.py:206-207`） |
| `engine_load_failed` | worker 自发 + gateway 映射 | worker 退出码 3（`worker.py:270-272`） |
| `internal_error` | worker 自发 + gateway 映射 / 仅 gateway 映射 | worker 退出码 4（主线程异常 `worker.py:208-211`；工作线程 BaseException 直接 `os._exit(4)` `worker.py:545-555`） |
| `translator_unavailable` | worker（`worker.py:288`） | 翻译器构造失败，只输出原文；会话继续 |
| `translate_failed` | worker（`worker.py:490, 499`） | 一句翻译失败或异常，事件带同一句 `final`/`translation` 使用的 `id`；客户端应按该 `id` 用本机翻译补齐，并避免把原始码 `translate_failed` 直接显示给用户；会话继续 |
| `asr_error` | worker（`worker.py:446`） | 一段识别失败，已跳过；会话继续 |

其中 `protocol_error`/`engine_load_failed`/`internal_error` 对客户端是**致命**的：通常路径下 worker 先自发一条 status，退出后 gateway 把 worker 退出前已产出的事件刷完，再补发一条同码的 status，然后以 close `1011` 关闭连接（`gateway.py:590-597`）；但 `internal_error` 存在两种路径：主线程异常走上述自发+补发，若为工作线程致命异常退出（`worker.py:545-555` 直接 `os._exit(4)`），worker 无法自发 status，仅由 gateway 映射发出。客户端可能因此看到同一个 code 两次（或仅一次），应按 code 去重。

### 退出码 → status.code 映射

`_EXIT_STATUS = {2: "protocol_error", 3: "engine_load_failed", 4: "internal_error"}`（`gateway.py:96`），其它任何退出码（含会话中的 0）一律 `worker_crashed`（`gateway.py:586`）。映射只在**会话进行中** worker 退出时触发；gateway 自己按保温到期/关机回收 worker（SIGTERM）不算崩溃（`gateway.py:415-416`）。

### 关闭码

| close code | 原因 | 出处 |
|---|---|---|
| 1000 | 会话达到 4h 上限（`SESSION_MAX_S`） | `gateway.py:60, 580` |
| 1003 | hello 不是 JSON；音频格式不支持；音频帧字节数为奇数；会话中收到非 JSON 或未知消息 | `gateway.py:642, 676, 688, 908, 919` |
| 1008 | hello 超时/缺失/版本不对/语言不合法；重复 hello；控制消息队列溢出 | `gateway.py:682, 686, 897, 902, 911, 914, 924` |
| 1009 | 单帧消息超过 64KB（`MAX_MESSAGE_BYTES = 64 * 1024`，协议层直接拒绝） | `gateway.py:58, 741` |
| 1011 | worker 异常退出（配合上表 status） | `gateway.py:597` |
| 1013 | 忙（会话数达到容量 `sessions >= capacity`）。**判断在读 hello 之前**，超额连接立即被拒 | `gateway.py:1188-1191` |

一个 worker 进程服务多个会话（容量 `capacity`，缺省 2，节点配置 `node_config.json` 的 `max_sessions` 可配 1–4）。各会话共享同一个 worker 进程中的模型与单线程执行器，队列按会话轮转公平取活（FairQueue，不让一个会话的积压饿死另一个）；会话之间状态与事件完全隔离，客户端协议不含 `sid`。心跳：每 10s ping，10s 无应答判断开，再加 2s 的 close 等待（`PING_INTERVAL_S`/`PING_TIMEOUT_S`/`CLOSE_TIMEOUT_S`，`gateway.py:67-71`）。

### 资源上限（S4）

- 音频：200 帧，满了丢最旧并换成静音占位（见 `audio_dropped`）。
- 控制消息：上限 64（`CTL_QUEUE_MAX`，`gateway.py:79`）；紧挨着的 `flush` 合并；仍超限则丢最旧的 `flush`；`drain` 永不丢，全是 `drain`/`hello` 导致无法再收时断开 1008（`gateway.py:445-488`）。

## P3 gateway ↔ worker（本机管道）

worker 命令行：`python -m realtime_subtitle.node.worker`（`WORKER_ARGV`，`gateway.py:99`）。

**上行（gateway 写 worker 的 stdin）**两种消息交错，靠首字节区分（`worker.py:7-17, 298-333`）：

- 控制消息：一行一个 JSON（`hello`/`flush`/`drain`/`end`），以 `{`（0x7B）开头，单行 ≤ `MAX_LINE = 4096`。多会话带整数 `sid`；无 `sid` 走单会话兼容（sid 0）。会话结束 gateway 发送 `end` 通知 worker 收口该会话（清分段器/语言/草稿/翻译器、滑动计入 rtf）。
- 音频帧：
  - 旧：4 字节大端长度 + s16le PCM，长度 ≤ `MAX_FRAME = 64KB`，且为偶数；大端首字节恒为 0x00，进 sid 0。
  - 新：首字节 `0x01` + 4 字节大端 `sid` + 4 字节大端长度 + s16le PCM（`gateway.py:583-585`，`worker.py:317-324`）。
- 其它首字节、超长行、非 JSON、非对象、会话（hello）之前来的音频/控制、未知控制类型，一律协议错误（退出码 2）。未知 sid 的音频或控制丢弃该帧不打死其它会话。

**下行（worker 写 stdout）**：一行一个 JSON 事件，格式同 P2；单行上限 1MB，超限 gateway 杀掉 worker（`gateway.py:101, 626-630`）。stdout 只走协议，`main()` 一开始就把 fd 1/2 指向 `/dev/null`，第三方库的 print 到不了事件流和日志（`worker.py:741-753`）。hello 携带显式 `sid` 时，worker 事件也携带 `sid`；gateway 按 `sid` 路由至对应客户端连接并剥除 `sid` 字段后转发（`gateway.py:659-673`）。

**会话与并发调度**：
- worker 内每个会话（sid）独立维护分段器、VAD、语言对、翻译器和句子 id；识别与翻译各单线程运行，队列按会话轮转取活（`FairQueue`，`worker.py:118-212`），避免单会话积压饿死其它会话。
- hello 与 ready 一一对应：worker 对每条 hello 恰好回一个 ready（带 `sid`）。保温中的 worker 跨会话复用。单个会话结束（收到 `end` 或客户端断开）只收口并释放该 sid 的状态与排队任务；最后一个会话结束后才开始 120s 保温计时（`gateway.py:551-565`）。

**退出码**（`worker.py:59-63`）：

| 码 | 含义 |
|---|---|
| 0 | 正常：stdin 关闭，或收到 SIGTERM（`worker.py:356-359, 756-773`） |
| 2 | 协议错误 |
| 3 | 引擎加载失败 |
| 4 | 内部错误（含工作线程被 BaseException 杀死，`worker.py:782-793`） |

**生命周期**（`gateway.py:97-98, 538-565, 685-712`）：无会话 = 无 worker；合法 hello 到达才 spawn；最后一个会话结束后保温 `WORKER_KEEPALIVE_S = 120s`，到期 SIGTERM，`WORKER_KILL_GRACE_S = 3s` 后 SIGKILL。gateway 回收只发 SIGTERM、不关 stdin，worker 在 SIGTERM 处理函数里尽力收口活跃会话的 rtf（限时拿锁 0.5s）后 `os._exit(0)`（`worker.py:756-773`）。

## 与 v1 的差异

v1（`realtime_subtitle/remote/server.py`，已在 TK-006 删除）是外包算力验证版，v2 取代它。

| 项 | v1 | v2 |
|---|---|---|
| 路径 | `WS /v1` | `WS /v2/session` + `GET /v1/info` |
| 端口 | 默认 8790（mini2 实际用 8791） | 固定 8791；8790 是 iPhone 采集服务，勿占 |
| hello 版本 | `v:1` | `v:2` |
| 进程模型 | 单进程，直接包装 `WhisperQueueTranslator` | gateway（常驻 <50MB）+ worker（按需、保温 120s、多会话复用单进程与模型） |
| 事件时间 | `t`（服务端流逝秒数），含 `volatile`/`draft` | `final` 带 `a0`/`a1`（按收到样本数计的音频秒数），不发 volatile/draft |
| `final`/`translation` | 同一回调里连发一对 | 分开：先 `final`，译文就绪后再发 `translation`（`id` 关联），识别延迟不被翻译拖累 |
| 控制消息 | 仅 hello | 增加 `flush`、`drain`/`drained`（迁移用）、`end`（会话结束收口） |
| 积压处理 | 无明确上限 | 音频 200 帧、控制 64、worker 段 8/句 64，按会话轮转，满了丢最旧并发 `audio_dropped`/`backlog_dropped` |
| 错误上报 | 通用 status | 结构化 `status.code`，worker 退出码映射 |
| 节点发现 | 无 | `GET /v1/info`（P1）供客户端路由 |
| 部署 | 手工 nohup | `scripts/node/install_node.sh` 装 LaunchAgent `com.realtimesubtitle.node`，失败自动回滚 |

## 隐私与安全边界

- **只绑 Tailscale**：TCP 监听地址每次启动现取 `tailscale ip -4`，且必须是 `100.64.0.0/10` 的 IPv4，拒绝通配/组播/主机名/带空白的值（`gateway.py:146-192`）；取不到则指数退避重试（1s 起，上限 60s）。调 CLI 时子进程环境追加 `TAILSCALE_BE_CLI=1`（`gateway.py:172`）：App Store 版 Tailscale 在 launchd 这种没有 `SHLVL` 的环境里会按 GUI 启动、往 stdout 打报错且退出码 0（2026-10-09 mini2 实测）。失败日志只记固定原因码 `tcp_bind_failed attempt=N reason=<code>`（`gateway.py:1112`），`<code>` ∈ `tailscale_cli_missing` / `tailscale_cli_failed` / `tailscale_cli_timeout` / `tailscale_no_ipv4` / `tailscale_not_ip` / `not_tailscale_range` / `wildcard_or_multicast` / `bind_failed` / `unexpected_error`，不写 CLI 原始输出。`install_node.sh` 第 10 步要求 gateway 进程确实在 `<Tailscale IP>:8791` LISTEN 且无通配监听，否则回滚。
- **客户端补会话路径**：P6 的 `nodes.json` 里 `url` 只写 `ws://<MagicDNS 名>:8791`（安装脚本即如此写），rslite 连接时路径为空就补 `/v2/session`，`/v1/info` 由同一主机派生。另一路是本机 UDS，目录 0700、socket 0600，目录是 symlink、属主不对、权限不对一律拒绝启动，并逐连接校验对端 uid（`gateway.py:1032-1039, 1072-1092, 1143-1155`）。
- **每设备 token 与鉴权**：
  - 支持单 token 文件（`token`，作为 `default` 设备加载）及每设备 token 目录 `rs-node/tokens/*.token`（文件名为设备名，正则 `^[A-Za-z0-9_.-]{1,64}$`）。权限要求 0600、属主本人（Windows 经 icacls 锁定为仅当前用户+SYSTEM）。每把 token 至少 32 字符（`MIN_TOKEN_CHARS`）。
  - 启动加载，Mac 收到 `SIGHUP` 信号重载，Windows 每 10s 检查目录 `mtime` 重载。吊销 = 删除对应 `*.token` 文件。
  - 鉴权逻辑（`TokenBook.match`）对账本中每把 token 常量时间比较（`hmac.compare_digest`），全部比完再出结论，杜绝提前返回的时序泄露。重复的 `Authorization` 头一律不认。
- **日志无正文无 token**：gateway 和 worker 都只记事件类型、时长、字节数、帧数、错误码/类名；转录/译文正文、token、hello 字段值都不进日志。日志中仅记录设备代号（如 `device=mac`），绝不记 token。websockets 自带的 DEBUG 日志会逐帧打印文本，所以给它单独一个钉在 WARNING 的 logger（`gateway.py:55-56`）；worker 的 `_log` 还会把形状不像「代号」的字符串值抹成 `?`，异常只记类名不记消息（`worker.py:69, 213-225`）。
- **音频不落盘**：音频只在内存队列和管道里流转，不写任何文件；worker 写盘的只有 `asr_state.json`（模型名、后端、rtf、翻译器名），权限 0600，原子替换（`worker.py:379-386`）。
- **`/v1/info` 的最小暴露**：`in_use` 只给布尔值，`translator_ok` 只给布尔值，`hw_hash` 只给哈希前 16 位；原始空闲秒数、Ollama URL/错误文本和硬件 UUID 不出 `info.py`（`info.py:9-10`）。

## 客户端侧：Windows Python 客户端

自 v3.2.0 起，Windows Python 客户端通过 `realtime_subtitle/remote/` 接入节点协议 v2（协议本身与节点服务端保持完全一致）：

1. **连接与代理隔离**：
   - 节点客户端连接必须显式指定 `trust_env=False`，禁止走系统环境代理。
   - 解析目标 URL 主机名后，严格校验其 IP 地址必须属于 `100.64.0.0/10`（Tailscale CGNAT 网段）；校验通过后，必须**直接钉住该 IP 发起连接**，禁止让底层库进行二次 DNS 解析，防止潜在的 DNS 漂移或绕过地址边界。

2. **凭据安全**：
   - 节点条目与 token 存放于 `%APPDATA%\realtime_subtitle\`，权限通过 icacls 去掉继承：groq.key 只给当前用户；节点 token 与 nodes.json 给当前用户和 SYSTEM（复用节点安装的 Lock-Down）。
   - Token 仅在内存中读取并通过认证头发送，绝不写入命令行参数、日志或崩溃报告。

3. **冷启动与音频缓冲**：
   - 客户端连接 `WS /v2/session` 后 5s 内发送 `hello`（`v: 2`，采样率 16000，`s16le`）。
   - 在收到服务端的 `ready` 事件前（节点 worker 冷启动需要数秒），客户端在上行发送端维护有界音频缓冲区，既不阻塞本地实时音频捕获，又避免丢弃冷启动期间的有效语音片段。

4. **双路字幕与结果融合**：
   - 本机 Faster-Whisper 先行产生初步字幕供实时显示；
   - 收到节点返回的 `final`（带 `a0/a1` 音频时钟）及关联的 `translation` 事件后，由客户端 `line_store` 将节点高精结果与本机字幕进行时间轴对齐，并依据 P5 规则（并集累计覆盖 ≥50%）平滑替换本机字幕。

5. **路由、防环回与降级**：
   - 节点开关在 Windows 客户端上默认关闭；
   - 客户端通过 `node_id` 识别本机身份：当同一台机器既是算力节点又是客户端时，自动跳过自身以避免环回连接；
   - 节点连接异常或超时时无缝回退本机识别；从云端退到节点时，等一段静音再连，避免一句话被切成两半。
