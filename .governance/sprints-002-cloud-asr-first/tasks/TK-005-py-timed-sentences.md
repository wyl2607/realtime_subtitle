# TK-005 — Windows 客户端句子带时间 + 样本时钟连续性（P10）

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
让 Python 桌面程序的每个上屏句子带 (t0, t1)（客户端样本时钟），为节点/云端结果按时间替换打基础；单机行为不变。

## Context
- RFC「P10」+ ARCH-03：`streaming_asr.process_iter` 只返回文本（streaming_asr.py:431-433），时间在这一层就丢了。
- 词已有 `(start, end, text)` 且加了 `buffer_time_offset`；`translator_queue._append_committed` 只收文本；UI 走 `on_pair`。
- CLAUDE.md 第 4 节第 31/35/41/42 条（语言切换、clear_context、代数守卫）必须保持。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - realtime_subtitle/asr/streaming_asr.py
  - realtime_subtitle/translate/translator_queue.py（以及它 re-export 的 text_rules 不改）
  - realtime_subtitle/ui/subtitle_window.py、realtime_subtitle/ui/subtitle_render.py
  - tests/test_timed_sentences*.py（新增）
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] `process_iter` 同时给出已提交词的时间（保持现有返回值语义兼容，调用方逐处改）
- [ ] 句子 t0=首词 start、t1=末词 end，`on_pair` 带 t0/t1/key；UI 句对历史存 t0/t1/source/key（本 TK 只存，不做替换）
- [ ] 时间基准 = 采集起累计样本数 / 16000；测试证明在 `clear_context`（语言切换）、`.paused` 暂停恢复、设备热切换后 t0 单调且与样本时钟一致
- [ ] 中文（无空格语言）与德语各一条端到端单测
- [ ] 全量 pytest 0 failed、ruff 绿（Mac 用 `~/projects/rs-mac-native/venv/bin/python -m pytest --basetemp=/private/tmp/claude-501/rsb`）

## 验证命令
```bash
~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/claude-501/rsb && ~/projects/rs-mac-native/venv/bin/ruff check .
```

## 执行建议
codex（high）。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
- 2026-10-11 16:38 CST: executing（批次 1）
- 2026-10-11 18:41 CST: done（CR-005 resolved，2 轮）
