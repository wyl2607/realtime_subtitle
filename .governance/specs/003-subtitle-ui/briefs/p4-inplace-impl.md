# Brief: P4 原位替换（Windows 主悬浮窗）

先读：`.governance/specs/003-subtitle-ui/p4-inplace-design.md`（语义 R1–R4，本 brief 的唯一需求源）、
`realtime_subtitle/ui/subtitle_render.py`（`_on_history_event`、`_render`、`_ever_shown`/`_fade_key`）、
`tests/test_ui_appear.py`（测试写法照抄它）、CLAUDE.md 第 4 节第 15/16/17/27 条。

## 要改
1. `realtime_subtitle/ui/subtitle_render.py`
   - `_on_history_event` 的 insert 分支：取 `self.history.get(event.keys[0])`；若 `row.src.strip()` 非空且出现在
     `self.live_committed` 里 → 晋升：从 `live_committed` 去掉首次出现的这段（接缝两侧原来任一侧是空白才用一个空格连接，
     否则直接拼；结果 strip），并把 key 加进 `self._ever_shown`（这样 `_render` 不会给它淡入）。然后照旧清草稿/状态、`_render()`。
   - 抽一个小的纯函数（模块级，例如 `_remove_promoted(live, src) -> str | None`，找不到返回 None），注释用中文写"为什么"。
   - 不许 import `realtime_subtitle.translate`（会把 translator_queue 整条拉起来）；不改 `_render` 的可见集合/minHold 逻辑。
2. `tests/test_ui_appear.py` 末尾补用例（只用 `_pump_until` 等条件，不写固定等待）：
   - 晋升：`live_committed = S0德语 + " " + "Rest des Satzes"`、`live_draft="草稿"`，`_render()`；然后 `_add(win, S0...)` →
     `live_committed == "Rest des Satzes"`、`live_draft == ""`、`_fade_key is None`、`_last_html` 里 S0 德语只出现一次；
     随后 `win._update_live("Rest des Satzes", "")` 不改变 `_last_html`。
   - 非晋升：live 行为空时 `_add` 新句 → `_fade_key == 该 key`（R2，保持原行为）。
   - R3/R4：两句上屏后 `win.update_dst("s1", 同长度的另一句中文)` → `_visible_pair_keys` 不变、`_fade_key is None`、
     用 `win._doc_for_render()` 对 `_last_html` 测得的高度不变、新译文在 `_last_html` 里旧译文不在。
   - `_remove_promoted` 纯函数参数化用例：开头/中间/末尾/找不到/中文无空格（"第一句。第二句"去掉"第一句。"→"第二句"）。

## 不许
- 改别的文件（ALLOW 只放这两个文件）；改 `_render` 二分/minHold；改命中测试；加配置项。

## 质量门
`TMPDIR=/tmp python -m pytest tests/test_ui_appear.py tests/test_history_store.py -q` 和 `ruff check realtime_subtitle tests` 全绿。
产出 RESULT.md：改了什么、测试结果。
