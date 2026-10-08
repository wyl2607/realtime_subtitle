# TK-007 — 端到端验收 + 五组功耗 + 结果文档

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
按 RFC 的 Done criteria 1–8 做真机验收；把 power_compare.sh 扩成五组（空闲、B、C、A、混合）并实测；结果写进 docs/node-acceptance.md。

## Context
- 由 Coordinator 主导。所有对 mini2 的远程操作（安装、停止 v1、kill worker 做故障测试）都要先获得用户批准。
- 第二个节点用本机模拟：在 MacBook 上用 `--local` 装一个节点，或者在另一个端口起一个 gateway。
- 功耗测量需要用户用 sudo 执行；测之前要停掉 A 版和浏览器里的视频。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - scripts/bench/**
  - docs/node-acceptance.md
- 禁止修改：
  - 产品代码（发现问题就回到对应的 TK 修）
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] Done criteria 1–8 逐条记录证据：命令、输出摘要、数字。
- [ ] 五组功耗的数据，以及「噪声有多大」的说明。
- [ ] 混合档在 67s 回放中的「最终文本」WER、首字时间、精修延迟中位数，都达到 RFC 的要求。
- [ ] 失败或不达标的项：要么回到对应 TK 修，要么在文档里写明原因和后续计划。

## 验证命令
```bash
sudo bash scripts/bench/power_compare.sh ~/projects/rs-mac-native-data/concat5.wav   # 用户执行
```

## 状态历史
- 2026-10-08 CST: planned
