# RESULT — 可信 RPD 翻日信任门（Python 端）

## 1. 结论

done。已按契约 R1–R7 补齐 Python 额度账本的恢复记录、key 绑定、乱序/旧周期响应拦截、
墙钟+单调钟双重翻日判定、只重置 RPD、`quota_reset_confirmed` 事件与落盘；并把 cloud client
的 key 指纹/请求发出时刻接到 `app.py` 的记账调用。未改路由状态机和 `route_cases.json`。

## 2. 改动摘要

- `realtime_subtitle/remote/quota.py`
  - 新增恢复记录 `_Recovery{reset_at, observed_at, key_fp, consumed, observed_mono}`；
    `observed_mono` 只在内存，不落盘。
  - `note()` 新增 `key_fp` / `sent_at`；只有同一响应同时带合法 remaining + reset 才建立/替换记录。
  - R2 key 变更丢弃记录但保留 `server_day_used` 下限；`key_fp=None` 不建记录。
  - R3 翻日后的迟到响应（`sent_at=None` 或 `< reset_at`）头一律不采信，只记本地事件。
  - R4 只有墙钟 `now >= reset_at`、无 >5s 回拨、且单调钟流逝足够才确认翻日；回拨丢弃记录并保留下限。
  - R5 翻日只清 24h 请求数的服务端分量，1 分钟/1 小时/24 小时音频窗口和本地事件不动。
  - R6 此前已把额度打进冷却时，翻日当次用 `quota_reset_confirmed`（四窗口最大值）代替 `quota_update`；
    `consumed` 落盘保证每条记录只触发一次。
  - R7 恢复记录原子落盘；旧文件缺字段视为无记录；字段损坏只丢记录不隔离整份账本。
- `realtime_subtitle/remote/cloud_client.py`
  - `CloudTranscript` 新增 `key_fingerprint` / `sent_at`；`CloudError`（含 `CloudCancelled`）新增 `sent_at`。
  - 请求发出时记录墙钟并随成功/失败结果返回；指纹仍只来自 key 哈希。
- `realtime_subtitle/app.py`
  - `_transcribe_one` / `_on_cloud_exception` 记账时传入请求实际 key 指纹与 `sent_at`。
- `tests/test_remote_quota.py`
  - 覆盖契约要求的 10 类场景，并更新 #87 旧用例以适配“同时带双头 + key 指纹 + 单调钟”的信任前提。
- `tests/test_app_remote.py` / `tests/test_remote_cloud_client.py`
  - 覆盖 key 指纹/`sent_at` 从 cloud client 到账本的接线，以及失败路径。

文件清单：

```
realtime_subtitle/remote/quota.py
realtime_subtitle/remote/cloud_client.py
realtime_subtitle/app.py
tests/test_remote_quota.py
tests/test_app_remote.py
tests/test_remote_cloud_client.py
RESULT.md（本文件）
```

建议 commit message：

```
feat(quota): gate RPD reset on trusted server-day rollover

Require both remaining and reset headers plus a key fingerprint to create a
recovery record. Confirm rollover only when the wall clock is past the reset
point without a >5s rollback and the monotonic clock has elapsed. Drop stale
old-period responses, emit quota_reset_confirmed exactly once in place of
quota_update, and persist the consumed flag. Wire key fingerprint and request
sent time through the Groq cloud client into app bookkeeping.

Co-Authored-By: Codex <noreply@openai.com>
```

## 3. 测试命令与结果

```bash
TMPDIR=/tmp /Users/yumei/projects/rs-mac-venv/bin/python -m pytest -q \
  tests/test_remote_quota.py tests/test_app_remote.py \
  tests/test_remote_cloud_client.py tests/test_p9_route_cases.py
# 114 passed in 17.95s

/Users/yumei/projects/rs-mac-venv/bin/python -m ruff check realtime_subtitle tests
# All checks passed!

TMPDIR=/tmp /Users/yumei/projects/rs-mac-venv/bin/python -m pytest -q
# 1512 passed, 33 skipped in 194.93s
```

测试均使用 `tmp_path`，未触碰真实 `~/.config/realtime_subtitle` 等用户目录。

## 4. PR 草稿

**标题**：feat(quota): 可信 RPD 翻日信任门（Python 端）

**描述**：

```markdown
## 背景
#87 已能解析 `x-ratelimit-reset-requests` 并在恢复点后重算 RPD，但账本没有可信的翻日
证据，也没有 `quota_reset_confirmed` 的生产者。本 PR 补齐 Python 端信任门（R1–R7）。

## 变更
- 恢复记录 = `{reset_at, observed_at, key_fp, consumed}`，仅同一响应同时带合法
  `remaining-requests` + `reset-requests` 时建立/替换；`observed_mono` 不落盘。
- key 变更丢弃记录但保留 `server_day_used` 下限；`key_fp=None` 不建记录。
- 翻日后 `sent_at=None` 或早于 `reset_at` 的响应头不采信。
- 翻日需墙钟过恢复点、无 >5s 回拨、单调钟流逝足够；只清 24h 请求数的服务端分量。
- 之前已进入额度冷却时，当次发一次 `quota_reset_confirmed`（四窗口最大值）代替
  `quota_update`，`consumed` 落盘防重复。
- Groq 客户端把 key 指纹和请求发出时刻带到 `app.py` 记账调用。
- 不改路由状态机、不改 `route_cases.json`。

## 测试
- `tests/test_remote_quota.py`：10 类契约场景 + 旧用例适配
- `tests/test_app_remote.py` / `tests/test_remote_cloud_client.py`：接线与字段
- 质量门 114 passed；全量 1512 passed, 33 skipped；ruff 全绿

## 风险
- 单调钟判定是新增约束：进程内记录必须真实经过恢复时长；重启后按墙钟 R4a 判定。
- 旧 #87 账本只有 `server_reset_at`，会被视为无恢复记录（保留下限，fail-closed）。
```

## 5. 风险 / 需要人决定的事项

- 未在真机 Groq 响应上跑过，只用单元测试与假 session 验证；服务端头行为仍以 #87 的解析为准。
- 重启后没有 `observed_mono`，只能按 R4a（墙钟）确认；这是契约允许的行为。
- `mono` 构造参数仅为测试注入，默认使用 `time.monotonic()`。
