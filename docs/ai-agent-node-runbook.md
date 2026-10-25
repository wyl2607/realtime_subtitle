# AI 助手节点部署与运维手册 (AI Agent Node Runbook)

本手册专为协助用户配置和维护实时字幕算力节点的 AI 助手（Claude Code、Codex 等）编写。操作时须严格遵循本手册划定的行为边界与命令清单。

---

## 1. 角色职责与行为边界 (Role & Action Boundaries)

AI 助手在操作算力节点时，必须严格区分**允许自主执行的操作**与**必须征得用户明确同意的操作**：

### 允许 AI 自主执行的操作（安全/只读）：
- 执行安装脚本的 `--dry-run` 预览安装计划；
- 读取本地及远程日志文件（Mac `~/Library/Logs/rs-node/`；Windows `%LOCALAPPDATA%\rs-node\logs\gateway.log`）；
- 查询本地及远程服务状态（`launchctl print gui/<uid>/com.realtimesubtitle.node`、Windows `Get-ScheduledTask -TaskName rs-node-gateway`）；
- 发起只读探针请求 `GET /v1/info`（通过 Tailscale IP 与合规 Token）；
- 检查 Tailscale 状态与网络连通性（`tailscale status`、`tailscale ip -4`）。

### 必须先征求用户许可的操作（高风险/写操作）：
- **真实执行安装命令**（如运行不带 `--dry-run` 的 `install_node.sh` 或 `install_node_windows.sh`，涉及远程代码执行、写盘与环境变更）；
- **修改系统级守护配置**（包括但不限于修改 LaunchAgent plist、注册/注销 Windows 计划任务、变更 Windows 防火墙规则）；
- **写入或覆盖认证凭据**（创建、修改或覆盖 Groq key 文件或节点 token 文件）；
- **删除或清理远程目录**（如移除旧版本备份目录或日志目录）。

---

## 2. 模式决策树 (Decision Tree: Local vs Node vs Cloud)

系统的路由机制遵循 RFC 路由分层与 README 回退顺序（**云端 → 自己的节点 → 本机**）：

```text
[用户发起识别请求]
        │
        ▼
   是否配置并启用云端 Groq？
        ├── 是 (且在限流配额内) ──▶ [Tier 0: 云端 Groq 识别] (延迟极低，超额自动降级)
        └── 否 / 配额已耗尽
                │
                ▼
        是否配置了算力节点？
                ├── 是 ──▶ 节点 /v1/info 是否存活且 busy==false？
                │             ├── 是 ──▶ [Tier 1: 算力节点 offload] (本机初步 + 节点精修)
                │             └── 否 ──▶ 降级到 Tier 2 (字幕不断)
                └── 否
                        │
                        ▼
                 [Tier 2: 完全本机识别] (默认模式，零网络依赖，隐私最高)
```

- **Tier 0（云端 Groq）**：仅在设置面板填入 Key 并打开云端开关时启用。受分钟/日配额限制（20次/分，7200秒/时，2000次/28800秒/天）。
- **Tier 1（算力节点）**：利用 Tailscale 内网中的专属机器；本机先出实时字幕，节点返回高精结果平滑替换；节点繁忙（1013）或网络离线时自动降级到本机。
- **Tier 2（完全本机）**：默认兜底方案；所有音频与文本均留在本地。

---

## 3. 按序命令清单与排查 (Execution Checklist)

所有部署操作均在**控制端（Mac）**上执行。

### 步骤 1：网络与 SSH 连通性检查（可自主执行）
```bash
# 验证控制端能否免密 SSH 登录节点机
ssh -o BatchMode=yes <ssh-host> "echo ok"
# 验证节点机 Tailscale IPv4
ssh <ssh-host> "tailscale ip -4"
```
- **成功判据**：SSH 输出 `ok`；Tailscale IP 处于 `100.64.0.0/10` 网段。

### 步骤 2：Dry-Run 计划预览（可自主执行）
```bash
# 若目标机是 Mac
bash scripts/node/install_node.sh --dry-run <ssh-host>

# 若目标机是 Windows
bash scripts/node/install_node_windows.sh <ssh-host> --dry-run
```
- **成功判据**：退出码为 0，终端打印完整的计划步骤（如预检、node_id、token 等），且敏感 Token 显示为 `<redacted>`。

### 步骤 3：正式部署安装（⚠️ 必须获得用户确认）
```bash
# 部署到 Mac 节点（下发到 Windows 客户端可加 --client <win-ssh>）
bash scripts/node/install_node.sh <ssh-host>

# 部署到 Windows 节点（下发到 Windows 客户端可加 --client <win-ssh>）
bash scripts/node/install_node_windows.sh <ssh-host>
```
- **成功判据**：输出 `✅ 节点安装完成`（或 `✅ Windows 节点安装完成`），退出码为 0。
- **失败排查路径**：
  - **Mac 节点**：
    - 日志文件：目标机 `~/Library/Logs/rs-node/gateway.log` 与 `gateway.err.log`；
    - 服务状态：`launchctl print gui/$(id -u)/com.realtimesubtitle.node`。
  - **Windows 节点**：
    - 日志文件：目标机 `%LOCALAPPDATA%\rs-node\logs\gateway.log`；
    - 服务状态：PowerShell 下执行 `Get-ScheduledTask -TaskName rs-node-gateway`。

### 步骤 4：健康检查与连通性验证（可自主执行）
从控制端经由 Tailscale 带 Token 请求 `/v1/info`：
```bash
TOKEN=$(cat ~/.config/rslite/tokens/<ssh-host>.token)
curl -s -H "Authorization: Bearer $TOKEN" http://<节点Tailscale-IP>:8791/v1/info
```
- **成功判据**：HTTP 状态码为 200，且返回 JSON 满足：
  - `"v": 2`
  - `"busy": false`
  - `"translator_ok": true`
  - `"node_id"` 与安装输出一致

---

## 4. 明确禁止事项 (Strict Prohibitions)

在协助用户排查或编写脚本时，**严禁执行以下操作**：

1. **禁止在 Windows 上执行 `taskkill /im python.exe` 或 `taskkill /im ollama.exe`**：
   - *原因*：会误杀用户自身日常使用的 Python 进程或 Ollama 实例；
   - *合规做法*：必须使用 CIM/WMI 按精确命令行子串匹配终止（如 `scripts/node/windows/node_setup.ps1` 中筛选 `run_gateway.py` 与路径）。
2. **禁止将 Token 或 API Key 放进命令行参数 (argv)**：
   - *原因*：命令行参数会被 `ps` 命令、任务管理器及系统审计日志暴露；
   - *合规做法*：仅通过标准输入（stdin）或权限受保护的文件（0600 / 受限 ACL）传递。
3. **禁止关闭 TLS 证书校验**：
   - *原因*：针对云端 Groq 的请求，严禁设置 `--insecure` 或 `verify=False`。
4. **禁止读取环境变量 `GROQ_API_KEY`**：
   - *原因*：系统明确承诺不读取环境变量，避免因宿主系统的全局环境变量导致音频在用户不知情时意外上传到第三方。
5. **节点客户端禁止走系统环境代理，且禁止二次 DNS 解析**：
   - *原因*：节点客户端若沿用环境代理，会将 Tailscale 内部流量转发给代理服务引发泄露；
   - *合规做法*：必须显式设置 `trust_env=False`（CLAUDE.md §4 第 47 条）；且必须在解析并验证目标 IP 属于 `100.64.0.0/10` 后，直接钉住该 IP 发起连接，禁止使用域名进行二次解析。
