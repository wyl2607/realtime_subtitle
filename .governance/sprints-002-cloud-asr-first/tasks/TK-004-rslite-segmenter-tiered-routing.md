# TK-004 — rslite CloudSegmenter + 分级路由接入 HybridEngine

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
rslite 在有 key 时默认走云端：按 TK-001 结论分段上传，结果经 P5 替换本机字幕；按 P7' 在云端/节点/本机之间静音点切换；状态行显示当前层。

## Context
- TK-001 结论决定分段方案（词时间分段 or A3'）。TK-002 Provider/状态机、TK-003 CloudClient/QuotaLedger。
- 现有 NodeRouter（RoutingDecider、MigrationGate、不连自己）与 HybridEngine（本机先出、节点替换）。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - macos-native/Sources/rslite/CloudSegmenter.swift（环形缓冲 + 分段）
  - macos-native/Sources/rslite/NodeRouter.swift、HybridEngine.swift、Overlay.swift（状态行）
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] 分段：静音 ≥0.6s 收段、≤15s 强切、<1.5s 合并规则；音频按 [a0−0.2, a1+0.2] 截取；时间 = 样本时钟
- [ ] 分级选择走 Provider 抽象，云端不进 nodes.json；层间切换在静音点；云端→节点期间本机字幕兜底
- [ ] 单段失败保留本机版不重发；连续 3 次降级；恢复后静音点切回
- [ ] 开关关立即取消在飞请求、丢弃响应
- [ ] 状态行「云端·Groq」/「混合·节点名」/「本机」
- [ ] selftest 跑 P9 夹具 + 新增的分段单测；swift build 绿
- [ ] （Coordinator 真机）67 秒回放：WER ≤ 2.63%、段尾→上屏 p50 ≤ 2s、无重复

## 验证命令
```bash
cd macos-native && swift build -c release && .build/release/rslite --selftest
```

## 执行建议
codex（high）；可能 split 为 A 分段器 / B 路由接入。真机由 Coordinator 跑。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
- 2026-10-11 17:59 CST: executing（批次 4）
