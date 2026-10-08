# TK-003 — 一键安装 install_node.sh

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
用 `scripts/node/install_node.sh <ssh-host>` 或 `--local` 一条命令，把节点装到目标 Mac，配好 LaunchAgent 自启，生成节点 token，并把这个节点写进客户端的 nodes.json。脚本要幂等、能回滚。

## Context
- 先读 RFC「架构选择」第 6 条和 S5、S6、P6。
- 现有可用的部分：`scripts/macos/install.sh --skip-models`（会建 venv、装依赖、构建 rstranslate）。
- mini2 的 `~/projects` 是一个悬空的软链接（指向没挂载的外置盘），必须装到内置盘 `~/rs-node`。
- mini2 上现有的 v1 部署 `~/rs-remote` 只能停掉，不能删除。
- macOS 自带 bash 3.2，不要展开空数组（第 7 节第 9 条）。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - scripts/node/install_node.sh
  - scripts/node/com.realtimesubtitle.node.plist.template
- 禁止修改：
  - scripts/macos/**（只能调用，不能改）
  - realtime_subtitle/**
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] preflight：检查磁盘、uv、swift、Tailscale；主机名用白名单 `^[A-Za-z0-9._@-]+$` 校验，ssh 调用时加 `--`。
- [ ] 部署：先 bundle 到 `~/rs-node.new`，跑安装和自检（调 /v1/info 并测 rtf），通过后原子换名，旧版本留作 `.prev`；失败就回滚到 `.prev` 并告警。
- [ ] token：每个节点单独一份，0600，只经 stdin 传递，不出现在命令行参数或日志里；已有就沿用。
- [ ] plist 里不写死 IP；用 `launchctl bootstrap gui/$UID` 和 `kickstart`。
- [ ] 客户端 nodes.json 原子写入、按 id 去重，地址用 MagicDNS 名。
- [ ] 检查翻译语言包：不是 installed 就打印需要用户在界面上操作的步骤。
- [ ] 支持 `--dry-run`；连续跑两次结果一致（幂等）；`bash -n` 和 shellcheck（如果装了）都通过。
- [ ] 真机：Coordinator 在获得用户批准后对 mini2 执行一次。

## 验证命令
```bash
bash -n scripts/node/install_node.sh
command -v shellcheck && shellcheck scripts/node/install_node.sh
bash scripts/node/install_node.sh --dry-run mini2
```

## 状态历史
- 2026-10-08 CST: planned
