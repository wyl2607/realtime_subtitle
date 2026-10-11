# TK-001 — 探针：rslite 词时间分段精度/延迟 + Groq 429 样本

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
量化 SpeechTranscriber volatile/final 结果的词边界精度与延迟，判定 rslite 能否用「词时间驱动分段」；拿到 Groq 真实 429 响应样本（头字段、retry-after、窗口语义）。

## Context
- RFC「架构选择 / 分段器两端不同」与 draft §4-1、§4-2。
- TK-009（Sprint 001）已发现 volatile 结果的 audioTimeRange 是「段首→当前」的宽区间；final 晚 2–4s。
- 素材：`~/projects/rs-mac-native-data/` 的 67 秒 concat5 回放 + FLEURS 60 条（人工转写带句边界）。
- 已有 `macos-native/Sources/rsbench/`（基准工具）可扩展。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - macos-native/Sources/rsbench/**（新增探针子命令）
  - .governance/sprints-002-cloud-asr-first/probes/**（结论文档、原始数据 jsonl）
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] rsbench 新增探针：回放音频 → 记录每个 volatile / final 结果的词级 audioTimeRange 与到达时刻（样本时钟）
- [ ] 输出：词时间分段得到的段边界 vs 人工句边界（或 Silero 段边界）的误差分布（p50/p90），以及段尾判定延迟分布
- [ ] 判定写进 `probes/segmentation.md`：p90 边界误差 ≤ 0.3s 且段尾判定延迟中位数 ≤ 1.0s → 采用词时间分段；否则采用 A3'（能量 VAD + final 校正），并给出 A3' 的阈值建议
- [ ] Groq 429 样本：用一次刻意超分钟限额（>20 次/分，最短 1s 音频）的小测拿 1 份 429 原始响应头（**key 打码**）写进 `probes/groq-429.md`，回答：有无 retry-after、x-ratelimit-* 各字段、音频秒窗口是滚动还是整点
- [ ] swift build 通过

## 验证命令
```bash
cd macos-native && swift build -c release && .build/release/rsbench --help
```

## 执行建议
codex 写探针代码；真机测量（SpeechTranscriber 需要桌面会话）与 Groq 小测由 Coordinator 自己跑（key 在 Coordinator 手里）。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
- 2026-10-11 16:38 CST: executing（批次 1）
