# TK-001c — worker：asr_state.json 的 translator 写当前真实翻译器名

## Goal
会话结束收口 asr_state.json 时，`translator` 字段写**本会话真实在用的翻译器名**（`apple` / `ollama:<model>`），而不是永久沿用文件里的旧值。

## Context
- 来源：CR-005 F4（拆分）。安装自检在语言包未装时写 `translator="ollama:<model>"`；之后用户装好德→中语言包，worker 实际已走 `apple`，但 `_update_rtf_file` 里 `old.get("translator")` 优先，`/v1/info` 永久报 ollama → 路由按 P7 的 translator_rank 打分偏低。
- 现状代码：`realtime_subtitle/node/worker.py::_update_rtf_file`（约 368–370 行）：旧值非空就保留，只有旧值缺失才用 `self._translator.name`。
- 时序：`_finish_session` 在 `_on_hello` 里先于翻译器替换调用、EOF/SIGTERM 路径翻译器未关，所以收口时 `self._translator` 正是刚结束那个会话的翻译器。
- 契约：RFC P1 `translator` 取值 `"apple|ollama:<model>"`（冻结）；S7 只写数字/名称字段，不写正文。

## Constraints
- write_scope:
  - realtime_subtitle/node/worker.py
  - tests/test_node_worker.py
- 禁止修改：其他一切路径（尤其 `realtime_subtitle/translate/translator_queue.py`、`node/info.py`、`node/gateway.py`、`scripts/node/**`、`.governance/**`）
- 不改 P1–P8；不改 asr_state.json 的键集合（仍是 model/backend/rtf/translator）

## Done criteria
- [ ] 本会话翻译器可用（`self._translator is not None`）→ `translator` 写 `self._translator.name`，覆盖旧值
- [ ] 本会话翻译器不可用（构造失败 / None）→ 保留旧的合法非空值；旧值也没有时写 `DEFAULT_TRANSLATOR`（不写 "none"：不在 P1 枚举内）
- [ ] model/backend 的沿用规则不变
- [ ] 测试：旧值 ollama + 本会话 apple → apple；旧值 apple + 本会话翻译器构造失败 → apple；无旧值 + 无翻译器 → DEFAULT；info.NodeInfo 读回与之一致
- [ ] 全量 pytest、ruff 通过

## 验证命令
```bash
PYTHONPATH=/home/user/stubs QT_QPA_PLATFORM=offscreen /home/user/venv-rs/bin/python -m pytest -q tests/test_node_worker.py tests/test_node_gateway.py
PYTHONPATH=/home/user/stubs QT_QPA_PLATFORM=offscreen /home/user/venv-rs/bin/python -m pytest -q --continue-on-collection-errors
/home/user/venv-rs/bin/ruff check .
```

## 状态历史
- 2026-10-09 10:40 CST: planned（CR-005 F4 拆出）
