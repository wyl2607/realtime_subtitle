# 算力节点配置指南

本指南面向希望将局域网内另一台性能更强的电脑（如 Apple Silicon Mac 或搭载 NVIDIA 显卡的 Windows 电脑）搭建为**专属语音识别与翻译算力节点**的用户。

搭建完成后，你的主力电脑（客户端）可将繁重的语音识别与大模型翻译交给该节点处理：本机先出低延迟初步字幕，节点返回的高精精修结果随后平滑替换。当节点离线或网络波动时，客户端会自动无缝回退到本机，字幕全程不断。

---

## 一、前置条件

部署脚本在执行前会对控制端与节点机进行严格的前置检查（Preflight）。请在安装前确认以下依赖与环境：

### 1. 网络与连接（通用）
- **Tailscale 内网**：控制端与节点机均须安装并登录 Tailscale，且处于同一 tailnet 内；两台机器均具有 `100.64.0.0/10` 网段的 IPv4 地址（可通过 `tailscale ip -4` 查看）。
- **SSH 免密登录**：控制端必须能通过 SSH 别名（如配置在 `~/.ssh/config` 中的 `mini2` 或 `win-desktop`）免密登录节点机。

### 2. 控制端（运行安装脚本的 Mac）
- 必须安装：`git`、`openssl`（用于生成安全 token）、`python3`（用于写入客户端 nodes.json）、`ssh`。

### 3. 目标节点机要求

#### 若目标机是 Apple Silicon Mac 节点
- **系统硬件**：必须为 Apple Silicon Mac（M1 及以上芯片，系统为 macOS/Darwin）；
- **磁盘空间**：家目录所在磁盘剩余空间不少于 **8GB**（venv 环境与 Whisper 模型约需此空间）；
- **基础依赖**：已安装 `brew`、`ollama`、`uv`、`swift`（脚本会逐个检查 `command -v`）；
- **系统翻译包（推荐）**：在节点机的「系统设置 → 通用 → 语言与地区 → 翻译语言」中下载「德语」与「中文（简体）」离线包。若未下载，节点翻译将自动回退到本地 Ollama；
- **用户登录状态**：节点机**必须存在图形桌面登录会话**（LaunchAgent 运行在 `gui/<uid>` 域中；Metal GPU 渲染与系统翻译均依赖桌面用户会话）。

#### 若目标机是 NVIDIA Windows 节点
- **系统服务**：已安装并启动 Windows OpenSSH Server，支持 SSH 登录；
- **软件环境**：已安装 Python 3.11+、Git 以及 uv（若无 uv 脚本会自动尝试 pip 安装）；
- **显卡与驱动**：搭载 NVIDIA 显卡，驱动正常（`nvidia-smi` 可正常执行，CUDA Version ≥ 12.0）；
- **Ollama 翻译模型**：已安装并运行 Ollama，且已提前拉取目标翻译模型（默认检查 `qwen3.5:4b`，请在节点机上执行 `ollama pull qwen3.5:4b`）；
- **用户登录状态**：节点计划任务以当前用户交互登录触发（Interactive 登录；CUDA 加速需要用户会话）。

---

## 二、安装与下发命令

**所有部署命令均在控制端（Mac）上执行**。

### 1. 部署到 Apple Silicon Mac 节点
```bash
# 步骤 1：先使用 --dry-run 预览计划（不联网、不写文件，Token 显示为 <redacted>）
bash scripts/node/install_node.sh --dry-run mini2

# 步骤 2：正式安装到远程 Mac 节点
bash scripts/node/install_node.sh mini2

# 步骤 3（可选）：安装完成后，同时把该节点条目下发到一台 Windows 客户端
bash scripts/node/install_node.sh mini2 --client win-client
```
*(注：若需在本机部署节点用于本地测试，可运行 `bash scripts/node/install_node.sh --local`，此时走本地 UDS 套接字，不写 nodes.json)*

### 2. 部署到 NVIDIA Windows 节点
```bash
# 步骤 1：先使用 --dry-run 预览计划
bash scripts/node/install_node_windows.sh win-desktop --dry-run

# 步骤 2：正式安装到远程 Windows 节点
bash scripts/node/install_node_windows.sh win-desktop

# 步骤 3（可选）：安装完成后，同时把该节点条目下发到另一台 Windows 客户端
bash scripts/node/install_node_windows.sh win-desktop --client win-client
```

---

## 三、部署检查点（Checkpoints）

安装完成后，请按以下三步逐项验证：

### 检查点 1：确认 Gateway 监听于 Tailscale 端口
节点 gateway 服务固定监听端口 **`8791`**，且**必须仅绑定在 Tailscale IPv4**（`100.64.0.0/10`），禁止绑定 `0.0.0.0`。
- **Mac 节点排查**：在节点机终端执行 `lsof -nP -iTCP:8791 -sTCP:LISTEN`，确认绑定的 IP 是 100.64.x.x。
- **Windows 节点排查**：在节点机 PowerShell 执行 `netstat -ano | findstr 8791`；检查防火墙规则：`Get-NetFirewallRule -Name rs-node-gateway-tailscale`，确认入站限制为 `100.64.0.0/10`。
- **未授权探测**：在控制端执行：
  ```bash
  curl -i http://<节点Tailscale-IP>:8791/v1/info
  ```
  预期结果：返回 `HTTP/1.1 401 Unauthorized`，且包含 `WWW-Authenticate: Bearer`。

### 检查点 2：验证 `/v1/info` 接口返回
在控制端携带 token 进行自检查询（Mac 控制端 token 保存在 `~/.config/rslite/tokens/<节点别名>.token`）：
```bash
TOKEN=$(cat ~/.config/rslite/tokens/<节点别名>.token)
curl -s -H "Authorization: Bearer $TOKEN" http://<节点Tailscale-IP>:8791/v1/info
```
预期返回 JSON 数据结构：
- `v`: 恒为 `2`（协议版本）；
- `node_id`: 安装时生成的节点 ID 字符串；
- `hw_hash`: 机器硬件标识哈希前 16 位；
- `asr`: 包含模型名称（如 `whisper-large-v3-turbo`）、后端（`mlx` 或 `cuda`）及滑动 `rtf`；
- `translator`: `apple` 或 `ollama:qwen3.5:4b`；
- `translator_ok`: `true`（表示系统翻译可用或 Ollama 探活成功）；
- `busy`: `false`（当前无活跃会话）；
- `on_ac`: 是否接通交流电源；
- `in_use`: 节点机最近 60 秒是否有鼠标键盘操作（布尔值）；
- `worker`: 空闲时为 `"cold"`，会话中为 `"warm"`。

### 检查点 3：确认客户端连接状态
- 启动客户端（Mac 原生版 `rslite`，或 Windows 客户端在 ⚙️ 设置面板中开启「使用算力节点」）；
- 查看悬浮窗左下角状态行：应由「本机」更新为**「混合·<节点名>」**（例如「混合·mini2」）；
- 播放一段视频：本机实时出字，稍后状态栏提示精修到达，字幕无缝更新。
- **Mac rslite：节点对比 / 测速**。菜单栏状态项（标题为「字·」加当前模式，例如「字·本机」）里的「设置…」（⌘,）打开「rslite 设置」，进入「算力节点」。表的列是「名称」「识别模型」「翻译」「延迟」「rtf」「状态」「预计」。按钮文案是「测速」，进行中变为「测速中…」。打开这一页时会自动测一次。表下说明原文：「预计每句耗时 ≈ RTT + rtf×4s（RTT 用秒，表里的延迟是毫秒）。这是估算，不是实测整句耗时。本机没有网络延迟，延迟显示为 —。」
- **Mac rslite：指定节点**。在「识别与语言」→「运行模式」选择「指定节点」（另外两项是「仅本机（默认）」和「自动选最快」）。`nodes.json` 里还没有节点时，原文是「nodes.json 里还没有节点。指定节点需要先添加。」有节点时，在「节点」里选定一台（第一项是「请选择」），点「保存」。页上原文：「选定后重启才会钉住这一台。不选具体节点则仍按混合路由。」以及「重启 rslite 后生效」。保存成功显示「已写入 settings.json。重启 rslite 后生效。」「指定节点」写入 `mode` 为 `hybrid`，并把所选 id 写入 `pinnedNode`。
- **命令行备选**：`rslite --mode hybrid --node ID`。usage 原文是 `usage: rslite [--source tap|mic|file:PATH] [--src de-DE] [--dst zh-Hans] [--headless] [--mode auto|local|hybrid] [--node ID] [--selftest] [--set-groq-key|--clear-groq-key|--cloud on|off]`。`--mode` 只能是 auto、local 或 hybrid。只写 `--node ID` 而 mode 仍是 local 时不连节点。命令行上的 `--mode` 和 `--node` 只覆盖这一次启动，不写回 `settings.json`。
- 指定的节点不可用时，状态文案为「<id> 不可用，已用本机」（`<id>` 是指定的节点 id）。该 id 不在节点列表里时，状态文案为「<id> 不在节点列表，已用本机」。

---

## 四、安全与隐私边界

算力节点在设计上恪守最严格的本地化安全边界：
1. **网络隔离**：Gateway TCP 服务每次启动动态获取 `tailscale ip -4`，强制要求必须属于 `100.64.0.0/10`，拒绝任何通配地址或公网暴露；
2. **凭据安全**：Token 采用 32 字节随机熵（64 位十六进制），仅通过标准输入（stdin）或权限严格锁定的文件传递（Mac 设为 `0600`，Windows 通过 `icacls` 移除继承并仅赋权当前用户和 SYSTEM）。Token 绝不出现在命令行参数（argv）、`ps` 进程列表或系统日志中；
3. **音频不落盘**：音频切片仅在内存管道与内存队列中流转，识别完成后立即释放，绝不在节点磁盘生成任何临时音频文件；
4. **日志脱敏**：Gateway 与 Worker 日志仅记录连接事件、耗时、帧数与状态码，**绝不记录任何转录原文或翻译文本内容**。
5. **Swift 客户端也校验 Tailscale 网段**：Mac 原生 `rslite` 在连节点前要求地址属于 `100.64.0.0/10`，并钉成 IPv4 字面量再连。主机名先解析成 IPv4；解析出的任一地址不在该网段就拒绝，文案为「节点地址不在 Tailscale 网段（100.64.0.0/10），拒绝连接：<主机名>」。`/v1/info` 与 `/v2/session` 都先做这一步。

---

## 五、常见问题与避坑指南

1. **悬空软链接问题（Mac 节点）**：
   - *现象*：部分节点机（如 mini2）的 `~/projects` 指向未挂载的外置磁盘，会导致基于该路径的构建失败；
   - *处理*：安装脚本一律固定安装在内置系统盘 `~/rs-node`，临时文件使用 `~/rs-*`，无需且不要依赖 `~/projects`（见 CLAUDE.md §7 第 18 条）。
2. **需要登录桌面会话（Mac / Windows 均适用）**：
   - *现象*：节点机开机或重启后，客户端连不上节点；
   - *根因*：Mac LaunchAgent（`gui/<uid>` 域）与 Windows 计划任务（Interactive）均依赖图形登录会话才会被拉起。此外，Metal/CUDA 驱动与系统翻译均需要用户桌面会话；
   - *处理*：节点机重启后需手动登录一次桌面，或开启系统自动登录（见 CLAUDE.md §7 第 19 条）。排查服务未运行可先检查是否有桌面会话。
3. **Tailscale GUI 模式陷阱（Mac 节点）**：
   - *现象*：App Store 版 Tailscale 在无 `SHLVL` 的 launchd 环境中会启动 GUI 模式并向 stdout 打印错误文本；
   - *处理*：安装脚本与 Gateway 在调用 Tailscale CLI 时均注入了 `TAILSCALE_BE_CLI=1` 环境变量（见 CLAUDE.md §7 第 22 条）。
4. **Windows 节点 Ollama 没启动**：
   - *现象*：识别正常，但节点翻译失败（返回 `translate_failed`），客户端状态行可能短暂提示或自行降级；
   - *处理*：当节点 Ollama 异常或未运行时，客户端会自动启动兜底逻辑——改用**本机翻译**补齐节点识别出的原文，字幕不会中断。此时只需在 Windows 节点机上启动 Ollama 服务即可恢复节点全流程加速。

---

## 六、卸载与回滚

### 1. 自动回滚机制
如果在安装过程中自检失败（例如等待 `/v1/info` 超时：Mac 默认 40 秒、Windows 默认 90 秒），安装脚本会自动执行 Rollback：
- 恢复上一版本 `~/rs-node.prev`（Windows 为 `%USERPROFILE%\rs-node.prev`）；
- 重新注册并启动旧版本服务。

### 2. 手动卸载节点
若不再需要该节点，可在目标节点机上清理：

- **Apple Silicon Mac 节点**：
  ```bash
  # 停止并卸载 LaunchAgent
  launchctl bootout gui/$(id -u)/com.realtimesubtitle.node
  rm -f ~/Library/LaunchAgents/com.realtimesubtitle.node.plist
  # 清理程序文件、配置与日志
  rm -rf ~/rs-node ~/rs-node.prev ~/.config/rs-node ~/Library/Application\ Support/rs-node ~/Library/Logs/rs-node
  ```

- **NVIDIA Windows 节点**（在 PowerShell 中运行）：
  ```powershell
  # 停止并注销计划任务
  Stop-ScheduledTask -TaskName rs-node-gateway -ErrorAction SilentlyContinue
  Unregister-ScheduledTask -TaskName rs-node-gateway -Confirm:$false -ErrorAction SilentlyContinue
  # 删除防火墙规则
  Remove-NetFirewallRule -Name rs-node-gateway-tailscale -ErrorAction SilentlyContinue
  # 清理程序与状态文件
  Remove-Item -Recurse -Force "$env:USERPROFILE\rs-node", "$env:USERPROFILE\rs-node.prev", "$env:LOCALAPPDATA\rs-node" -ErrorAction SilentlyContinue
  ```

- **客户端清理**：
  在控制端或客户端机器上，打开 `~/.config/rslite/nodes.json`（Windows 为 `%APPDATA%\realtime_subtitle\nodes.json`），删除该节点对应的 JSON 对象；并删除 `tokens/` 目录下对应的 `<节点别名>.token` 文件即可。
