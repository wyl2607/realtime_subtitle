# TK-005 — rslite：NodeClient v2 + NodeRouter + 本机档门槛

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
实现 v2 客户端（NodeClient）、路由器（NodeRouter：读 P6 节点清单、探测、P7 打分、迁移、故障回退、node_id 校验）、本机档门槛（P8），以及 `--mode auto|local|hybrid`。删除 RemotePipeline.swift。

## Context
- 先读 RFC 的 P1、P2、P6、P7、P8。
- 依赖 TK-004 提供的 AudioFanout 和带时间的句子契约。
- v1 客户端 RemotePipeline.swift 里有实测验证过的两个经验，要搬过来：URLSession 把 1013 报成 1005，真正的 code 在 reason 的前两个字节；关闭时要等 didCloseWith 回调才能保证真的发出 close 1000。
- 本机节点通过 UDS 发现：hw_hash 和本机一致就走 UDS，不需要 token。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - macos-native/Sources/rslite/NodeClient.swift, NodeRouter.swift, HybridEngine.swift, Capability.swift, main.swift
  - 删除 macos-native/Sources/rslite/RemotePipeline.swift
  - macos-native/README.md
- 禁止修改：
  - Capture/**, AudioSource.swift, Pipeline.swift, Overlay.swift, AudioFanout.swift（归 TK-004；需要改的话提给 Coordinator）
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] NodeClient 实现 P2；先核对 node_id，不一致就不发 token 和音频（S5）；每个节点用自己的 token。
- [ ] NodeRouter：P6 两个文件都原子写、权限 0600；每 30s 探测一次；P7 打分，权重集中在一张常量表；连续 2 次领先 margin 才迁移；在静音点 drain 旧会话，最多等 3s；故障时标记 offline_until=60s 并回退到次优节点，再不行就回到 B；每次选择都打一行日志说明理由，日志里不带正文。
- [ ] P8 门槛：芯片、内存、电源由 Capability 判定；MacBook Air M2 判定为不满足。
- [ ] 打分和迁移决策写成可测的纯逻辑，至少覆盖：更优节点上线、节点下线、节点 busy、node_id 不匹配、电池供电这几种情况。
- [ ] 界面显示「本机」「混合·<节点>」「精修中」。
- [ ] Swift release 构建通过；用一个假的 v2 节点做 headless 冒烟。

## 验证命令
```bash
cd macos-native && swift build -c release --product rslite
```

## 状态历史
- 2026-10-08 CST: planned
