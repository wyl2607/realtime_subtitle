# TK-004 — rslite：带时间句子 + AudioFanout + 混合替换 + 单实例

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
让 B 产出的每一句都带客户端时钟上的 [t0,t1]。用 AudioFanout 把一路采集同时分给 B 和 NodeClient（共用同一个样本时钟）。Overlay 按 P5 规则把本机行替换成节点行。再加上单实例保护。

## Context
- 先读 RFC 的 P4、P5 和「架构选择」第 4、5 条。
- B 侧开启 `attributeOptions:[.audioTimeRange]`，API 以 SDK 的 swiftinterface 为准。
- SentenceCommitter 提前定稿的句子也要带时间（取对应 volatile 结果的 range），算不出时间时传 nil。
- Overlay 现在已经有自动换行、高度自适应、草稿尾部截断，这些都要保留。
- 两个实例会互相删掉对方的 tap 聚合设备，这就是要加单实例保护的原因。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - macos-native/Sources/rslite/Pipeline.swift, Overlay.swift, AudioFanout.swift, SingleInstance.swift, SentenceCommitter.swift
- 禁止修改：
  - macos-native/Sources/rslite/Capture/**, AudioSource.swift
  - NodeClient、NodeRouter、HybridEngine、main（归 TK-005）
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] 回调契约改成 `onFinal(id,text,t0,t1)`；headless 输出的 JSON 里加上 t0 和 t1。
- [ ] AudioFanout：一路采集供两个消费者使用；时钟按样本数推进；某一路消费慢不能阻塞另一路（有界缓冲，满了丢最旧的）。
- [ ] 替换规则 P5：做成可测的纯逻辑（比如放在 rslite 的 `--selftest` 里，或者单独一个函数），覆盖重叠 50% 的边界、多行替换、无匹配时插入、节点行不会被二次替换。
- [ ] 单实例：flock；第二个实例打印提示后退出，退出码非 0。
- [ ] concat5 回放用 headless 跑，本机 B 的行为和延迟没有退化（和 ea5d4af 的结果对比）。
- [ ] Swift release 构建通过。

## 验证命令
```bash
cd macos-native && swift build -c release --product rslite
.build/release/rslite --selftest   # 由本 TK 新增（若采用）
```

## 状态历史
- 2026-10-08 CST: planned
