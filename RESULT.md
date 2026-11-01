# 修复结果

## 结论
**done** - 两个问题均已修复，所有测试通过，ruff 检查通过。

## 改动摘要

### G1（高）：`_parse_reset_record` 时间边界校验
- **文件**：`realtime_subtitle/remote/quota.py:485-494`
- **修改**：在 `_parse_reset_record` 中增加校验 `0 <= reset_at - observed_at <= MAX_RESET_REQUESTS_S`。不满足时返回 `None`（丢弃该恢复记录），保留 `server_day_used` 下限，不隔离整份账本。
- **配套**：`_apply_payload` 返回 `(legacy_saturated, normalized)` 元组，`normalized=True` 表示有数据被清理（坏事件或坏恢复记录），触发落盘固化。

### G3（中）：重启漏发 `quota_reset_confirmed`
- **文件**：`realtime_subtitle/remote/quota.py:206-213`
- **修改**：在 `_load_locked` 中，`_advance_reset` 之前先用旧周期完整用量 `_measure(now)` 判断是否 ≥ `ENTER_PERCENT`。若是，先置 `_quota_hot=True`，再推进重置消费记录。这样 `_publish` 里 `previously_hot=True`，会正确发出 `quota_reset_confirmed` 事件，路由才能从 `cooldown_quota` 恢复。

### 新增回归测试
- **文件**：`tests/test_remote_quota.py`
- `test_parse_reset_record_validates_time_bounds`：验证 G1 三种边界情况（负差值、超上限、合法）。
- `test_restart_after_reset_emits_quota_reset_confirmed_when_hot`：验证 G3 场景（冷却中、reset 后重启、恢复记录未消费）。
- 修正既有测试 `test_restarted_ledger_trusts_elapsed_reset_without_a_monotonic_observation` 的预期（该场景因 server_day_used=1995≥90% 也应发确认事件）。

## 测试命令与结果

```bash
# 单元测试
/Users/yumei/projects/rs-mac-venv/bin/python -m pytest -q tests/test_remote_quota.py tests/test_app_remote.py tests/test_p9_route_cases.py tests/test_remote_router.py
# 97 passed in 6.13s

# 代码风格检查
/Users/yumei/projects/rs-mac-venv/bin/python -m ruff check realtime_subtitle tests
# All checks passed!
```

## PR 标题
fix(quota): G1/G3 quota ledger fixes — reset_record time bounds validation + restart quota_reset_confirmed emission

## PR 描述草稿
### 变更内容
1. **G1**: `_parse_reset_record` 现校验 `0 <= reset_at - observed_at <= MAX_RESET_REQUESTS_S`。越界记录被丢弃（返回 `None`），保留 `server_day_used` 下限，不把整份账本隔离。`_apply_payload` 新增 `normalized` 返回值，触发清理后落盘。
2. **G3**: `_load_locked` 在 `_advance_reset` 前用旧周期 `_measure` 判热度（≥90% 置 `_quota_hot=True`），再消费恢复记录。修复重启时漏发 `quota_reset_confirmed` 导致路由卡在 `cooldown_quota` 的问题。
3. 新增两个回归测试，修正一既有测试预期。

### 验证
- 全量相关测试 97 通过
- ruff 无告警

## 风险 / 需要人决定的事项
- 无遗留风险。改动最小、仅触及两个明确缺陷点，均有回归测试覆盖。