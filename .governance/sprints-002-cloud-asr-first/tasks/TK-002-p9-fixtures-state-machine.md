# TK-002 — P9 夹具 + 云端状态机 + Provider 抽象（双端读同一份夹具）

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
把 RFC P7' 的分级选择与云端状态机、P5 替换规则写成两端共用的 JSON 夹具，并让 Python 与 Swift 各有一份纯逻辑实现读夹具通过。

## Context
- RFC「P7' 分级路由」状态表（ARCH-01）、「P9」、「架构选择 / Provider 抽象」（ARCH-02）。
- Swift 现有：`NodeRouter.swift`（RoutingDecider、MigrationGate）、`SentenceCommitter.swift`/`Overlay.swift`（LineStore，003 版 P5 并集累计覆盖 ≥50%）、`main.swift --selftest`。
- Python 侧目前没有路由代码；本 TK 只写**纯函数**的规则层（无网络、无 Qt），供 TK-006 使用。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - tests/fixtures/route_cases.json、tests/fixtures/p5_cases.json
  - tests/test_p9_route_cases.py、tests/test_p9_p5_cases.py
  - realtime_subtitle/remote/__init__.py、realtime_subtitle/remote/routing_rules.py（纯函数：tier 选择 + 云端状态机 + P5 合并）
  - macos-native/Sources/rslite/Provider.swift（Provider 协议 + CloudState 状态机，纯逻辑）
  - macos-native/Sources/rslite/main.swift 中 --selftest 的夹具读取段（只加不改其它）
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] route_cases.json 覆盖 RFC 状态表每一行（含连续 3 次失败、401、429+retry-after、额度 90%/80% 滞回、试探成功/失败、开关关/开、key 修改），以及「无 key → 跳过 Tier 0」「节点开关关 → 跳过 Tier 1」「不连自己」
- [ ] p5_cases.json 覆盖：远端句覆盖本机句 ≥50% 替换 / <50% 不替换 / 多个远端句并集累计覆盖 / 远端先到本机后到丢弃 / 已滚出屏幕不回滚
- [ ] Python `routing_rules` 与 Swift `Provider.swift` 读同一份夹具全部通过；pytest + `rslite --selftest` 都绿
- [ ] `realtime_subtitle/remote/` 无 001 v1 残留（先 `git log --all -- realtime_subtitle/remote` 确认）
- [ ] ruff 绿；swift build 绿

## 验证命令
```bash
python -m pytest -q tests/test_p9_route_cases.py tests/test_p9_p5_cases.py && ruff check realtime_subtitle/remote tests/test_p9_*.py && cd macos-native && swift build -c release && .build/release/rslite --selftest
```

## 执行建议
codex（high）。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
