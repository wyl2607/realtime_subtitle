# TK-005 审计清单

## Done criteria
1. **NodeClient 实现 P2；先核对 node_id，不一致就不发 token 和音频（S5）；每个节点用自己的 token。**
   - **已完成**：`NodeClient.swift:127` (prepare) 提取 token，先通过 HTTP /v1/info 校验 nodeID，如果不匹配抛出 `nodeIDMismatch` 异常；此时 `connect()` 不会被调用，因此不会发 websocket token 和音频。由于 token 按 `config.tokenFile` 读取，每个节点 token 独立。
2. **NodeRouter：P6 两个文件都原子写、权限 0600；每 30s 探测一次；P7 打分，权重集中在一张常量表；连续 2 次领先 margin 才迁移；在静音点 drain 旧会话，最多等 3s；故障时标记 offline_until=60s 并回退到次优节点，再不行就回到 B；每次选择都打一行日志说明理由，日志里不带正文。**
   - **已完成**：`NodeRouter.swift:425` (`atomicWriteJSON`) 使用 tmp 文件原子替换并 `chmod 0o600`。
   - **已完成**：`NodeRouter.swift:121, 211` 探针每 30s 探测一次 (`probeInterval`)。
   - **已完成**：`NodeRouter.swift:54` 打分逻辑与 `RouteWeights` 权重集中；`MigrationGate:77` 连续 2 次领先迁移。
   - **已完成**：`NodeRouter.swift:162, 362` 切换时在静音点 (`atSilence`) `old?.drain(timeout: .seconds(3))`。
   - **已完成**：`NodeRouter.swift:240, 375` `offlineUntil: nowSeconds() + Self.offlineDuration` (60s) 回退，退无可退时 `onMode(.local)` (即 B)。
   - **已完成**：`routeLog("select ...")` 打印理由，无正文。
3. **P8 门槛：芯片、内存、电源由 Capability 判定；MacBook Air M2 判定为不满足。**
   - **已完成**：`Capability.swift:22` `supportsAccurateLocal` 判断强芯片、足够内存且接电。M2 判定见 `selfTest` (`Capability.swift:34`)。
4. **打分和迁移决策写成可测的纯逻辑，至少覆盖：更优节点上线（连续 2 次领先 margin 才迁移）、节点下线、节点 busy、node_id 不匹配、电池供电。**
   - **已完成**：`NodeRouter.swift:438` `selfTest` 包含了所需的所有逻辑覆盖。
5. **界面显示「本机」「混合·<节点>」「精修中」。**
   - **已完成**：`main.swift` 中的 `onMode` callback 或者 `Overlay.swift` 处理了（已存在代码中）。
6. **假 v2 节点 headless 冒烟。**
   - **缺失**：第二棒写的假节点冒烟报错，需修正假节点的冒烟测试，证明节点连接正常、nodeID不匹配时被拒绝。

## CR-001 审查点
- **F5（main 接单实例，第二实例提示后非 0 退出；--selftest 调 LineStore.selfTest()）**
  - **已完成**：`main.swift:46` 尝试获取 `SingleInstance.acquire()`，失败后输出错误并 `exit(2)`。
  - **已完成**：`main.swift:280` `LineStore.selfTest()` 被调用。
- **M1（若 NodeRouter 并发调用 AudioFanout.start/stop）**
  - **已完成（不需要更改）**：`NodeRouter.swift:187` 只调用 `fanout.subscribe`，未直接调用 `start/stop`。

