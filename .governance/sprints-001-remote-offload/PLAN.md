# PLAN — sprints-001-remote-offload

## 目标

做完以后：
- MacBook 打开 rslite，本机省电模型会立刻出字。
- 能连上更强的节点（目前是 mini2，下一期加 Windows 主机）时，会自动用大模型精修，并按音频时间替换掉本机的那几句。
- 节点用一条命令就能装好，平时不占用资源，有会话时才加载模型，用完自动释放。
- 节点那台机器自己当主力机用时，走本机，不绕网络。

## 接续点

接的是这几个已有的东西：

| 提交 | 内容 |
|---|---|
| `56b3628` | rslite 本机 B |
| `000a6ea` | rslite 远程 v1 客户端 |
| `274d19e` | 服务端 v1 |
| `3e9fb47` | 功耗脚本 |

v1 已经部署在 mini2 的 `~/rs-remote`，用的是系统翻译。这个 Sprint 会用节点 v2 替换它。

悬而未决的事情：
- mini2 的自动登录要不要开，由用户决定。
- WhisperKit 推迟到下一期再评估。

## 范围

**包含**
- 节点 worker：VAD 分段，整段识别，再翻译
- 节点 gateway：生命周期管理、协议 v2、鉴权和资源上限
- 一键安装和回滚
- rslite 端：
  - 带时间的句子契约，单实例保护
  - 混合档的替换逻辑
  - NodeClient 和 NodeRouter：路由、迁移、故障回退
- 清理 v1 并补文档
- 五组功耗对比和端到端验收

**明确不做**
- Windows 节点和 Windows 客户端的实现
- 多客户端并发
- 公网访问、TLS
- 打包、签名、公证
- WhisperKit
- 服务端流式模式（`mode:"stream"`）
- 改动 A 版

## 车道（并行 lanes）

| lane | TKs | write_scope 摘要 |
|------|-----|-----------------|
| L-A 节点 Python | TK-001 → TK-002 | `realtime_subtitle/node/**`, `tests/test_node_*.py` |
| L-B 安装 | TK-003 | `scripts/node/**` |
| L-C 客户端 Swift | TK-004 → TK-005 | `macos-native/Sources/rslite/**`, `macos-native/README.md` |
| L-D 收尾 | TK-006 → TK-007 | v1 删除、文档、`scripts/bench/**`、`docs/**`、`CLAUDE.md` |

- 协议和接口都已经在 RFC 的 P1–P8 里定死，所以 L-A、L-B、L-C 可以并行开工，各自对照契约写代码。
- 联调和验收放在 TK-007。

## Checklist（与 TK done 状态保持一致）
- [ ] TK-001 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译
- [ ] TK-002 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限
- [ ] TK-003 一键安装 install_node.sh（preflight/原子换名/回滚/幂等/--local）
- [ ] TK-004 rslite：带时间句子契约 + AudioFanout + 混合替换 Overlay + 单实例
- [ ] TK-005 rslite：NodeClient v2 + NodeRouter（打分/迁移/故障回退/node_id 校验）+ 本机档门槛
- [ ] TK-006 清理 v1 + 文档（CLAUDE.md §7、README、docs/protocol-v2.md）
- [ ] TK-007 端到端验收 + 五组功耗 + 结果文档

## 退出条件
- 所有 TK 都处于 done 状态。
- 所有 CR 都已关闭，没有待处理的 finding。
- RFC 里的 Done criteria 1–8 全部验证通过，结果记入 `docs/`。
- Experience 子 Agent 已提炼出候选规则。
- 没有未解释的失败模式。
