# TK-002b — gateway：launchd 下取 Tailscale IP 失败 + 安装自检补 TCP 监听校验

## Goal
mini2 上由 launchd 拉起的 gateway 能取到 Tailscale IPv4 并在 `<Tailscale IP>:8791` 监听；取不到时日志给出不含敏感信息的原因码；`install_node.sh` 的自检在 TCP 没监听时判失败并回滚。

## Context（根因，2026-10-09 本机 Coordinator 在 mini2 只读复现）
- mini2 的 Tailscale 是 App Store 版，`tailscale` 实际是 `/Applications/Tailscale.app/Contents/MacOS/Tailscale`（GUI 主程序，APFS 大小写不敏感，plist PATH 里的 `.../MacOS/tailscale` 就解析到它）。
- 该二进制按环境判断 CLI / GUI 模式：环境里**没有 `SHLVL`**（launchd 正是如此）时按 GUI 启动，往 **stdout** 打 `The Tailscale GUI failed to start: ... (Tailscale.CLIError error 3.)`，**退出码 0**。
- `resolve_tailscale_ipv4` 取第一行 → `validate_host` 抛 ValueError → `_tcp_loop` 只记 `err=ValueError`，无限退避。
- 只读 A/B（env -i + plist PATH，直接 exec）：仅加 `SHLVL=1` → 正常；仅加 `TAILSCALE_BE_CLI=1` → 正常（官方强制 CLI 开关）；加 PWD / `_` / OLDPWD、改 argv[0] 大小写 → 都失败。ssh 交互环境有 SHLVL 所以手测一直正常。
- 安装脚本第 10 步只经 UDS 验 `/v1/info`，TCP 没起来也报成功（`scripts/node/install_node.sh` SWAP_SH 末尾）。
- 预检第 71 行 `ts_ip=$(tailscale ip -4 ... | head -n 1)` 只判非空，同样会把 GUI 报错行当 IP。

## Constraints
- write_scope:
  - realtime_subtitle/node/gateway.py
  - realtime_subtitle/node/info.py（仅当 run_command 需要增加可选 env 参数时）
  - tests/test_node_gateway.py
  - scripts/node/install_node.sh
  - tests/test_install_node.py
- 禁止修改：其他一切路径（尤其 `realtime_subtitle/translate/translator_queue.py`、`node/worker.py`、`.governance/**`、plist 模板除非确有必要且在回报中说明理由）
- RFC P1–P8 冻结不改；S1–S8 全部有效：**永不绑 0.0.0.0**；token 0600、只经 stdin/文件传，不进 argv；日志不写转录正文、不写 token。
- 原因码日志**不得**包含 tailscale 的原始 stdout/stderr 或任何 IP 以外的外部字符串（防日志注入/泄露）；可以只记固定枚举码。

## Done criteria
- [ ] 调 tailscale CLI 时子进程环境带 `TAILSCALE_BE_CLI=1`（在继承的 os.environ 基础上追加，不清空），对 launchd 的无 SHLVL 环境有效；测试覆盖「env 里确实带了这个变量」
- [ ] `resolve_tailscale_ipv4` / `_tcp_loop`：失败原因以固定枚举码记录，至少区分 `tailscale_cli_missing`（FileNotFoundError）、`tailscale_cli_failed`（非 0 退出）、`tailscale_cli_timeout`、`tailscale_no_ipv4`（空输出）、`tailscale_not_ip`（首行不是 IP，正是 GUI 模式这次的形态）、`not_tailscale_range`、`bind_failed`（OSError，如端口占用）；日志形如 `tcp_bind_failed attempt=N reason=<code> retry_in_s=…`，不含原始输出
- [ ] validate_host 的安全语义不变（仍拒通配/组播/非 100.64/10/带空白）
- [ ] 回归测试：注入一个模拟 GUI 模式的 runner（stdout=`The Tailscale GUI failed to start: ...`、rc=0）→ 记 `reason=tailscale_not_ip` 且不绑任何地址；日志里不出现该原始文本
- [ ] install_node.sh：
  - 预检 `tailscale ip -4` 同样带 `TAILSCALE_BE_CLI=1`，且校验首行是 100.64.0.0/10 的 IPv4，否则报错退出
  - 第 10 步在 UDS 自检通过后，**有界等待**（新增 `RS_TCP_WAIT`，默认 30 秒）确认 gateway 已在 `<Tailscale IP>:8791` LISTEN（例如 `lsof -nP -a -p <gateway pid> -iTCP@<ip>:8791 -sTCP:LISTEN`，或等价手段）；同时确认 8791 上**没有** `*`/0.0.0.0 监听；不满足 → 走现有 rollback（含重拉 v1）
  - 不把 token 放进任何 argv
  - `--dry-run` 计划里第 10 步文案同步
- [ ] tests/test_install_node.py 覆盖：TCP 未监听 → 回滚；监听在正确 IP → 成功；预检拿到非 IP 首行 → 失败
- [ ] 全量 pytest、ruff、`bash -n` + shellcheck（若本机有）通过

## 验证命令
```bash
cd ~/projects/rs-tk-002b
QT_QPA_PLATFORM=offscreen venv/bin/python -m pytest -q tests/test_node_gateway.py tests/test_install_node.py
QT_QPA_PLATFORM=offscreen venv/bin/python -m pytest -q
venv/bin/ruff check .
bash -n scripts/node/install_node.sh && (command -v shellcheck && shellcheck scripts/node/install_node.sh || true)
```

## 状态历史
- 2026-10-09 15:00 CEST: planned → executing（根因见 Context；安全类，单独一批）
