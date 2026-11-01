# 结果

## 1. 结论

完成。Python 端已实现可信 RPD 翻日信任门、一次性 `quota_reset_confirmed` 事件及请求元数据接线。

## 2. 改动摘要与文件清单

- `realtime_subtitle/remote/quota.py`：增加可落盘的恢复记录（含 key 指纹与 consumed）；以墙钟和单调钟共同确认翻日；拒绝旧周期迟到头；仅清除 RPD 服务端分量；恢复记录字段损坏时仅丢记录。
- `realtime_subtitle/remote/cloud_client.py`：成功和实际发出的失败请求都携带 key 指纹、请求发出墙钟时间。
- `realtime_subtitle/app.py`：将上述元数据传入账本。
- `tests/test_remote_quota.py`：覆盖双头建记录、时钟门、换 key、迟到响应、持久化与重启等契约。
- `tests/test_app_remote.py`、`tests/test_remote_cloud_client.py`：覆盖元数据接线。

建议提交信息：

```text
feat(quota): gate RPD reset on trusted observation

Co-Authored-By: Codex <noreply@openai.com>
```

## 3. 测试命令与结果

```text
TMPDIR=/tmp /Users/yumei/projects/rs-mac-venv/bin/python -m pytest -q tests/test_remote_quota.py tests/test_app_remote.py tests/test_remote_cloud_client.py tests/test_p9_route_cases.py
# 109 passed

/Users/yumei/projects/rs-mac-venv/bin/python -m ruff check realtime_subtitle tests
# All checks passed!
```

## 4. PR 标题与描述草稿

标题：`feat(quota): gate RPD reset on trusted observation`

```markdown
## Summary

- Persist a key-bound RPD reset observation and require trusted wall-clock/monotonic-clock rollover before clearing the server day-request floor.
- Emit one `quota_reset_confirmed` event after a trusted rollover instead of that cycle's `quota_update`.
- Pass request key fingerprints and send timestamps from CloudClient through RemoteBridge to QuotaLedger.

## Validation

- `TMPDIR=/tmp /Users/yumei/projects/rs-mac-venv/bin/python -m pytest -q tests/test_remote_quota.py tests/test_app_remote.py tests/test_remote_cloud_client.py tests/test_p9_route_cases.py`
- `/Users/yumei/projects/rs-mac-venv/bin/python -m ruff check realtime_subtitle tests`
```

## 5. 风险 / 需要人决定的事项

- 无需额外决定；改动未触及 Swift/iOS、路由状态机或 fixture。
- 未提交、未推送，供编排器复核后提交。
