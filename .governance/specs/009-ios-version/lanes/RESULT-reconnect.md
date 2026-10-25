# 重连策略实现结果

## 实现内容

### 新增文件
1. `ios/RSBroadcast/ReconnectPolicy.swift` - 纯逻辑重连策略结构体
2. `ios/Tests/ReconnectSelftest.swift` - 自测函数

### 修改文件
1. `ios/RSBroadcast/SampleHandler.swift` - 集成重连逻辑
2. `ios/Tests/NodeSelftest.swift` - 调用 `runReconnectSelftest()`
3. `ios/Tests/run-selftest.sh` - 编译命令加入新文件

## 核心逻辑

### ReconnectPolicy 行为
| 关闭码 | 行为 | 延迟 |
|--------|------|------|
| 1008, 1003, 1009 | 不重连 (协议/配置错误) | `nil` |
| 1013 (busy) | 固定重连 | 10 秒 |
| 1000, 1006, 1011 等 | 指数退避 | 1, 2, 4, 8, 16, 30, 30... 秒 |
| `NodeEvent.isFatal` 对应状态 | 由调用方判断，不重连 | `nil` |

### SampleHandler 集成
- 仅 `engine_mode == "node"` 时生效
- 关闭后按 policy 延迟，在 `connectionQueue` 上重新走 `startPinnedSession`（含 TailnetPin 校验）
- 广播已结束 (`!broadcastActive`) 则不重连
- 重连前把 `readyReceived` 置 false（ready 之前的帧照旧丢弃计入 drop）
- 收到 `ready` 事件后调用 `reconnectPolicy.reset()`
- 状态行加 `reconn=<累计重连次数>`，`last=` 照旧显示最后关闭码
- 所有共享状态经 `stateQueue` 保护，`processSampleBuffer` 无 sleep/阻塞

## 验收结果

```
bash ios/Tests/build-check.sh
BUILD-CHECK OK
```

```
bash ios/Tests/run-selftest.sh
SELFTEST OK
```

自测覆盖：
- 退避序列 1,2,4,8,16,30,30 (1006)
- reset 后从 1 开始
- 1013 → 固定 10 秒
- 1008/1003/1009 → nil