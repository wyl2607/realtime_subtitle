# TK-005b — rslite：本机节点经 UDS 发现与连接（P6/P8）

## Goal
rslite 运行在节点机本机（如 mini2 自己开 rslite）时，经本机 UDS（`~/Library/Application Support/rs-node/gw.sock`）发现并使用本机 gateway：hw_hash 与本机一致就走 UDS、不需要 token、不经网络；P8 本机档门槛与 localUnderP8Penalty / noNetworkBonus 真正生效。

## Context
- 来源：CR-009 Round 3 R3-3（opus 终审）：NodeRouter.swift:262 `isLocal` 恒为 false，P6 与 TK-005 Context「本机节点通过 UDS 发现：hw_hash 和本机一致就走 UDS，不需要 token」未实现。
- RFC Done criteria 4「在 mini2 本机运行时走 UDS」依赖本项；TK-007 验收前必须完成。
- 服务端：gateway 已支持 UDS（LOCAL_PEERCRED 校验同 uid，/v1/info 与 /v2/session 均可走 UDS，无需 token）。
- 依赖 TK-005 合并后的 NodeClient/NodeRouter。

## Constraints
- write_scope：macos-native/Sources/rslite/{NodeClient,NodeRouter,Capability,main}.swift、macos-native/README.md
- RFC P1–P8 冻结；S1–S8 有效（UDS 路径不发 token；socket 路径防 symlink；日志无正文无 token）
- 不加需求外旋钮

## Done criteria
- [ ] 启动与 30s 探测时检查本机 gw.sock：存在且是 socket、属主为当前用户 → 经 UDS 请求 /v1/info，hw_hash 与本机 IOPlatformUUID 推出的值一致 → 作为 isLocal=true 的候选节点（不需要 nodes.json 条目、不发 token）
- [ ] 会话经 UDS 建立（URLSession 不支持 UDS 时说明替代实现）
- [ ] P7 打分里本机节点的 localUnderP8Penalty / noNetworkBonus 生效；selftest 覆盖「本机节点 + P8 不满足」「本机节点 + 远端更优」
- [ ] 冒烟：在 MacBook 上起本机 gateway（UDS）+ 假 worker，rslite --mode auto 选中本机节点、全程无 TCP、无 token
- [ ] swift build、--selftest、TK-005 原冒烟 18/18 不回归

## 状态历史
- 2026-10-09 18:35 CEST: planned（CR-009 R3-3 拆出）
