# TK-008 — 文档 + v3.2.0 + Mac/Windows 端到端验收

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
README 三语与 CLAUDE.md 隐私承诺改写、云端与节点使用说明、v3.2.0 版本号；RFC Done criteria 1–15 逐条真机验证并留证据。

## Context
- requirement 002 §3 隐私条与 10-11 改定第 4 条：承诺改为「默认完全本地；只有配置了云端 key 才会把音频发送到云端识别服务」，三语一致。
- draft §4-8：Groq 数据保留政策要查官方条款，不凭记忆写。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - README.md、README.*.md、CLAUDE.md、docs/**（含 docs/zh/user-guide-template.txt、docs/protocol-v2.md）、version.py
  - .governance/sprints-002-cloud-asr-first/acceptance/**（验收记录，key 打码）
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] README 三语：隐私承诺改写、云端开关/key 配置/代理可见性说明、Groq 数据政策（附官方链接）、Windows 使用算力节点一节
- [ ] CLAUDE.md 开头承诺 + 第 4/7 节新增避坑条目；用户手册模板同步；`version.py` 3.2.0 + 理由注释
- [ ] 验收（Coordinator 真机）：RFC Done 1–15 每条一行证据（命令/日志片段/数字），全部写进 acceptance/
- [ ] 全量 pytest（Mac + Windows）0 failed；swift build；ruff

## 验证命令
```bash
~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/rsb && ~/projects/rs-mac-native/venv/bin/ruff check . && (cd macos-native && swift build -c release)
```

## 执行建议
文档：inkling/免费 lane 起草 + Coordinator 审（security lens：隐私承诺）；验收 Coordinator 自留。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
- 2026-10-11 19:19 CST: executing（文档部分，批次 6）
