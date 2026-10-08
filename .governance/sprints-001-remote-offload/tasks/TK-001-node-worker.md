# TK-001 — 节点 worker：VAD 分段 + 整段识别 + 可插拔翻译

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 的 P1–P8 为准，**不得自行修改契约**。

## Goal
实现节点上的识别子进程。它从 stdin 读 P3 管道协议的音频和控制消息，用 Silero VAD 把音频切成段，每段只做一次整段识别，再翻译，然后通过 stdout 输出 P2 格式的 final 和 translation 事件，每条都带精确的 a0 和 a1。

## Context
- 当前分支是 feat/macos-native，先读 RFC 的「架构选择」第 2、3 条。
- 可复用的代码：
  - `realtime_subtitle/asr/backends.create_whisper_model`：包含 MLX 单线程执行器的修复，见 d9d6f5d。
  - `realtime_subtitle/translate/text_rules`：德语断句和幻觉黑名单。
  - `realtime_subtitle/translate/apple_translate.AppleTranslator`
  - `faster_whisper.vad`
- **不要用** `WhisperQueueTranslator`。
- 分段参数：静音 ≥0.6s 判为一段结束；单段最长 15s，超过就在能量最低的地方强制切开。
- `AsrEngine` 和 `Translator` 写成接口，Mac 用 MLX 实现。Translator 先选 Apple，语言对的状态不是 installed 才退回 Ollama。

## Relationship to Existing Systems (RC-002)
- 共存：A 版（Python 桌面字幕，包括 `translator_queue.py`）和 rslite 本机 B 的行为不变。
- 替代：节点 v2 取代 v1（`realtime_subtitle/remote/`、`RemotePipeline.swift`），具体删除由 TK-005 和 TK-006 负责。
- 衔接：完成后由 Coordinator 更新 TASKS.md、CURSOR.md 和 metrics。

## Constraints
- write_scope:
  - realtime_subtitle/node/worker.py, segmenter.py, engines.py
  - tests/test_node_worker.py, tests/test_node_segmenter.py
- 禁止修改：
  - realtime_subtitle/translate/**, realtime_subtitle/asr/**（只能读，不能改）
  - realtime_subtitle/node/gateway.py, info.py（归 TK-002）
- 不跳过 hooks；不做破坏性 git 操作；不 push。
- 注释用中文，写清「为什么」；不加需求范围外的功能、配置旋钮或回退逻辑。
- 遵守 CLAUDE.md 第 4 节和第 7 节的避坑条目。

## Done criteria
- [ ] segmenter：用合成音频测试 0.6s 静音切段、15s 强制切；a0/a1 按样本数计算，误差 ≤10ms。
- [ ] worker：用假的 AsrEngine 和 Translator 跑通 hello → 音频 → flush/drain → final/translation/drained 的事件序列和 id 递增，退出码按 P3 约定。
- [ ] S1：Ollama 适配器构造时调用 `_assert_local_ollama`，有测试；URL 只能经 `ollama_url()` 取。
- [ ] S7：日志里不出现转录或翻译正文，有测试断言。
- [ ] 真机冒烟（Coordinator 执行）：用 concat5.wav 在本机跑 worker，每段说完后到 final 的延迟中位数 ≤4s（M2 本机只作参考），记下 WER。
- [ ] 全量 pytest 和 ruff 通过。

## 验证命令
```bash
QT_QPA_PLATFORM=offscreen ~/projects/rs-mac-venv/bin/python -m pytest -q
~/projects/rs-mac-venv/bin/ruff check .
```

## 状态历史
- 2026-10-08 CST: planned
