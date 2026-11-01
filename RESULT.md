# 结果

## 1. 结论

部分完成。macOS rslite 的可信 RPD 翻日信任门、路由事件接线与自检已实现，隔离配置下 `rslite --selftest` 通过。默认 `swift build -c release` 先被宿主禁止 manifest sandbox；加 `--disable-sandbox` 后源码编译和链接完成，但 `dsymutil` 写入 dSYM 仍被宿主拒绝。关闭 dSYM 后的等价 release 构建通过。

## 2. 改动摘要

- `macos-native/Sources/rslite/QuotaLedger.swift`
  - 新增持久化恢复记录 `reset_at`、`observed_at`、`key_fp`、`consumed`，并兼容旧账本与坏恢复字段。
  - 仅在同响应具备合法 remaining/reset 且有 key 指纹时建立记录；实施 key 绑定、乱序响应拒绝、墙钟回拨和单调钟双重校验。
  - 只在可信翻日时重置 RPD 服务端分量，并一次性发出 `quota_reset_confirmed`。
  - 增加 R1–R10 自检覆盖。
- `macos-native/Sources/rslite/CloudClient.swift`
  - 成功和 HTTP 错误配额元数据携带 SHA-256 key 指纹及实际请求发送时刻；不记录原 key。
- `macos-native/Sources/rslite/CloudSegmenter.swift`
  - 将上述可信上下文传入账本 `note`。

建议提交信息：

```text
feat(rslite): gate RPD reset on trusted rollover

Co-Authored-By: Codex <noreply@openai.com>
```

## 3. 测试

- `cd macos-native && HOME=$(mktemp -d /private/tmp/rslite-home.XXXXXX) swift build -c release --disable-sandbox -debug-info-format none`：通过。
- `cd macos-native && HOME=$(mktemp -d /private/tmp/rslite-home.XXXXXX) RSLITE_CONFIG_DIR=$(mktemp -d /private/tmp/rslite-config.XXXXXX) .build/release/rslite --selftest`：通过，输出含 `SELFTEST OK`。
- 默认质量门 `cd macos-native && swift build -c release`：未通过；受限宿主禁止 manifest sandbox。加 `--disable-sandbox` 后，唯一失败点为 `GenerateDSYMFile ... dsymutil ... Operation not permitted`。

所有测试的 `HOME` 和 `RSLITE_CONFIG_DIR` 均指向 `/private/tmp`，未启动 GUI，未写真实配置目录。

## 4. PR 草稿

标题：`feat(rslite): gate RPD reset on trusted rollover`

```markdown
## Summary

- persist and validate trusted RPD reset records bound to key fingerprints
- reject stale/unknown in-flight quota headers after a reset boundary
- emit one `quota_reset_confirmed` event only after wall-clock and monotonic-clock confirmation
- cover trusted rollover, persistence, rollback, key rotation, and restart self-tests

## Validation

- `swift build -c release --disable-sandbox -debug-info-format none`
- `RSLITE_CONFIG_DIR=$(mktemp -d) .build/release/rslite --selftest`
```

## 5. 风险 / 需要决定事项

- 默认 release 构建的 dSYM 生成受当前执行沙盒阻断；请在允许 `dsymutil` 的环境复跑未加参数的质量门。
- 未修改 Python、iOS、路由状态机或 fixture；本次仅覆盖指定 macOS Swift 端。
