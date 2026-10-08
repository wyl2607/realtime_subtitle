# TK-006 — 清理 v1 + 文档

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
删除协议 v1 的服务端和测试，补齐文档：CLAUDE.md 第 7 节的新避坑条目，以及 docs/protocol-v2.md（写明和 v1 的差异，P1–P3）。

## Context
- 新避坑条目来源：本 Sprint 中各 TK 的评审结论，以及之前已经证实过的坑。
  - MLX 必须单线程执行；
  - rslite 的 main 必须同步；
  - URLSession 把 1013 报成 1005；
  - mini2 的 `~/projects` 是悬空链接；
  - LaunchAgent 依赖用户已登录；
  - 两个 rslite 实例会互相删 tap 设备；
  - 测 B 时不能同时开着 A；
  - 等等。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - 删除 realtime_subtitle/remote/**, tests/test_remote_server.py
  - CLAUDE.md, docs/protocol-v2.md
- 禁止修改：
  - 其他代码
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] v1 的代码和测试已删除，全量测试通过，仓库里没有残留的引用（用 rg 检查 remote.server 和 8790）。
- [ ] CLAUDE.md 第 7 节新增条目，每条都是有实测依据的坑，并写明日期。
- [ ] docs/protocol-v2.md 涵盖 P1–P3、和 v1 的差异、隐私边界。

## 验证命令
```bash
QT_QPA_PLATFORM=offscreen ~/projects/rs-mac-venv/bin/python -m pytest -q
~/projects/rs-mac-venv/bin/ruff check .
rg -n 'realtime_subtitle.remote|remote_server' -g '!*.governance*' || true
```

## 状态历史
- 2026-10-08 CST: planned
