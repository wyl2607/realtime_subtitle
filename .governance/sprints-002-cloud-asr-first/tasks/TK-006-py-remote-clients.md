# TK-006 — Python remote/：node_client + router + line_store + cloud_client + quota

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
Windows 桌面程序具备与 rslite 同构的远端能力：节点 WS v2 客户端、云端 Groq 客户端与记账、分级路由、P5 替换。

## Context
- RFC「架构选择」「安全决策」「云端请求」；规则层用 TK-002 的 `routing_rules.py`；句子时间用 TK-005。
- 移植参考：rslite NodeClient（ready 前缓冲、1013/busy）、NodeRouter、LineStore；节点侧 `realtime_subtitle/node/segmenter.py` 直接复用。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - realtime_subtitle/remote/**（除 routing_rules.py 外）
  - requirements.txt（加 websockets，需过 deps-resolve 3.10–3.13）
  - tests/test_remote_*.py
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] node_client：协议 v2；token 只读文件；URL 解析结果必须在 100.64.0.0/10 否则拒连；ready 前有界缓冲；不走代理
- [ ] cloud_client：requests、**禁 verify=False**（源码扫描测试）、超时 5/10、取消在飞请求、日志不含 key（注入假 key 测试）
- [ ] quota：与 TK-003 同规则，同读 P9 夹具
- [ ] router：分级 + 层内打分 + MigrationGate + 不连自己（node_id 判据，读 `%LOCALAPPDATA%\rs-node\node_id`）
- [ ] line_store：P5 夹具全过
- [ ] 全量 pytest 0 failed、ruff 绿；deps-resolve 本地循环通过

## 验证命令
```bash
~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/rsb tests/test_remote_*.py tests/test_p9_*.py && ~/projects/rs-mac-native/venv/bin/ruff check realtime_subtitle/remote
```

## 执行建议
预计 split：TK-006A（node_client/router/line_store，codex）与 TK-006B（cloud_client/quota/segmenter 复用，免费 lane + Coordinator security 审）。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
