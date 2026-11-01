# Spec 003 P3 设计：统一字幕历史 `SubtitleHistoryStore`（Windows/macOS Python 版）

状态：接口定稿（Claude，2026-11-01）。实现派非 Claude lane；本文件是唯一接口真相源，实现与它不一致时以本文件为准、或回来改本文件。

## 1. 现状问题（为什么要做）

四个显示面各自持有数据，靠 `subtitle_render` / `app.py` 手动扇出同步：

| 显示面 | 现在的数据 | 问题 |
|---|---|---|
| 悬浮主窗 | `sentence_pairs`：7 元组列表，上限 `HISTORY_KEEP=50` | 元组下标散落各处（`item[6]` 是 key、`item[3]` 是 t0） |
| 📜 历史窗 `HistoryWindow` | 自己的 `_entries`（2000 条） | 只能追加/改/删，没有时间、来源、搜索、导出 |
| 📺 电视窗 `TVWindow` | 自己的 `_entries`，隐藏时不维护，打开时 `backfill(sentence_pairs)` | 回填只拿到主窗的 50 条 |
| 🎞 影院条 `CinemaBar` | 只记 `_current_key` | 远端句插到中间（`_reinsert_pair_by_t0`）时收不到 |

每加一种变更（P2 失败句、P5 远端替换），都要在 3~4 处各写一遍 `append_pair/update_dst/drop_keys`，漏一处就有一个视图不同步（analysis.md A 节）。

## 2. 目标与非目标

做：
- 一个数据源 `SubtitleHistoryStore`，四个显示面都从它读，变更以事件通知。
- 历史窗：按时间排序（= store 顺序）、显示时间与来源、搜索过滤、复制全文、导出 SRT、语言切换处插分界线。
- 事件按 key 粒度，给以后的「原位替换不整窗重排」（P4 没做完的那部分）留好接口。

不做：
- **原位替换本身**：主窗收到 `update` 事件仍然整窗 `_render()`（第 20 条：`_render` p50 0.6ms，没必要为性能加复杂度）。
- 草稿（live 行的流式译文）不进 store，理由见 §4.4。
- rslite 的简易历史（另一条线）。
- 不改识别/翻译/路由行为，不改模式系统，不碰命中测试。

## 3. 模块与位置

新文件 `realtime_subtitle/ui/history_store.py`：
- **零 Qt 依赖**（只 import 标准库、`realtime_subtitle.config`、`design_tokens`、`translate.transcript` 里的 `_srt_time` / `estimate_duration`）。这样可以不起 QApplication 单测，也不会把 PyQt6 拉进别的导入链。
- 实例由 `SubtitleWindow.__init__` 建：`self.history = SubtitleHistoryStore()`，在建历史窗/电视窗/影院条**之前**，把同一个实例传进三者构造函数。

## 4. 数据模型

### 4.1 `HistoryRow`

```python
@dataclass
class HistoryRow:
    key: str                 # 稳定且唯一。业务 key（翻译句 id / 远端负数 id 的字符串）
                             # 或 store 生成的 "@n:<序号>"（调用方没给 key 时）
    src: str                 # 原文
    dst: str                 # 译文；失败句为 design_tokens.TEXT_UNTRANSLATED，
                             # 重试中为 TEXT_TRANSLATING（"翻译中…"）
    wall: float              # 首次加入的墙钟 time.time()；更新/挪位不变
    t0: float | None = None  # 音频时钟（和 transcript 会话导出同一个时基）
    t1: float | None = None
    source: str = "local"    # "local" | "remote"（远端替换后改成 "remote"）
    revision: int = 0        # 语言代数（_lang_revision），用于分界线

    @property
    def status(self) -> str: # "ok" | "failed" | "retrying"，由 dst 推出，不另存
```

`status` 不单独存：它和 `dst` 是同一个事实的两种写法，分开存一定会走散。

在 `design_tokens` 加 `TEXT_TRANSLATING = "翻译中…"`，把 ui/ 里散落的 `"翻译中…"` 字面量换成它（行为不变）。

### 4.2 顺序与容量

- 顺序 = 时间顺序：本机句按到达追加；远端替换句用 `move_by_t0` 按 t0 插回（原 `_reinsert_pair_by_t0` 的算法原样搬进来）。历史窗「按时间排序」就是 store 顺序，不在视图里另排。
- 容量 `HISTORY_STORE_MAX = 2000`（模块常量，**不加 config 键**）：等于原历史窗/电视窗的封顶。超出从最旧开始丢，发 `delete(reason="trim")`。
- 主窗不再有 `HISTORY_KEEP`：它本来只上屏 `MAX_SENTENCE_PAIRS`（≤20）条，从 store 尾部倒着取即可。副作用：🤖AI 分析的 `filter_recent_german_context` 能拿到完整的最近 N 分钟（原来被 50 条截断），它自己有 `AI_CONTEXT_MAX_CHARS` 封顶。

### 4.3 key 规则

- 调用方给了 key（非空）→ 用它（`str(key)`）。
- 没给 → store 生成 `"@n:<自增序号>"`。以 `@` 开头，不会和业务 key 撞。
- `add()` 遇到已存在的 key = 更新（沿用现在 `_add_pair` 的语义），不新增一行。

### 4.4 草稿不进 store

草稿是 live 行的临时状态：随下一个 `update_live` 失效、不该被搜索/导出、没有 key。仍由主窗 `live_draft` 持有，`_update_draft` 照旧直接调 `tv_window.update_draft` / `cinema_bar.update_draft`。新句 `insert` 时主窗清 `live_draft`（现状如此）；电视窗收到 insert 时照旧先撤草稿行再追加。

## 5. 接口（定稿）

```python
HISTORY_STORE_MAX = 2000

@dataclass(frozen=True)
class HistoryEvent:
    kind: str                       # "insert" | "update" | "delete" | "move" | "reset" | "language"
    keys: tuple[str, ...] = ()      # insert/update/move：恰好 1 个；delete：被删的全部；reset/language：空
    index: int | None = None        # insert/move：该行操作后在 rows() 里的下标
    fields: frozenset[str] = frozenset()  # update：变了哪些字段，子集 {"src","dst","source"}
    reason: str = ""                # delete："drop"（远端替换删本机句）| "trim"（超容量）

class SubtitleHistoryStore:
    def __init__(self, capacity: int = HISTORY_STORE_MAX): ...

    # ---- 读 ----
    def rows(self) -> list[HistoryRow]          # 时间顺序的浅拷贝列表（行对象本身是共享的，只读）
    def get(self, key) -> HistoryRow | None
    def last(self) -> HistoryRow | None
    def __len__(self) -> int
    def language_label(self, revision: int) -> str   # mark_language 记下的 "德语→中文"；没记过返回 ""
    def search(self, query: str) -> list[HistoryRow] # 空白 query = 全部；否则 src/dst 大小写不敏感子串

    # ---- 写（只在 Qt 主线程）----
    def add(self, src, dst, *, key=None, t0=None, t1=None,
            source="local", revision=0, wall=None) -> HistoryRow
    def update(self, key, *, dst=None, src=None) -> bool
        # dst/src 为 None = 不改；src 只有非空才覆盖（失败句保留原文）。
        # 值没变不发事件、返回 False；key 不存在返回 False。
    def drop(self, keys) -> list[str]           # 返回真删掉的 key；不存在的静默忽略
    def move_by_t0(self, key, *, source=None) -> bool
        # 把该行按 t0 挪到时间顺序位置（t0 None 视为 -inf，比较 TypeError 视为不更晚）；
        # source 非 None 时一并改来源。发一个 move 事件；source 变了再发 update(fields={"source"})。
    def clear(self) -> None                     # 发 reset
    def mark_language(self, src, tgt, revision) -> None  # 记标签并发 language 事件

    # ---- 订阅 ----
    def subscribe(self, listener: Callable[[HistoryEvent], None]) -> None
    def unsubscribe(self, listener) -> None
        # 同步调用，按订阅顺序。单个 listener 抛异常：打印一行 ⚠️ 后继续通知其它 listener，
        # 不往上抛——调用链是 Qt 槽，PyQt6 槽里未捕获的异常会 abort 整个进程（第 27 条）。

    # ---- 导出 ----
    def to_text(self, rows=None) -> str         # 复制全文：每行 "[HH:MM:SS] 原文\n译文\n" 空行分隔
    def to_srt(self, rows=None, mode="bilingual") -> str
```

模块级纯函数 `rows_to_srt(rows, mode="bilingual") -> str`（`to_srt` 调它）：
- mode：`bilingual`（原文上、译文下）/ `target`（只译文，缺译文退回原文）/ `source`。
- 失败/重试中的行：译文按空处理（不把「未翻译 ⟳」写进字幕文件）。
- 时间轴和 `transcript._save_transcript` 同一套：有 t0/t1 用音频时钟；缺的用 `wall - 第一行wall` 作 end、`estimate_duration` 推 start；整体单调不重叠（start = max(上一条 end, …)，end ≤ start 时补时长）。
- 空列表返回 `""`。

## 6. 线程边界

- **所有写操作只在 Qt 主线程**。现有写入口已经都在主线程：`subtitle_render` 的槽（`_add_pair` / `_on_translate_failed` / `update_dst` / `_handle_retry_click`）、`app.py` 的 `_ui_show_line` / `_ui_drop_keys`（经 `_ui_invoke`）、`_on_language_applied`。store 不加锁、不做线程检查。
- 后台线程想改历史，只能走现有信号（`signals.pair` / `translate_failed`）或 `_ui_invoke`，不许直接拿 `window.history`。

## 7. 语言代数（第 42 条不变）

- 旧语言事件**仍在最后消费处拦**：`_add_pair` / `_update_draft` / `_on_translate_failed` 里 `_is_stale_language(revision)` 为真就不写 store。store 本身不判代数，只把 revision 存进行里。
- `_on_language_applied(src, tgt, revision)` 里调 `self.history.mark_language(src, tgt, revision)`（在 `language_revision` 赋值之后）。
- 历史窗分界线：渲染时相邻两行 revision 不同，就在后一行前插一行「── 切换到 {language_label(rev)} ──」（标签为空时写「── 切换语言 ──」）。不往 store 里塞假行。
- 远端句沿用 `_ui_show_line` 现在的代数取法（`max(rev, window.language_revision)`）。

## 8. 各视图怎么订阅

| 视图 | 订阅后做什么 |
|---|---|
| 主窗（`LiveTextRenderMixin`） | `_on_history_event`：`insert` 时清 `live_draft`/状态行（原 `_add_pair` 里的那几行），然后任何事件都 `_render()`。`_render` 从 `self.history.rows()` 尾部倒着取 ≤`MAX_SENTENCE_PAIRS` 条，`_pair_key(row)` 直接是 `row.key`。|
| 📜 `HistoryWindow(store)` | 去掉 `_entries` / `append_pair` / `update_dst` / `drop_keys`。`insert` 且落在末尾、且无搜索词 → 增量追加一块；其它事件 → `_rebuild()`（按当前搜索词 `store.search(q)` 重建，保持"在底部就跟随、否则保持滚动位置"）。顶部一行：搜索框 `QLineEdit`（textChanged 即过滤）+「复制全文」+「导出 SRT」按钮。每块显示 `[HH:MM:SS]`（`row.wall`）、来源徽标（local→「本机」，remote→「远端」）、原文、译文（失败/重试色同现在）。复制/导出作用于**当前过滤后**的行。导出用 `QFileDialog.getSaveFileName`（默认文件名 `subtitles_YYYYmmdd_HHMMSS.srt`，utf-8 写入；取消即什么都不做）。|
| 📺 `TVWindow(store)` | 去掉 `_entries` / `append_pair` / `update_dst` / `drop_keys` / `backfill`。隐藏时忽略事件。可见时：`insert` 落在末尾 → 撤草稿 + 追加一块（现有增量路径）；其它事件 → 从 `store.rows()` 重建。`_toggle_tv` 打开前改调 `tv_window.rebuild_from_store()`（原 backfill 的清空重建）。显示文本规则不变：失败/重试中显示「原文 + 标记」，否则只显示译文。|
| 🎞 `CinemaBar(store)` | 去掉 `append_pair` / `update_dst` / `drop_keys`。隐藏时忽略事件。`insert` 且该行是 `store.last()` → 显示它（= 原 append_pair 逻辑）；`update` 且 key == `_current_key` → 原地改文字（= 原 update_dst）；`delete` 含 `_current_key` → 清空（= 原 drop_keys）；`move`/`reset` → 若 `_current_key` 不在 store 里就清空。`_toggle_cinema` 打开时显示 `store.last()`。|

视图订阅在各自构造函数里做（`store.subscribe(self._on_history_event)`）。这些窗口与主窗同寿命，不需要退订。

`_on_history_event` 是普通方法不是 Qt 虚函数，但它在 Qt 槽的调用链上：里面只用已确认存在的 PyQt6 属性（第 27 条）。

## 9. 调用点改写清单

- `subtitle_window.py`：建 store；`HistoryWindow(self.history)` / `TVWindow(self.history)` / `CinemaBar(self.history)`；删 `HISTORY_KEEP` 和 `sentence_pairs`；`_toggle_tv` / `_toggle_cinema` / `_on_ai_analysis_clicked` 改读 store（AI 分析传 `[(r.src, r.dst, r.wall) for r in self.history.rows()]`）；`_on_language_applied` 加 `mark_language`。
- `subtitle_render.py`：`_add_pair` → `self.history.add(...)`；`update_dst` → `self.history.update(key, dst=..., src=...)`；`_on_translate_failed` → 有该 key 就 `update(dst=TEXT_UNTRANSLATED, src=source_text)`，没有就 `add`；删掉所有对 `history_window/tv_window/cinema_bar` 的 `append_pair/update_dst` 扇出；`_render` 改读 `HistoryRow`。
- `app.py`：`_reinsert_pair_by_t0(window, key, source)` 改成 `window.history.move_by_t0(key, source=source)`（删掉这个模块函数）；`_ui_drop_keys` → `window.history.drop(keys)`（删掉对三个视图的 `drop_keys` 扇出和手动 `_render`）。
- 没有其它地方读 `sentence_pairs`（`translate/lookup.py` 只收参数）。

公开方法保留：`SubtitleWindow.add_pair` / `update_draft` / `on_translate_failed` / `update_dst`（线程安全入口与 app 调用不变）。

## 10. 测试

- 新 `tests/test_history_store.py`（纯 Python，不起 Qt）：add/同 key 更新/生成 key/容量 trim 事件/update 无变化不发事件/drop/move_by_t0（含 t0 None、来源改写）/clear/mark_language/search/to_text/to_srt 三种模式与失败句/时间轴单调/listener 抛异常不影响其它 listener。
- 视图测试（`tests/test_history_views.py` 或并入现有）：同一 store 上四个视图一致——插入、按 key 改译文、远端替换（drop + add + move）后历史窗/电视窗/影院条内容都对；历史窗搜索过滤、复制全文（剪贴板）、导出 SRT（monkeypatch `QFileDialog.getSaveFileName` 指向 tmp_path）、语言分界线；电视窗隐藏时收事件、打开时从 store 重建。
- 现有用 `sentence_pairs` / `append_pair` / `backfill` / `drop_keys` 的测试改成走 store（`win.history.clear()` 代替 `win.sentence_pairs.clear()` 等），断言含义不变，不许删用例凑绿。
- 规则：模块级 `_APP` 持 QApplication（第 16 条）；等 UI 用 `_pump_until`（第 15 条）；测试句子自己现写（第 34 条）。
