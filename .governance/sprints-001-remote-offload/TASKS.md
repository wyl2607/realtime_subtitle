# TASKS — sprints-001-remote-offload

> 状态枚举：`planned | executing | reviewing | fixing | recheck | done | blocked | split`
> 每次切换 phase 时由 Coordinator 同步本表。契约统一以 RFC 的 P1–P8 为准。

| id | title | status | blocked_by | write_scope | reviewer_lanes | notes |
|----|-------|--------|------------|-------------|----------------|-------|
| TK-001 | 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译 | done | — | realtime_subtitle/node/{worker,segmenter,engines}.py, tests/test_node_worker.py, tests/test_node_segmenter.py | default,security | 涉及 S1、S7；CR-002 四轮收敛，05:55 合并；07:05 Mac 回归冒烟通过（WER 0.88%、延迟中位 3.40s）；node/__init__.py（空文件）随本 TK 建包，CR-002 F6 登记；**后续小项**：P1 rtf 每次会话结束滑动更新 asr_state.json（临时文件+rename），CR-003 定归属，单独一批、在 TK-003 前完成 |
| TK-001b | worker：会话结束滑动更新 asr_state.json 的 rtf（P1） | executing | TK-002（info.STATE_DIR 已合并） | realtime_subtitle/node/worker.py, tests/test_node_worker.py | default | CR-003 归属决定：worker 手里有 seg_s/asr_s；临时文件+rename 原子写；只写数字字段，不含正文（S7）；gateway 只读 |
| TK-002 | 节点 gateway：生命周期 + 协议 v2 + 鉴权与上限 | done | TK-001（只依赖 P3 契约，可以对照假 worker 并行写） | realtime_subtitle/node/{gateway,info,__init__}.py, realtime_subtitle/node/requirements.txt, tests/test_node_gateway.py | default,security | 涉及 S2/S3/S4/S8；鉴权和状态机，需准备第 3 轮评审；须并发读 worker stdout（worker 持锁写管道，CR-002 round2 记）；云端执行：tailscale/ioreg/IOPS 走可注入接口，RSS<50MB 待 Mac 实测 |
| TK-003 | 一键安装 install_node.sh | planned | TK-002（只用于验收联调） | scripts/node/** | default,security | 涉及 F04、S5、S6；须生成 node_id 文件（0600、原子写，路径对齐 info.STATE_DIR）与 asr_state.json 初值（安装自检测 rtf）；token ≥32 字符、文件 0600（gateway 启动时会校验，CR-003 S-F6） |
| TK-004 | rslite：带时间句子 + AudioFanout + 混合替换 + 单实例 | done | — | macos-native/Sources/rslite/{Pipeline,Overlay,AudioFanout,SingleInstance,SentenceCommitter}.swift | default | 涉及 F01 和 P4/P5；云端只能代码复查，合并前须回 Mac swift build + headless 回放；07:10 Mac 构建+回放通过并合并；深层验收项转 TK-005/TK-007（见 CR-001.md） |
| TK-005 | rslite：NodeClient v2 + NodeRouter + 本机档门槛 | planned | TK-004 | macos-native/Sources/rslite/{NodeClient,NodeRouter,HybridEngine,Capability,main,RemotePipeline}.swift, macos-native/README.md | default,security | 涉及 F02、F03、S5 和 P6–P8；状态机，需准备第 3 轮评审；**另含 CR-001 F5：单实例接线进 main，并验收「第二个实例提示后以非 0 退出」，headless 的 file: 来源可以跳过**；若 NodeRouter 让 AudioFanout.start/stop 并发调用，须一并修 CR-001 M1（加锁前 source.start 的漏停窗口）并更正 AudioFanout.swift:94 注释 |
| TK-006 | 清理 v1 + 文档 | planned | TK-001, TK-002, TK-005 | realtime_subtitle/remote/**, tests/test_remote_server.py, CLAUDE.md, docs/protocol-v2.md | default | v1 删除时 RemotePipeline.swift 由 TK-005 负责删；protocol-v2.md 须写明：会话中 worker 退出（含 0）→ worker_crashed，status code 映射 2/3/4 与 audio_dropped/backlog_dropped（CR-002/CR-003） |
| TK-007 | 端到端验收 + 五组功耗 + 结果文档 | planned | TK-001…TK-006 | scripts/bench/**, docs/node-acceptance.md | default | 涉及 F05；mini2 远程操作要先征得用户批准 |

## 字段说明
- **write_scope**：路径前缀白名单，Executor 只能写其中的文件。
- **reviewer_lanes**：`default` / `architecture` / `security` / `perf` 可任意组合，决定要派几个 Reviewer。
- **blocked_by**：上游依赖的 TK id 列表。
- **notes**：split 时记录子 TK 的 id；blocked 时记录原因。
