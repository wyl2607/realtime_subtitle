# Spec 007 — 节点多设备接入 + 手机配对（手机版用电脑算力的前提）

状态：requirement（2026-10-11 用户确认：安卓先做、本 Mac 开发、一加 13 主测；iOS 以后）

## 背景
- 节点协议 v2 同时只服务一个会话：第二个连接在读 hello 前就收到 close 1013/busy（`docs/protocol-v2.md` P2）。Mac rslite 在用 mini2 时，手机连 mini2 必被拒。
- 节点 token 现在由 `install_node.sh` 生成，一台节点一把，靠 ssh 拷到客户端文件里。手机没有 ssh、没有文件系统入口，需要配对流程。
- 安卓 App（`wyl2607/realtime_subtitle_android` v0.4.0）全本地跑：ASR 占 CPU 约 90%、发热、中文等待 P50 2.4–3.7s——用节点算力正好解决，本地识别留作兜底。

## 目标
1. 一台节点可同时服务多台设备（容量可配）。
2. 每台设备一把独立 token，可单独吊销。
3. 手机扫码配对，无需 ssh、无需手抄密钥。
4. 安卓 App 增加「电脑算力」模式，连不上自动回本机识别。

## 设计

### A. 多会话（gateway + worker）
- **一个 worker 进程复用模型，服务多个会话**（不是每会话一个进程：mini2 16GB 上每份 turbo 约 2GB，按进程复制不划算）。gateway↔worker 管道消息加 `sid`（会话号）；worker 每会话一份分段器/语言状态，识别调用仍在单线程执行器里串行（CLAUDE.md §7 第 15 条）。
- 容量 `max_sessions`：写在节点配置，默认 Mac 节点 2、Windows N 卡节点 2；超出容量才回 1013/busy（判断仍在读 hello 之前）。
- `/v1/info` 新增 `sessions`（当前会话数）、`capacity`；`busy` 语义改为 `sessions >= capacity`，旧客户端不需改动即兼容。
- 公平：worker 对各会话的识别请求轮转，不让一个会话的积压饿死另一个；`rtf` 仍按会话结束滑动更新。路由打分把 `sessions/capacity` 计入负载（新客户端）。

### B. 每设备 token
- 节点侧 token 目录 `rs-node/tokens/<device>.token`（0600 / Windows icacls 仅本人+SYSTEM），gateway 启动和收到 SIGHUP（Windows：文件时间变化轮询）时重载；鉴权对每把 token 做常量时间比较。现有单 token 文件作为 `default` 设备迁入，老客户端不受影响。
- 日志只记设备名，不记 token。吊销 = 删文件。

### C. 配对
- 在 Mac 上运行 `bash scripts/node/pair_device.sh <节点 ssh 别名> <设备名>`：ssh 到节点生成新 token 写入 tokens 目录，在本机终端打印二维码（`qrencode -t ansiutf8`，没有就打印配对串并提示安装）。以后 rslite 设置页「算力节点」加「配对手机…」按钮做同样的事。
- 二维码内容：`rsnode://pair?v=1&id=<node_id>&host=<tailnet FQDN>&port=8791&tok=<token>&name=<设备名>`。**二维码即密钥**：只在用户点按钮后显示、显示 60 秒自动隐藏、不写日志不落盘。
- 手机端：扫码 → 校验 host 能解析到 100.64.0.0/10 → `GET /v1/info` 核对 node_id → 存入 Android Keystore 加密的偏好（token 不出现在日志、不进备份：`android:allowBackup` 排除）。

### D. 安卓「电脑算力」模式
- 模式：仅本机（默认）/ 自动（有节点用节点，否则本机）/ 指定节点——与 rslite 一致。
- 客户端：OkHttp WebSocket，协议 v2 hello/音频/flush/drain、处理 ready/final/translation/status/translate_failed；心跳与 1013/1005 处理照 rslite 经验（CLAUDE.md §7 第 17 条）。
- 安全：连接前解析 host，要求全部地址在 100.64.0.0/10，钉 IP 连接；OkHttp 关代理（`Proxy.NO_PROXY`）。与 Python/Swift 客户端同一规则（CLAUDE.md 第 4 节第 47 条）。
- 兜底：节点断开/忙/超时 → 立刻切回手机本地 ASR+MT（现有管线），状态行「本机」；节点恢复后接回。音频在切换时不丢（本地管线始终在跑能量分段，或保留最近 N 秒环形缓冲重放）。
- 流量：先发 16kHz s16le（≈32KB/s，Wi-Fi 足够）；手机流量下的 Opus 压缩列为后续（需协议 v2.1 加 `format:"opus"`）。

## 分阶段
| 阶段 | 内容 | 端 |
|---|---|---|
| M0 | gateway/worker 多会话 + info 字段 + 每设备 token + 迁移 | 节点（Python） |
| M1 | `pair_device.sh` + 二维码；节点重装（需用户批准远程操作） | 节点 + Mac |
| M2 | 安卓节点客户端 + 模式切换 + 扫码配对 + 兜底 | 安卓 |
| M3 | 真机验收：一加 13 + mini2/Windows，Mac 与手机同时连同一节点 | 全部 |
| 后续 | Opus 压缩；rslite 设置页配对按钮；iOS（复用 rslite Swift 核心，抽共享包） | — |

## 验收
- Mac rslite 与一加 13 同时连 Windows 节点，两路字幕都正常，互不阻塞；第三台在容量 2 时收到 busy 并回本机。
- 吊销手机 token 后手机下次连接 401，状态行回「本机」，Mac 不受影响。
- 节点 host 解析到非 tailnet 地址时手机拒连（单测 + 手工改 hosts 验证）。
- 手机断 Wi-Fi → 字幕不断（切本机）；恢复后接回。
