# 结果

## 结论

partial：G2 修复和文件内回归自检已完成；标准 `swift build -c release` 在受管 sandbox 中被宿主的 `sandbox-exec` 权限限制阻断，未能以原命令完成。

## 改动摘要

- `macos-native/Sources/rslite/QuotaLedger.swift`
  - 墙钟回拨到未来 `dayEpoch` 之前时，将该锚点按 `-∞` 处理，日请求事件与服务端校正继续 fail-closed 计入。
  - `pruned` 保留未来锚点，墙钟追上后恢复正常过滤。
  - 新增重启后错误前跳、消费锚点、墙钟回拨、墙钟追上四步回归用例。

建议提交信息：

```text
fix(rslite): ignore future day epoch during clock rollback

Co-Authored-By: Codex <noreply@openai.com>
```

## 验证

- 红：新增用例在修复前输出 `SELFTEST FAIL QuotaLedger: 未来 dayEpoch 在墙钟回拨期间必须忽略，保留本地事件和 remaining=0 校正`。
- 绿：`cd macos-native && swift build -c release --disable-sandbox -debug-info-format none` 成功（不生成 dSYM 是受管环境对 `dsymutil` 的限制规避）。
- 绿：`RSLITE_CONFIG_DIR=$(mktemp -d ...) .build/release/rslite --selftest` 输出 `SELFTEST OK`；临时配置目录为空并已删除。
- 阻断：标准 `cd macos-native && swift build -c release` 的 manifest 阶段报 `sandbox-exec: sandbox_apply: Operation not permitted`；即使加 `--disable-sandbox`，默认 dSYM 阶段仍因 `dsymutil` 报 `Operation not permitted`。

## PR 草稿

标题：`fix(rslite): ignore future day epoch during clock rollback`

```markdown
## Summary
- Treat a persisted future `dayEpoch` as inactive while the wall clock is behind it.
- Preserve the anchor so normal RPD filtering resumes once the wall clock catches up.
- Add a self-test for restart, false forward jump, rollback, and recovery.

## Validation
- `swift build -c release --disable-sandbox -debug-info-format none`
- `RSLITE_CONFIG_DIR=$(mktemp -d ...) .build/release/rslite --selftest`
```

## 风险 / 待决定事项

- 在普通开发机或 CI 仍应运行未附加参数的 `swift build -c release`，确认 dSYM 生成路径；本工作树未访问真实配置目录、未启动 GUI、未触及生产环境。
