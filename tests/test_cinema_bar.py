"""🎞 影院字幕条单测：最新一句语义 / 草稿 / 自动淡出 / 接线 / PyQt 枚举防线。

运行: venv\\Scripts\\python.exe -m pytest tests/test_cinema_bar.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114（见 main.py / test_hittest.py）。
⚠️ QApplication 必须持有模块级引用，否则会被立即 GC → 建 QWidget 时 qFatal 秒退。
"""
import torch  # noqa: F401  先于 PyQt5
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QEvent

import realtime_subtitle.config as config
from realtime_subtitle.ui.cinema_bar import CinemaBar


_APP = None  # 必须持有引用，理由同 test_tv_window


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _shown_bar():
    """建一个普通 show（非覆盖层全屏）的 CinemaBar：append/draft 有 isVisible 门控。"""
    _app()
    bar = CinemaBar()
    bar.resize(600, 200)
    bar.show()
    return bar


# ---------------------------------------------------------------------------
# ☠️ 这条是本文件最重要的测试：它抓的是一次"没有 traceback 的崩溃"
# ---------------------------------------------------------------------------

def test_ui_code_never_references_nonexistent_qevent_members():
    """☠️ UI 里引用的每个 `QEvent.X` 都必须真实存在于 PyQt5。

    2026-08-28 加影院条时踩过：`changeEvent` 里写了 `QEvent.ScreenChangeInternal`
    ——那是 Qt 的**内部**枚举，PyQt5 根本没暴露（`dir(QEvent)` 里一个带 screen
    的都没有）。于是每次窗口构造时 changeEvent 一被调用就 AttributeError，而
    **PyQt5 对虚函数重写里的未捕获异常是直接 abort() 整个进程**，不是往上抛。

    现场表现极具迷惑性：`pytest -q` 跑到 90% 直接消失，没有 FAILED、没有
    traceback、没有 short test summary，退出码 127（看着像"命令没找到"）。
    单独构造那个窗口同样是秒退无输出。查这种问题只能靠逐行 print 二分。

    所以这条防线是**源码扫描**而不是行为测试：行为测试要真触发那条事件分支
    才炸，而它平时不触发；源码扫描则是写下去就红。
    """
    ui_dir = Path(__file__).resolve().parent.parent / "realtime_subtitle" / "ui"
    offenders = []
    for path in sorted(ui_dir.glob("*.py")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            code = line.split("#", 1)[0]  # 注释里可以提这些名字（本条注释就提了）
            for name in re.findall(r"\bQEvent\.(\w+)", code):
                if not hasattr(QEvent, name):
                    offenders.append(f"{path.name}:{lineno} QEvent.{name}")
    assert not offenders, (
        "这些 QEvent 成员在 PyQt5 里不存在，虚函数里访问会 abort 整个进程："
        + ", ".join(offenders))


# ---------------------------------------------------------------------------
# 内容语义
# ---------------------------------------------------------------------------

def test_hidden_bar_drops_content():
    """藏着时不攒内容：打开时由 _toggle_cinema 灌最后一句。"""
    _app()
    bar = CinemaBar()  # 没 show
    bar.append_pair("Hallo", "你好")
    bar.update_draft("草稿")
    assert bar.chinese_label.text() == ""
    assert bar.german_label.text() == ""


def test_append_pair_shows_only_latest():
    """☠️ 影院条是"只显示最新一句"，不是滚动流——多来几句只留最后一句。

    在视频上叠多行会挡画面，而挡画面正是这个条要解决的问题。要回看历史用
    📜 历史窗或 📺 电视窗。
    """
    bar = _shown_bar()
    bar.append_pair("Erster Satz.", "第一句。")
    bar.append_pair("Zweiter Satz.", "第二句。")
    assert bar.chinese_label.text() == "第二句。"
    assert bar.german_label.text() == "Zweiter Satz."


def test_blank_chinese_is_ignored():
    bar = _shown_bar()
    bar.append_pair("Hallo", "你好")
    bar.append_pair("Leer", "   ")
    assert bar.chinese_label.text() == "你好", "空翻译不该把上一句冲掉"


def test_draft_replaces_chinese_but_keeps_german():
    """☠️ 草稿只换中文行，德语行必须保持。

    草稿退场的正常路径是"正式翻译到了"，紧接着 append_pair 会把两行都重写；
    中间那一拍如果把德语也清掉，画面上会闪一下。
    """
    bar = _shown_bar()
    bar.append_pair("Das ist ein Test.", "这是一个测试。")
    bar.update_draft("这是一个…")
    assert bar.chinese_label.text() == "这是一个…"
    assert bar.german_label.text() == "Das ist ein Test.", "草稿不该动德语行"
    assert bar.chinese_label.property("draft") is True

    bar.append_pair("Das ist ein Test.", "这是一个测试。")
    assert bar.chinese_label.property("draft") is False, "正式translation要清掉草稿态"


def test_show_german_false_hides_german_line():
    """CINEMA_SHOW_GERMAN=False = 纯中文，最不挡画面。"""
    bar = _shown_bar()
    orig = config.CINEMA_SHOW_GERMAN
    try:
        config.CINEMA_SHOW_GERMAN = False
        bar.append_pair("Guten Tag", "你好")
        assert bar.chinese_label.text() == "你好"
        assert bar.german_label.text() == ""
        assert not bar.german_label.isVisible(), "空德语行不该占位显示空底衬"
    finally:
        config.CINEMA_SHOW_GERMAN = orig


def test_clear_text_empties_both_lines():
    bar = _shown_bar()
    bar.append_pair("Hallo", "你好")
    bar.clear_text()
    assert bar.chinese_label.text() == ""
    assert bar.german_label.text() == ""


def test_hold_timer_restarts_on_each_line():
    """静默淡出计时器每来一句就重置；CINEMA_HOLD_SEC=0 表示不自动隐藏。"""
    bar = _shown_bar()
    orig = config.CINEMA_HOLD_SEC
    try:
        config.CINEMA_HOLD_SEC = 5.0
        bar.append_pair("Hallo", "你好")
        assert bar._hold_timer.isActive()

        config.CINEMA_HOLD_SEC = 0
        bar.clear_text()
        bar.append_pair("Hallo", "你好")
        assert not bar._hold_timer.isActive(), "0 = 不自动隐藏，不该起计时器"
    finally:
        config.CINEMA_HOLD_SEC = orig


# ---------------------------------------------------------------------------
# 字号 / 屏幕
# ---------------------------------------------------------------------------

def test_adjust_font_clamps_and_syncs_config():
    bar = _shown_bar()
    orig = config.CINEMA_FONT_SIZE
    try:
        config.CINEMA_FONT_SIZE = config.CINEMA_FONT_SIZE_MAX
        bar.adjust_font(+1)
        assert config.CINEMA_FONT_SIZE == config.CINEMA_FONT_SIZE_MAX, "上限要钳住"

        config.CINEMA_FONT_SIZE = config.CINEMA_FONT_SIZE_MIN
        bar.adjust_font(-1)
        assert config.CINEMA_FONT_SIZE == config.CINEMA_FONT_SIZE_MIN, "下限要钳住"

        config.CINEMA_FONT_SIZE = 34
        bar.adjust_font(+1)
        assert config.CINEMA_FONT_SIZE == 36, "一格 2px"
    finally:
        config.CINEMA_FONT_SIZE = orig


def test_clamp_screen_index():
    _app()
    bar = CinemaBar()
    n = len(QApplication.screens())
    assert bar._clamp_screen_index(0) == 0
    assert bar._clamp_screen_index(-3) == 0
    assert bar._clamp_screen_index(n + 5) == n - 1  # 拔了屏的持久化值钳回
    assert bar._clamp_screen_index(None) == 0


# ---------------------------------------------------------------------------
# ⚙️ 面板接线
# ---------------------------------------------------------------------------

def _panel_with_bar():
    """⚙️ 面板 + 挂好的影院条（真实里由 SubtitleWindow.__init__ 回挂）。"""
    from realtime_subtitle.ui.settings_window import SettingsWindow
    _app()
    win = SettingsWindow()
    bar = CinemaBar()
    bar.resize(600, 200)
    bar.show()
    win._cinema_bar = bar
    return win, bar


def test_cinema_controls_do_not_drop_out_of_preset():
    """☠️ 调影院条**不该**把左上角指示器打成「⚙️ 自定义」。

    PRESETS 里没有任何一个模式定义 CINEMA_* 键，所以调字幕条字号并没有偏离
    当前模式。而模式身份一掉就再也回不去（面板本来就有的痛点），别再加新的
    触发点——对照组是 test_presets 里那条：拨 idle_flush_slider 必须掉出模式。
    """
    win, _bar = _panel_with_bar()
    orig = (config.CINEMA_FONT_SIZE, config.CINEMA_BG_ALPHA, config.CINEMA_SHOW_GERMAN)
    try:
        win.restore_active_preset("看剧")
        assert win._active_preset == "看剧"

        info = win.cinema_font_slider
        info["slider"].setValue(round(40 / info["step"]))
        assert config.CINEMA_FONT_SIZE == 40, "值要真写进 config"
        assert win._active_preset == "看剧", "调字号不该掉出「看剧」"

        info = win.cinema_alpha_slider
        info["slider"].setValue(round(200 / info["step"]))
        assert config.CINEMA_BG_ALPHA == 200
        assert win._active_preset == "看剧", "调底衬不该掉出「看剧」"

        win.cinema_german_cb.setChecked(False)
        assert config.CINEMA_SHOW_GERMAN is False
        assert win._active_preset == "看剧", "切德语行不该掉出「看剧」"
    finally:
        (config.CINEMA_FONT_SIZE, config.CINEMA_BG_ALPHA,
         config.CINEMA_SHOW_GERMAN) = orig


def test_panel_pushes_style_to_the_bar_live():
    """面板一动，字幕条当场重排——不用重启也不用等下一句。"""
    win, bar = _panel_with_bar()
    orig = config.CINEMA_FONT_SIZE
    try:
        bar.append_pair("Hallo", "你好")
        info = win.cinema_font_slider
        info["slider"].setValue(round(48 / info["step"]))
        assert "48px" in bar.chinese_label.styleSheet(), "字号没推到样式表"
    finally:
        config.CINEMA_FONT_SIZE = orig


def test_hiding_german_clears_the_stale_german_line():
    """☠️ 关掉德语行要连当前内容一起清。

    否则关掉之后**上一行德语还留在屏幕上**，要等下一句才消失——用户会以为
    开关没生效。
    """
    win, bar = _panel_with_bar()
    orig = config.CINEMA_SHOW_GERMAN
    try:
        bar.append_pair("Guten Abend", "晚上好")
        assert bar.german_label.text() == "Guten Abend"
        win.cinema_german_cb.setChecked(False)
        assert bar.german_label.text() == "", "残留的德语行没清掉"
    finally:
        config.CINEMA_SHOW_GERMAN = orig
        win.cinema_german_cb.setChecked(orig)


# ---------------------------------------------------------------------------
# 与主窗接线
# ---------------------------------------------------------------------------

def test_subtitle_window_wires_cinema_bar_and_persists_style():
    """SubtitleWindow 建出 cinema_bar；state 有 cinema 段；_add_pair 接线到位。

    ☠️ state 里**不存"上次是不是开着"**：影院条是跟着"我现在要全屏看剧"这个
    当下意图开的，重启后默认关着才对（开着会挡住下一次的普通使用）。
    """
    import os
    import json
    import tempfile
    import realtime_subtitle.ui.subtitle_window as sw_mod
    from realtime_subtitle.ui.subtitle_window import SubtitleWindow

    _app()
    tmpdir = tempfile.mkdtemp()
    orig_state = sw_mod.STATE_FILE
    sw_mod.STATE_FILE = os.path.join(tmpdir, "window_state.json")
    try:
        win = SubtitleWindow()
        assert isinstance(win.cinema_bar, CinemaBar)
        assert not win.cinema_bar.isVisible(), "初始必须是隐藏的"

        # 接线：_add_pair 要喂到影院条（德语+中文两个参数，和 tv_window 不同）
        win.cinema_bar.show()
        win._add_pair("Ein Satz.", "一句话。")
        assert win.cinema_bar.chinese_label.text() == "一句话。"
        assert win.cinema_bar.german_label.text() == "Ein Satz."
        win.cinema_bar.hide()

        win._save_state_if_changed()
        with open(sw_mod.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        # ☠️ 样式三项走 tuning（唯一数据源），state["cinema"] 只剩 screen_index
        assert set(state["cinema"]) == {"screen_index"}
        assert "visible" not in state["cinema"], "别持久化开关态，理由见 docstring"
        for key in ("CINEMA_FONT_SIZE", "CINEMA_SHOW_GERMAN", "CINEMA_BG_ALPHA"):
            assert key in state["tuning"], f"{key} 该由 tuning 持久化"
            assert key not in state["cinema"], f"{key} 存了两份，迟早漂"
        win.container.close()
    finally:
        sw_mod.STATE_FILE = orig_state
