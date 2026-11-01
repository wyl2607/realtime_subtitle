# Brief：实现 spec 003 P3 统一字幕历史 SubtitleHistoryStore

## Goal
按 `.governance/specs/003-subtitle-ui/p3-design.md`（**接口已定稿，必须逐条照做，不许改接口名/签名/事件语义**）实现
`realtime_subtitle/ui/history_store.py`，并把悬浮主窗、📜 HistoryWindow、📺 TVWindow、🎞 CinemaBar 改成读同一个 store。

## 先读
1. `.governance/specs/003-subtitle-ui/p3-design.md`（全文，尤其 §5 接口、§8 视图、§9 调用点清单、§10 测试）
2. `CLAUDE.md` 第 4 节第 14/15/16/27/34/42 条
3. 现有代码：`realtime_subtitle/ui/subtitle_render.py`、`subtitle_window.py`、`popups.py`、`tv_window.py`、`cinema_bar.py`、`realtime_subtitle/app.py` 的 `_reinsert_pair_by_t0` / `_ui_show_line` / `_ui_drop_keys`

## 允许改的文件（只这些）
- 新建 `realtime_subtitle/ui/history_store.py`
- `realtime_subtitle/ui/{subtitle_render,subtitle_window,popups,tv_window,cinema_bar,design_tokens}.py`
- `realtime_subtitle/app.py`（只改 §9 列出的两处）
- `tests/*.py`（新建 `tests/test_history_store.py`、`tests/test_history_views.py`；改写现有用 `sentence_pairs`/`append_pair`/`backfill`/`drop_keys`/`HISTORY_KEEP` 的用例，断言含义不变，**不许删用例或加 skip 凑绿**）
- `RESULT.md`（仓库根，见下）

## Constraints
- 不改识别/翻译/路由行为；模式系统只走 `_apply_mode`；不碰 WM_NCHITTEST/BTN_RESERVE/命中测试。
- `history_store.py` 不 import PyQt6。
- 旧语言事件仍在 `_add_pair`/`_update_draft`/`_on_translate_failed` 用 `_is_stale_language` 拦，store 不判代数。
- 不实现原位替换：主窗收到任何 history 事件都整窗 `_render()`。
- Qt 测试：模块级 `_APP` 持 QApplication；等 UI 用 `_pump_until`；测试句子自己现写（不得读取/抄 transcripts/ 或 downloads/）。
- 不加 config 键、不加 design 之外的功能、不做无关重构。
- 不要 git commit / push。

## Done（自验后写进 RESULT.md）
- `TMPDIR=/tmp PYENV_VERSION=3.13.0 ~/.pyenv/shims/python -m pytest -q` 全绿（基线 1670 passed / 33 skipped，新增用例后只增不减）
- `ruff check .` 全绿
- `grep -rn "sentence_pairs\|HISTORY_KEEP\|_reinsert_pair_by_t0" realtime_subtitle tests` 无结果（lookup.py 的形参名 `sentence_pairs` 除外）
- RESULT.md：改了哪些文件、每个 §10 测试点对应哪个用例、pytest/ruff 末行输出、偏离设计之处（应为无；若有必须写理由）
