# Code Review: Sprint 002 / CR-007 (TK-007) lens=default

- id: BUG-01
  severity: blocker
  location: realtime_subtitle/app.py:293
  evidence: `on_local_pair` 执行 `del revision`，且 `_ui_show_line:1115` 写死 `window.add_pair(..., 0)`。用户切换语言后 `window.language_revision >= 1`，`subtitle_render.py:83` 的 `_is_stale_language(0)` 判定为过期，后续所有本机及远端字幕均被静默丢弃。
  suggested_fix: `on_local_pair` 与 `_ui_show` 保留并透传真实 `revision`。

- id: BUG-02
  severity: major
  location: realtime_subtitle/app.py:318
  evidence: `feed_audio` 仅在 `tier == "cloud"` 时喂音频给 `self.segmenter`。若因静音等待或临时降级未进 cloud 档，分段器缺少该段样本计数；切回云端后分段器吐出的 `a0` 相对真实时钟滞后，导致 `_origin_seconds + a0` 时间戳回退，破坏 P5 对齐。
  suggested_fix: 在 `self.cloud_on` 开启期间持续将音频喂给分段器，避免分段器时钟落后。

- id: BUG-03
  severity: major
  location: realtime_subtitle/app.py:349
  evidence: 配额 ≥90% 触发 `cooldown_quota` 后，路由降档不再发云端请求，`ledger.note()` 停止调用；全工程无任何地方调用 `ledger.usage()`，配额自然衰减后永远无法触发 `<80%` 恢复。同理，429 与 3 连败进入冷却后，没有任何逻辑派发 `cooldown_expired`，导致冷却永久卡死。
  suggested_fix: 在音频驱动或定时器中定期调用 `self.ledger.usage()` 并在冷却到期时派发 `cooldown_expired`。

- id: BUG-04
  severity: major
  location: realtime_subtitle/app.py:667
  evidence: `_on_node_closed` 断开时仅将 `self._node_client = None`，未停止 `self._loop` 与 `self._node_thread`。退避到期后新音频触发 `_ensure_node:533` 调用 `_start_node_loop()`，新建循环和线程并覆盖字段，导致旧事件循环和线程永久泄漏。
  suggested_fix: 在 `_on_node_closed` 中安全停止并清理旧循环与线程，或在重连时复用事件循环。

- id: BUG-05
  severity: major
  location: realtime_subtitle/app.py:1115
  evidence: P5 远端替换本机句时，`_ui_drop` 移除了旧句，随后 `_ui_show_line` 调用 `window.add_pair` 将远端句追加在 `sentence_pairs` 末尾（`subtitle_render.py:89`）。因远端返回有延迟，新近出现的本机句已先上屏，导致被替换的更早句子跳到最底行，界面字幕时序错乱。
  suggested_fix: 替换时按时间戳 `t0` 原位更新或重新插入 `sentence_pairs`，保持时序递增。

- id: BUG-06
  severity: minor
  location: realtime_subtitle/ui/settings_window.py:18
  evidence: 文件顶层直接 `from realtime_subtitle.remote.cloud_client import config_dir, read_groq_key`。`app.py:93` 启动即导入设置面板，即使两开关全关，也打破了「两开关都关时不 import 远端模块」的契约。
  suggested_fix: 将 `config_dir` 和 `read_groq_key` 改为延迟导入或移出远端模块。

- id: BUG-07
  severity: minor
  location: realtime_subtitle/app.py:551
  evidence: `_ensure_node` 在音频线程异步连接节点，若主线程中途触发 `close()` 设 `self._closed = True`，连接完成后未检查 `self._closed` 仍赋值 `self._node_client = client`，导致已关闭的 bridge 残留活动连接。
  suggested_fix: 连接返回后检查 `if self._closed:`，若已关闭则立即调用 `client.close()` 并退出。

verdict: needs_fix
