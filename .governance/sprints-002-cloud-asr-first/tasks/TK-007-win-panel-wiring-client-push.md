# TK-007 — Windows 面板 + 接线 + `--client` 下发

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
⚙️ 面板可填 Groq key、开关云端、开关节点并看到当前路由；app 把 remote/ 接进主链路；节点安装脚本可把节点配置下发到 Windows 客户端。

## Context
- RFC「P6'」「架构选择 / 下发」「安全决策 1/5」；005 requirement Done 1–5。
- 模式切换仍只走 `_apply_mode` 单入口（CLAUDE.md §14）；面板控件显示靠 `refresh_from_config()`。
- 下发复用 `scripts/windows/node_setup.ps1` 的 Lock-Down（icacls）；Windows 坑见交接包（GBK、引号、scp .ps1 再 -File）。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - realtime_subtitle/ui/settings_window.py、realtime_subtitle/app.py
  - scripts/node/install_node.sh、scripts/node/install_node_windows.sh、scripts/windows/node_setup.ps1
  - tests/test_settings_*.py、tests/test_install_node*.py
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] 面板：「使用算力节点」默认关（无 nodes.json 时置灰并说明）；「云端识别」密码框 + 开关（有 key 默认开）；当前路由状态
- [ ] key 写 `%APPDATA%\realtime_subtitle\groq.key` 并 icacls 仅当前用户，不进 config_local.py；不读环境变量
- [ ] 老用户更新后两个开关行为 = 3.1.0（无 key、节点关）
- [ ] `--client <ssh 别名>`：nodes.json 条目合并写入、token 经 stdin、ACL 收紧；幂等
- [ ] 全量 pytest 0 failed、ruff 绿

## 验证命令
```bash
~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/rsb && ~/projects/rs-mac-native/venv/bin/ruff check . && bash scripts/node/install_node_windows.sh --help
```

## 执行建议
codex；key 存储/ACL/下发 token 部分由 Coordinator 逐行 security 审，真机下发由 Coordinator 跑。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
