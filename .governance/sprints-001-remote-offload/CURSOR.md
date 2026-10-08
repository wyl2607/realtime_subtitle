# CURSOR — sprints-001-remote-offload

## 当前状态
<!-- Machine fields: new writes must use "- key: `value`". Legacy variants are read-only compatibility. -->
- current_phase: `scheduler`
- current_tk: `—`
- current_cr: `—`
- current_round: `—`
- escalated: `false`

## 进度快照

| TK | 标题 | 状态 |
|----|------|------|
| TK-001 | 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译 | planned |
| TK-002 | 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限 | planned |
| TK-003 | 一键安装 install_node.sh | planned |
| TK-004 | rslite：带时间句子 + AudioFanout + 混合替换 + 单实例 | planned |
| TK-005 | rslite：NodeClient v2 + NodeRouter + 本机档门槛 | planned |
| TK-006 | 清理 v1 + 文档 | planned |
| TK-007 | 端到端验收 + 五组功耗 + 结果文档 | planned |

## 最近动作
- 2026-10-09 04:05 CST: Sprint 001 initialized, awaiting TK-001（用户已批准全部 TK 排队执行，含 TK-007 对 mini2 的远程操作）

## 下一步
- /sprint-tick：第一批并行派 TK-001、TK-002、TK-003、TK-004；TK-005 等 TK-004；TK-006、TK-007 等前面全部完成
