# 001 Draft：外包算力版产品化技术方案

- 依据：[requirement.md](requirement.md)（已确认，53771ae）
- 状态：草案，等待 `/sdd-review`
- 日期：2026-10-08

## 0. 一句话

**每台机器都可以是「节点」（node）。** 节点平时只常驻一个很轻的网关进程；有会话时才拉起识别子进程，会话结束并空闲超时后结束子进程。

客户端 rslite 一直在本机跑省电小模型（系统识别加系统翻译），立即出字。同时按路由打分，选出当前最合适的节点，让它对同一段音频做「整段精修」，再按音频时间把本机草稿替换掉。

服务端不再做流式识别。

---

## 1. 技术方案

### 1.1 架构

```text
┌──────────── 客户端 rslite（MacBook Air）────────────┐
│ CaptureSource(tap) ─┬─▶ Pipeline（系统识别+系统翻译）─▶ Overlay（立即上屏）     │
│   (音频分发，统一样本时钟) │                                     ▲ 按 [a0,a1] 替换 │
│                     └─▶ NodeClient ──WebSocket/UDS──▶ 节点 ──final(a0,a1)──┘  │
│ NodeRouter：读节点清单 → 探测 /v1/info → 打分 → 在停顿处迁移                     │
└────────────────────────────────────────────────────────────┘
节点（mini2；以后加 Windows 主机）
  rs-node gateway（launchd LaunchAgent，常驻 <50MB，不加载模型）
     └─ 有会话时 spawn → worker 子进程（VAD 分段 → Whisper 整段识别 → 翻译）
        会话结束 + 空闲 120s → 结束 worker（模型随进程释放）
```

### 1.2 服务端引擎：按停顿分段、整段识别（替代现有流式流水线）

- worker 收 16kHz s16le 音频。每 0.5s 用 Silero VAD（复用 `faster_whisper.vad`，Mac 的 venv 里已经有）判断语音段。
  - 遇到 ≥0.6s 的静音就把这一段收尾。
  - 一段超过 15s 时，在段内能量最低处强制切开。
- 每段**只调用一次** Whisper，作为可插拔的 `AsrEngine` 接口：Mac 上用 MLX turbo，下一期 Windows 用 faster-whisper CUDA。
  - 这一段的 `a0`/`a1` 由服务端按**收到的样本数**算出，是精确的会话内音频时间。
  - Whisper 段内的分句和词时间戳，用来把一段拆成多句，每句都有自己的 `[a0, a1]`。
- 翻译用可插拔的 `Translator` 接口。默认是系统翻译（`rstranslate`），节点上的语言包状态不是 installed 时退回 Ollama。翻译时把同一会话的前两句作为上下文。只有 Ollama 路径用得上上下文；系统翻译是逐句的。
- 复用现有代码的部分：`asr/backends.create_whisper_model`（包括 MLX 单线程执行器那个修复）、`text_rules` 里的德语断句和幻觉黑名单、`translate/apple_translate.AppleTranslator`。**不复用** `WhisperQueueTranslator`。
- 预计延迟：一段说完 → 0.6s 静音判定 → M4 上整段识别约 1–2s → 翻译 0.1s（系统）或 1–3s（qwen）。也就是**说完后约 2–4s 出精修**，比现在 C 的 5.6–12.3s 快，计算量也小：每段只识别一次，不再每 0.5s 重复识别。

### 1.3 节点生命周期：常驻网关 + 按需起的子进程

- **gateway**（`python -m realtime_subtitle.node.gateway`）：
  - 只 import `websockets`、`json`、`subprocess`，不 import numpy 或 mlx，常驻目标 <50MB。
  - 由 launchd 的 **LaunchAgent** 守护（`KeepAlive`、`RunAtLoad`）。用 LaunchAgent 是因为 Metal 和系统翻译都需要用户会话，要求 mini2 保持登录。
  - 监听两处：
    - Tailscale IP:8791，用于远程，需要 Bearer token。
    - Unix 套接字 `~/Library/Application Support/rs-node/gw.sock`（权限 0600），本机直连，靠文件权限鉴权，不需要 token。
- **worker**：会话开始时由 gateway 启动 `python -m realtime_subtitle.node.worker`，两者之间用 stdin/stdout 管道通信：音频帧进，JSON 事件出。
- **空闲释放**：
  - 会话结束后，worker 保温 120s，方便快速重连。超时后 gateway 先 SIGTERM，等 3s 再 SIGKILL，模型随进程一起释放。
  - 客户端被强杀或断网时，靠 websockets 自带的 ping/pong（`ping_interval=10`、`ping_timeout=10`），约 20s 内发现会话已断，然后进入同样的空闲计时。
- **冷启动**：worker 启动加模型加载，M4 上实测 Whisper 约 11s。混合档里有 B 先出字，用户感觉不到冷启动；纯精修的首句要求 ≤15s。

### 1.4 协议 v2（替代 v1；v1 只有我们自己用，直接废弃）

- `GET /v1/info`（需要 token；本机套接字不需要）返回：
  `{"node_id","hw_uuid","v":2,"asr":{"model":"whisper-large-v3-turbo","backend":"mlx","rtf":0.08},"translator":"apple|ollama:qwen3.5:4b","busy":bool,"on_ac":bool,"user_idle_s":int,"worker":"cold|warm"}`
  - `rtf` 是节点安装时自测的值，后面每次会话结束时滑动更新。
  - `user_idle_s` 来自 `ioreg HIDIdleTime`，表示这台机器上的人多久没碰键盘鼠标了。
- WebSocket `/v2/session`：
  - hello：`{"type":"hello","v":2,"src","dst","sample_rate":16000,"format":"s16le","context":true}`
  - 节点回 `ready`，内容包括 `engine`、`cold`（布尔）、`load_s`。
  - 音频是 100ms 一帧的二进制，和 v1 一样。
  - 控制消息：`{"type":"flush"}`；`{"type":"drain"}`（迁移用：节点把手上的段全部收尾，发完结果后回 `{"ev":"drained"}`）。
  - 节点 → 客户端：`{"ev":"final","id","a0","a1","text"}`、`{"ev":"translation","id","text"}`、`{"ev":"status",...}`、`{"ev":"drained"}`。**没有 volatile**，即时出字由本机 B 负责。
  - `a0`/`a1`：从 hello 起算的会话内音频秒数，按样本数计算。
- 节点同时只服务一个会话，忙时回 close `1013`。这一条保持不变。

### 1.5 客户端：统一时钟、混合替换

- **AudioFanout**：一路采集分给两处，一处喂 B（`Pipeline`），一处喂 `NodeClient`。两边共用**同一个样本计数时钟**，单位是会话内秒。
  - B 侧开启 `ResultAttributeOption.audioTimeRange`，拿到每句的音频时间。
  - 节点侧：会话开始时记下时钟偏移 `off`，收到的 `a0`/`a1` 加上 `off`，映射到客户端时钟。
- **替换规则**（Overlay）：
  - 每行带一个 `[t0,t1]` 和来源（B 或节点）。
  - 节点的 final 到了，就把与它的时间区间**重叠超过各自时长 50%** 的 B 行全部换掉，换上节点给的原文加译文。
  - 换上去的行不再被 B 改动。没有可以对上的行时，按时间顺序插入。
  - 已经滚出屏幕的 B 行不追回，只更新历史。
- **单实例保护**：用 `~/Library/Application Support/rslite/lock` 加 `flock`。第二个实例启动时提示并退出，避免两个 tap 互相删掉对方的聚合设备。

### 1.6 路由：节点清单、探测、打分、迁移

- **节点清单**：`~/.config/rslite/nodes.json`，由安装脚本写入，格式是 `[{id, url, token_file}]`。本机节点不写进清单，靠本机套接字是否存在来发现。
- **探测**：启动时探测一次，之后每 30s 一次，对每个节点调 `/v1/info`。超时 2s 判为离线。
- **打分**（分数越高越好，权重写在一处常量表里）：
  - `quality = asr_rank(model) + translator_rank`（large-v3 > turbo > 小模型；系统翻译和 qwen 9b 以上 > qwen 4b）
  - `speed = −rtf×k`
  - 惩罚项：`busy` 直接排除；`user_idle_s < 60` 扣分（那台机器有人在用，把算力让给他）；`!on_ac` 扣分；网络 RTT 超过 100ms 扣分。
  - **本机节点**：等同于普通节点，另外加上「无网络」加分。但如果本机是笔记本、用的是电池，或者没过 1.7 节的门槛，就排除。
- **迁移**：
  - 条件：新节点的分数比当前节点高出 margin，并且连续 2 次探测都是这样，避免来回切。
  - 时机：等到下一个静音点（B 的 volatile 清空，或者本地 VAD 判出 ≥0.6s 静音）。
  - 做法：给旧会话发 `drain`，同时开新会话，从静音点开始送音频。旧会话等 `drained`，最多 3s，然后关闭。
  - 迁移期间 B 一直在出字，最坏情况只是某一句缺精修，B 的版本会留在屏幕上，不会断，也不会重复。
  - 每次选择都打一行日志：节点、分数构成、迁移原因。
- **服务器本机当主力机**：mini2 上运行 rslite 时，本机套接字存在，而且 `/v1/info` 返回的 `hw_uuid` 等于本机 IOPlatformUUID。于是本机节点通过 UDS 直连，不走网络、不需要 token，第 4 条需求自然满足。

### 1.7 本机准确档的门槛（chip + 内存 + 电源）

- 允许把**本机节点**用作精修的条件：`(芯片名含 Pro/Max/Ultra 或 hw.memsize ≥ 24GB) 并且 插着电源`，或者本机是台式机（没有电池）。
- 数据来源：`sysctl machdep.cpu.brand_string`、`hw.memsize`、`IOPSCopyPowerSourcesInfo`。
- MacBook Air M2 16GB 一律不过门槛。mini2（M4 16GB）是台式机，算过。
- 引擎：这一期**本机节点和远程节点用同一套 worker**（MLX）。WhisperKit（CoreML/ANE）作为下一期的候选，见 2.4。

### 1.8 一键安装：`scripts/node/install_node.sh <ssh-host|--local>`

1. `git bundle` 当前分支，scp 到目标机，clone 或 fetch 到**内置盘** `~/rs-node`。
   - 脚本会检测 `~/projects` 这类悬空软链接，统一不使用。
2. 在目标机上执行 `scripts/macos/install.sh --skip-models`，再 `uv pip install -r realtime_subtitle/node/requirements.txt`，然后构建 `rstranslate`。
3. 生成节点 token（目标机上已有就沿用），用 0600 权限写到两端。通过 stdin 传输，不出现在命令行或日志里。
4. 在目标机上探测 Tailscale IP，写入 LaunchAgent plist，再 `launchctl bootstrap gui/$UID`，然后 `kickstart`。
5. **自检**：调 `/v1/info` 拿到 `rtf`（用一段内置的 5s 德语音频做基准），通过后把节点写进客户端的 `nodes.json`（按 id 去重）。
6. 检查系统翻译语言包。不是 installed 时，打印需要在目标机界面上操作的步骤，这一步不能自动化。
7. 幂等：重复执行时只更新代码和 plist，token 和节点清单不重复写。

---

## 2. 备选项

### 2.1 服务端识别方式
| 方案 | 结论 | 理由 |
|---|---|---|
| A. 沿用现有 `WhisperQueueTranslator` 流式（现在的 C） | **否决** | 句子的时间信息在翻译层丢了，要拿到得改 1900 行的核心文件；local agreement 每 0.5s 重复识别，浪费服务端算力；实测延迟 5.6–12.3s |
| B. VAD 分段、整段识别 | **采用** | 时间信息天然精确；每段只识别一次；整段上下文更准；实现独立，也适合 Windows |
| C. 服务端也提供 volatile 流 | 推迟 | 混合档不需要；Windows 客户端没有系统识别，可能会用到，到时以协议 v2 的扩展 `mode:"stream"` 的形式加 |

### 2.2 节点生命周期
| 方案 | 结论 | 理由 |
|---|---|---|
| 常驻完整服务（现在的做法） | 否决 | 违反「不用时不占用」，模型常驻约 2GB 以上 |
| launchd socket activation，按连接拉起 | 否决 | Python 要用 ctypes 调 `launch_activate_socket` 才能接管 launchd 给的 socket；每次连接都冷启动，没有保温窗口；调试麻烦 |
| 轻网关 + 按需起的 worker 子进程 + 空闲超时 | **采用** | 进程退出就释放 MLX 内存，最干净；保温窗口可以调；网关很轻 |

### 2.3 节点发现
| 方案 | 结论 | 理由 |
|---|---|---|
| mDNS/Bonjour | 否决 | 不能可靠地跨 Tailscale；会在局域网里暴露节点的存在 |
| 扫描 `tailscale status` 的所有设备 | 否决 | 会探测无关设备，而且每台都得有 token |
| 安装脚本写节点清单，再用 `/v1/info` 探测 | **采用** | 显式、私密、可审计；Windows 节点装好后也只是往清单里加一行 |

### 2.4 本机准确档用哪个引擎
| 方案 | 结论 | 理由 |
|---|---|---|
| 复用节点 worker（MLX，GPU） | **本期采用** | 不用写新代码，本机和远程是一套 |
| WhisperKit（CoreML/ANE，Swift 进程内） | 下一期评估 | 走 ANE，可能明显更省电；但要引入新的第三方依赖，还要单独跑一次基准 |
| A 版 Python 桌面应用 | 否决 | 就是它把机器跑热的；而且它是整套界面应用，不适合作为引擎嵌入 |

### 2.5 混合档怎么对齐
| 方案 | 结论 | 理由 |
|---|---|---|
| 文本模糊匹配 | 否决 | B 和 Whisper 的断句、用词都不一样，匹配不稳定 |
| 统一样本时钟加音频时间区间重叠 | **采用** | 两侧都能拿到精确的时间 |

---

## 3. 影响面

### 3.1 新增
- `realtime_subtitle/node/`：`gateway.py`、`worker.py`、`segmenter.py`（VAD 分段）、`engines.py`（`AsrEngine` 和 `Translator` 接口及适配）、`info.py`（能力、空闲时间、电源）、`requirements.txt`
- `scripts/node/install_node.sh`、`scripts/node/com.realtimesubtitle.node.plist.template`
- `macos-native/Sources/rslite/`：
  - `AudioFanout.swift`
  - `NodeClient.swift`（替代 `RemotePipeline.swift`）
  - `NodeRouter.swift`
  - `HybridEngine.swift`（组合 B 和 NodeClient）
  - `Capability.swift`
  - `SingleInstance.swift`
- 测试：
  - `tests/test_node_gateway.py`：生命周期、鉴权、忙、空闲回收、UDS
  - `tests/test_node_segmenter.py`：静音切段、15s 强制切、a0/a1 精度
  - `tests/test_node_worker.py`：用假的 ASR 和翻译走一遍事件流
  - 路由打分的纯函数测试，用 Swift 的 test target，或者直接在 rslite 里写一个 `--selftest`

### 3.2 修改
- `Pipeline.swift`：开启 `audioTimeRange`，回调里带上 `[t0,t1]`；音频来源改为来自 `AudioFanout`。
- `Overlay.swift`：每行带时间区间和来源，实现替换规则；状态显示「本机 / 混合·节点名 / 精修中」。
- `main.swift`：新增 `--mode auto|local|hybrid`，去掉 `--remote`（由节点清单替代）。
- `CLAUDE.md` 第 7 节加避坑条目，README 写节点和混合档的用法，`docs/` 写明协议 v1 和 v2 的区别。

### 3.3 删除
- `realtime_subtitle/remote/server.py`、`tests/test_remote_server.py`、`RemotePipeline.swift`。v1 只有我们自己在用，没有外部用户。

### 3.4 依赖
- 节点端只需要 `websockets`（已在用），VAD 复用 `faster_whisper.vad`，都不进根目录的 `requirements.txt`。
- 客户端不引入第三方依赖。

### 3.5 兼容性
- A 版（Python 桌面字幕）在 Windows 和 Mac 上都不受影响，`translator_queue.py` 不改。
- 根目录清单测试：`.governance/` 和 `scripts/node/` 都不在根目录的文件清单里，已验证通过。
- 需要清理 mini2 上现有的 `~/rs-remote` 部署：停掉进程、删掉目录，改由 `~/rs-node` 加 LaunchAgent 接管。

---

## 4. 未解决问题

1. **mini2 的系统翻译语言包**：需要用户在 mini2 的界面上下载一次。没下之前，节点用 qwen 4b（实测有错译）。
2. **LaunchAgent 要求 mini2 保持登录**（Metal 和 Translation 都需要用户会话）。mini2 是否开了自动登录还没确认；如果重启后停在登录界面，节点就不可用。
3. **`HIDIdleTime` 当成「有人在用」的信号够不够准**：远程桌面或屏幕共享时也会更新它。要在 mini2 上实测。
4. **分段参数**：0.6s 静音、15s 上限这两个值，在直播这种连续说话的场景下是否合适，要用真实直播录音调。
5. **保温窗口 120s**：这是在「快速重连」和「及时释放 2GB」之间取舍，要按实际使用习惯调。
6. **迁移 margin 和连续 2 次的去抖**：要在模拟第二节点的验收中调参。
7. **Tailscale IP 变化**（很少见）：节点清单里存的是 MagicDNS 名，还是 IP 加 `hw_uuid` 校验，需要定。
8. **翻译上下文**：只有 Ollama 路径用得上。系统翻译做不到「把前一句一起翻、只取后半句」，这个技巧本期不做。
9. **混合档的错误率评估**：
   - 「最终文本」的定义：精修覆盖到的部分用节点的结果，没覆盖到的保留 B。
   - 用 67s 回放计算，要在评审中确认这个口径。
