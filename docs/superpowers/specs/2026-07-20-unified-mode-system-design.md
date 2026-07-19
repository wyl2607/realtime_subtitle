# 统一模式系统（场景预设 + 游戏模式合并）设计

日期：2026-07-20
状态：用户已确认（架构方向/config schema/指示器/热键语义/测试迁移范围均已过）

## 背景与目标

项目里现在有两套都跟"游戏"沾边、但做完全不同事的机制：

1. **设置面板"场景预设"四按钮**（📺直播/🎬看剧/🎮游戏/🎧精听，`config.PRESETS`）：
   只调 5 个面板参数（CHUNK_SUBMIT_SECONDS/IDLE_FLUSH_SEC/MAX_SENTENCE_PAIRS/
   SHOW_BILINGUAL/DRAFT_TRANSLATION），不碰识别/翻译模型。
2. **全局热键 Ctrl+Alt+G "游戏模式"**：额外切 WHISPER_BEAM_SIZE（面板完全没有
   对应控件）和真正的 OLLAMA_MODEL（换轻量模型腾显存），且是"临时开关"语义——
   开启前存快照，关闭时把用户当时的自定义值恢复回去。

历史记录里这个"两套都叫游戏"的命名混淆被自己标注过好几次"遗留问题"。这次
用户明确要求：合并成一套统一的模式系统，几个按钮切换 + 一个常驻指示器显示
当前模式。

## 需求（用户已确认的关键决策）

1. **范围**：彻底合并，不是简单加个指示器摆在旧机制上。
2. **"游戏"模式改名"性能"**（⚡性能），点击时同时应用面板参数 + 切换轻量
   翻译模型（不是分开两个概念）。
3. **语义简化**：四个模式（直播/看剧/性能/精听）完全平等并列，都是"固定套值，
   点哪个就套用哪套参数"——**不保留**当前 G 热键那种"临时开关，关闭恢复
   进入前自定义值"的复杂语义。
4. **UI 位置**：四个模式按钮仍留在 ⚙️ 设置面板；主悬浮窗新增一个**常驻显示**
   （不随 hover 淡入淡出）的小指示器，显示当前模式。
5. **热键**：Ctrl+Alt+G 改为快捷跳到"性能"模式；再按一次跳回进入性能模式
   之前所在的模式（没记录则回默认"直播"）。
6. **自定义态同步**：面板里手动改任何一个参数滑块后，预设按钮会取消高亮
   （变"自定义"）——主悬浮窗指示器要跟着同步显示"自定义"，不能撒谎。

## 架构

### 统一入口：`main.py::SubtitleApp._apply_mode(name)`

唯一的"应用模式"函数，纯 `setattr(config, key, val)` 赋值（不碰任何 Qt
widget），因此**热键线程和主线程都能安全调用**（延续现有
`_toggle_game_mode` 已经在用的"config 属性赋值线程安全，widget 操作才需要
marshal 到主线程"这条约定）：

```python
def _apply_mode(self, name):
    """name=None 表示"用户手动改了参数，清成自定义"——只更新记账+指示器，
    不碰任何 config 值。name 为真实模式名时套用 PRESETS[name] 全部键
    （含 WHISPER_BEAM_SIZE）+ 按需切换翻译模型，再通知主线程同步 UI。"""
    if name is None:
        self._current_mode = None
        self.subtitle_window.notify_mode_applied(None)
        return True
    presets = getattr(config, "PRESETS", {}) or {}
    if name not in presets:
        return False
    for key, val in presets[name].items():
        setattr(config, key, val)
    target_model = (config.GAME_MODE_OLLAMA_MODEL if name == "性能"
                    else self._baseline_ollama_model)
    if target_model and target_model != config.OLLAMA_MODEL:
        old_model = config.OLLAMA_MODEL
        config.OLLAMA_MODEL = target_model
        self.translator.request_warm_model(old_model=old_model, new_model=target_model)
    self._current_mode = name
    self.subtitle_window.notify_mode_applied(name)
    return True
```

`self._baseline_ollama_model = config.OLLAMA_MODEL` 在 `__init__` 里创建
`self.translator` **之前**拍一次快照（这是 config_local 覆盖生效之后、任何
模式切换发生之前的值，代表"这台机器的正常档位"）。非性能模式统一切回这个
快照值，而不是 PRESETS 字典里写死的值——因为不同机器 config_local 里的
OLLAMA_MODEL 不一样，写死会覆盖用户机器的显存分档。

### 信号/槽：`subtitle_window.py`

沿用现有 `game_mode = pyqtSignal(bool)` 的模式，改造成：

```python
mode = pyqtSignal(object)  # 当前模式名（str）或 None（自定义）
```

```python
def notify_mode_applied(self, name):
    """线程安全：热键线程/主线程都调这个来通知模式已变。"""
    self.signals.mode.emit(name)

def _on_mode_applied(self, name):
    """主线程槽：真实模式名才需要重新从 config 拉一遍面板控件+高亮；
    None（自定义）只更新指示器文字。"""
    if name is not None:
        self.settings_window.refresh_from_config()
        self.settings_window.restore_active_preset(name)
    self._update_mode_indicator(name)
```

`refresh_from_config()` 已经存在（现在就是"游戏模式热键改 config 后面板
必须跟上"用的那个通用同步函数），不用为模式切换单独再写一套 widget 路由——
这正是这次简化的核心：**面板参数的"写"只有一条路（滑块 setValue 触发的
valueChanged 回调），"读回显示"也只有一条路（refresh_from_config）**，
不再有 `apply_preset`/`_apply_preset_key` 这套专门给预设按钮准备的旁路。

### 设置面板：`settings_window.py`

**删除**：`apply_preset()`、`_apply_preset_key()`、`set_game_mode()`、
`_game_mode_active` 字段、`PRESET_CONTROL_ATTRS` 常量（不再需要，因为面板
参数应用不再区分"预设走哪个 attr"，全部套 `refresh_from_config` 的通用
读回逻辑）。

**新增**：`__init__` 接受 `on_mode_change=None` 回调参数（同 `on_font_change`
参数的现成模式，由 main.py 在构造完 subtitle_window 后挂上
`self.subtitle_window.settings_window.on_mode_change = self._apply_mode`，
和现有 `self.subtitle_window.on_lookup = lambda ...: self.translator...`
是同一种"main.py 事后挂平铺回调"的接线风格）。

`_on_preset_clicked(name)` 简化为：
```python
def _on_preset_clicked(self, name):
    if self._on_mode_change:
        self._on_mode_change(name)
```
（不再自己动 widget；高亮/滑块由 `_apply_mode` 触发的 `mode` 信号 →
`_on_mode_applied` 统一同步。按钮 clicked 信号原生自带 toggle checked 行为，
这里不需要额外处理，因为 `restore_active_preset` 随后会覆盖成正确状态。）

`_mark_custom()`（用户手动拖滑块触发）末尾新增：
```python
if self._on_mode_change:
    self._on_mode_change(None)
```
这是唯一"主动通知变成自定义"的地方，已经在主线程（滑块 valueChanged 回调），
直接调用即可，不需要走信号。

`_PRESET_BUTTONS` 里 `("游戏", "🎮 游戏")` 改成 `("性能", "⚡ 性能")`。

### config.py

`PRESETS` 字典键 `"游戏"` 改名 `"性能"`，四个模式各自补齐
`WHISPER_BEAM_SIZE`（直播/看剧/精听=3，性能=沿用现有
`GAME_MODE_BEAM_SIZE`=1 的值）。`GAME_MODE_SUBMIT_SECONDS`/
`GAME_MODE_BEAM_SIZE`/`GAME_MODE_DISABLE_DRAFT` 三个旧的"游戏模式专属"
常量作废删除（数值直接写进 `PRESETS["性能"]`）；`GAME_MODE_OLLAMA_MODEL`
保留（`_apply_mode` 仍要用它做性能模式的模型目标）。

### 主悬浮窗指示器：`subtitle_window.py` + `window_chrome.py`

新增 `self.mode_indicator = QLabel(self.container)`，绝对定位**左上角**
（`btn_bar`/`ct_indicator` 都在右上角，避开），样式仿 `ct_indicator`
（半透明深底圆角小字），**不接入** `_set_controls_visible` 的 hover
淡入淡出——常驻显示，`_position_chrome()` 里加一行摆位置。

```python
def _update_mode_indicator(self, name):
    text = f"{_MODE_ICON.get(name, '⚙️')} {name}" if name else "⚙️ 自定义"
    self.mode_indicator.setText(text)
    self.mode_indicator.adjustSize()
```

初始状态：`SubtitleWindow.__init__` 里 `self.settings_window.restore_active_preset(...)`
调用之后紧接着 `self._update_mode_indicator(self.settings_window._active_preset)`
——直接读刚恢复好的高亮值，不重复读一遍 `self._state`，避免两处数据源不同步。
不触发 `_apply_mode`（和现在"重启只恢复高亮不重放"的既有行为一致——性能
模式的真实模型/beam 值永远从 `self._baseline_ollama_model`/config 默认值
起步，重启后不会自动重新切换模型，这和今天"重启后游戏模式总是关"的行为
完全一致，不是新引入的限制）。

### 热键：`main.py`

```python
def _toggle_perf_hotkey(self):
    """Ctrl+Alt+G：跳到性能模式；已经在性能模式则跳回之前那个模式
    （没记录过就回默认"直播"）。"""
    if self._current_mode == "性能":
        target = self._mode_before_perf or "直播"
    else:
        self._mode_before_perf = self._current_mode or "直播"
        target = "性能"
    self._apply_mode(target)
```
`self._mode_before_perf` 在 `SubtitleApp.__init__` 里初始化为 `None`。
`self._current_mode` **不能**无条件初始化成 `None`——`SubtitleWindow()`
构造时已经从持久化状态恢复了面板高亮（见上一节），如果 main.py 这边的
记账不同步，重启后第一次按热键会把错误的"之前模式"记成兜底的"直播"而
不是实际显示的高亮模式。正确顺序：`self.subtitle_window = SubtitleWindow()`
构造完之后，`self._current_mode = self.subtitle_window.settings_window._active_preset`
（读刚恢复好的高亮值，可能是 `None` 即自定义，也可能是某个模式名）。
热键注册表里 `4: ("Ctrl+Alt+G", ord('G'), ...)` 的处理函数从
`self._toggle_game_mode` 改成 `self._toggle_perf_hotkey`。

`_toggle_game_mode` 方法整个删除（`_game_mode_saved` 字段一起删）。

## 错误处理

- `_apply_mode(name)` 传入未知模式名（不在 `PRESETS` 里）：返回 `False`，
  不修改任何 config，打印警告（沿用现有 `apply_preset` 的错误处理风格）。
- `request_warm_model` 内部的老/新模型显式传入写法已经修过热键连按竞态
  （2026-07-14 压测发现的 bug），本次改动不touch这段，继续复用。
- 主悬浮窗指示器的 `mode_indicator` 找不到当前模式对应的 emoji（理论上
  不会发生，`_MODE_ICON` 覆盖 `PRESETS` 全部键）时回退默认 ⚙️。

## 测试

- `test_presets.py`：大改。不再测 widget 路由（`apply_preset`/
  `_apply_preset_key` 已删），改测 `main.py::_apply_mode`：
  - 应用"性能"模式 → 5 个 config 键 + `WHISPER_BEAM_SIZE` 全部落地正确值，
    且 `request_warm_model` 被调用（用 `test_game_mode.py` 已有的
    stub translator 模式，断言 `warmed` 列表）。
  - 应用非性能模式（如"直播"）且当前模型是性能模式的轻量模型时，
    正确切回 `self._baseline_ollama_model`。
  - 模型已经等于目标模型时不重复调用 `request_warm_model`（幂等）。
  - `name=None` 只更新 `_current_mode`，不碰任何 config 键、不调
    `request_warm_model`。
  - 未知模式名返回 `False`，不修改 config。
- `test_game_mode.py`：大改。原有"存值/恢复值"逻辑整个不存在了，改测
  `_toggle_perf_hotkey` 的跳转+记忆语义：
  - 从"直播"按热键 → 变"性能"；再按热键 → 变回"直播"。
  - 从"看剧"按热键 → 变"性能"；再按 → 变回"看剧"（不是回默认"直播"）。
  - 从未选过任何模式（`_current_mode is None`）按热键 → 变"性能"；
    再按 → 回默认"直播"。
- `test_settings_sync.py`/`test_tuning.py` 里各一个测 `set_game_mode` 的
  用例删除（方法已不存在）。
- 新增 `subtitle_window.py` 侧测试（在现有 UI 测试文件里追加，具体归属
  到实现计划阶段再定）：`mode` 信号触发 `_on_mode_applied` 后
  `refresh_from_config`+`restore_active_preset`+指示器文字都同步正确；
  `name=None` 时指示器变"⚙️ 自定义"且不触碰面板控件。
- 回归：全量 `pytest` + `test_hittest.py`（指示器新增的 widget 是否影响
  按钮命中区，理论上不影响，指示器在左上角不在 `BTN_RESERVE` 范围内，
  但仍需跑一遍确认）。

## 明确不做（YAGNI）

- 不新增第 5 个模式；PRESETS 只在现有 4 个基础上补齐 `WHISPER_BEAM_SIZE`
  和"性能"改名。
- 不给非性能三个模式设计"各自的模型目标"字段——只有性能模式需要换模型，
  为这一个用例给 PRESETS 加通用 schema 字段是过度设计。
- 不恢复"临时开关/进入前自定义值快照"这套复杂语义——用户已经明确选择
  "四个模式平等并列、都是固定套值"的简化方案。
- 不做重启后自动重放上次模式的模型/beam 值——和现状一致，只恢复高亮
  显示，不重新触发副作用。
