# v3.2.0 Mac 自动验收结果

## 验收范围
本次验收针对 `realtime_subtitle` 仓库的 `feat/macos-native` 分支，验证 Mac 端 rslite 客户端在 v3.2.0 版本中的功能完整性。

## 检查项对应关系

### Gates (Done 7)
- **pytest**: `~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/tmp/rs-accept-pytest`
  - 对应 Done 7：pytest 0 failed
- **ruff**: `~/projects/rs-mac-native/venv/bin/ruff check .`
  - 对应 Done 7：ruff 0 errors
- **swift build**: `(cd macos-native && swift build -c release)`
  - 对应 Done 7：swift build 退出码 0
- **rslite --selftest**: `macos-native/.build/release/rslite --selftest`
  - 对应 Done 7：输出包含 `SELFTEST OK`
- **设计令牌检查**: `python3 scripts/design/gen_tokens.py --check`
  - 对应 CI 中 rslite-macos job 的设计令牌生成物同步

### Documentation (Done 8)
- **version.py 检查**: `grep '__version__ = "3.2.0"' realtime_subtitle/version.py`
  - 对应 Done 8：`version.py` = 3.2.0
- **README 文件检查**: 对 `README.md`、`README.en.md`、`README.de.md` 进行以下检查：
  - Groq 提及：对应 Done 8：README 提到 Groq
  - 开关方法：对应 Done 8：README 提到关闭云端的方法（`--cloud off`）
  - 隐私/数据保留：对应 Done 8：README 包含隐私承诺和数据保留条款
  - key 配置方法：对应 Done 8：README 提到 key 配置方法（`--set-groq-key`）

### Fallback 测试 (Done 4 的可自动化子集)
所有测试使用以下公共参数：
- `--mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless`
- 事件流输出到 `$OUT/<场景>_events.jsonl`
- 错误日志输出到 `$OUT/<场景>.stderr.log`
- 使用 `perl -e 'alarm'` 或后台+kill 实现 240s 超时
- 评分使用：`python3 scripts/bench/hybrid_score.py <事件文件> --refs $HOME/projects/rs-mac-native-data/refs.jsonl --json > $OUT/<场景>/score.json`

#### 1. bad-key 场景
- **额外步骤**: 使用随机生成的假 key (`gsk_<40个随机字母数字>`) 通过 `--set-groq-key` 设置
- **额外参数**: `--cloud on`
- **节点配置**: 复制真实 `~/.config/rslite/nodes.json` 到假ホーム目录
- **判据来源**:
  - stderr 日志模式: `mark_key_invalid` 或 `disabled_invalid_key`（源码位置：`macos-native/Sources/rslite/CloudUploadController.swift` 中的状态机处理 401/403 错误）
  - 事件流: 应包含节点精修结果（表明降级到节点成功）
  - 评分要求: `hybrid_score.py` 输出的 JSON 中 `problem_counts.duplicates = 0` 且 `problem_counts.dropped = 0`（无重复、无丢句）
  - 额外检查: 假 key 字符串不得出现在 stdout/stderr 任何位置（源码位置：`macos-native/Sources/rslite/CloudConfigStore.swift` 中的 `isValidKeyShape` 和 `writeKey` 函数确保 key 不会被记录到日志）

#### 2. cloud-off 场景
- **额外参数**: `--cloud off`
- **节点配置**: 复制真实 `~/.config/rslite/nodes.json` 到假ホーム目录
- **判据来源**:
  - stderr 日志模式: `disabled` 或 `disabled_switch_off`（源码位置：`macos-native/Sources/rslite/HybridEngine.swift` 中的 `activateCloudController` 和 `applyCloudEffect` 函数处理开关关闭）
  - 事件流: 应包含节点精修结果（表明降级到节点成功）
  - 评分要求: `hybrid_score.py` 输出的 JSON 中 `problem_counts.duplicates = 0` 且 `problem_counts.dropped = 0`

#### 3. node-down 场景
- **额外步骤**: 使用假 key（使云端不可用），且假ホーム目录中不包含 `nodes.json`（模拟节点离线）
- **判据来源**:
  - stderr 日志模式: `no_available_node` 或 `consecutive_failure_backoff`（源码位置：`macos-native/Sources/rslite/NodeRouter.swift` 中的路由决策逻辑和 `HybridEngine.swift` 中的降级处理）
  - 事件流: 应包含本机识别结果（表明降级到本机 B）
  - 评分要求: `hybrid_score.py` 输出的 JSON 中 `problem_counts.duplicates = 0` 且 `problem_counts.dropped = 0`
  - 额外检查: 应有 final 事件表明本机识别正常工作

## 自动化限制说明
以下检查项无法完全自动化，需要人工验证或特定条件：

1. **人工操作场景**：断网测试（Done 4 场景 5）需要人工在场关闭 Wi-Fi / 拔网线
2. **长测场景**：连续 1 小时额度测试（Done 6）需要专用设备和长时间运行
3. **交互式验证**：某些 GUI 行为和状态行显示需要人工观察
4. **跨机器测试**：Windows 客户端和节点交互测试（Done 9-14）需要额外设备
5. **性能基准**：识别准确率（Done 2）和延迟（Done 3）虽然可以自动测量，但需要特定音频素材和安静环境以获得可重复结果

## 验收结论
自动化验收脚本覆盖了 Done 7、8 和 Done 4 中可自动化的三个子场景（bad-key、cloud-off、node-down）。通过这些检查可以验证：
- 代码质量门禁满足要求
- 文档正确说明了隐私承诺和配置方法
- 降级机制在各种故障场景下能正常工作，且字幕不断、不重复、不丢句
- 关键安全机制（key 不泄露）得到体现

对于无法自动化的检查项，需要根据 `.governance/sprints-002-cloud-asr-first/acceptance/plan.md` 中的详细步骤进行人工验证。
