"""📺 电视全屏模式单测：句对追加/草稿替换/字号钳制/屏幕索引解析。

运行: venv\\Scripts\\python.exe -m pytest test_tv_window.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114（见 main.py / test_hittest.py）。
⚠️ QApplication 必须持有模块级引用，否则会被立即 GC → 建 QWidget 时 qFatal 秒退。
"""
import torch  # noqa: F401  先于 PyQt5
import sys

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication

import config
from tv_window import TVWindow


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _shown_tv():
    """建一个普通 show（非全屏）的 TVWindow：append/draft 有 isVisible 门控。"""
    _app()
    win = TVWindow()
    win.resize(400, 300)
    win.show()
    return win


def test_append_pair_appends_blocks():
    win = _shown_tv()
    try:
        win.append_pair("第一句")
        win.append_pair("第二句")
        assert win.text.toPlainText() == "第一句\n第二句"
    finally:
        win.hide()


def test_empty_or_blank_chinese_is_ignored():
    win = _shown_tv()
    try:
        win.append_pair("")
        win.append_pair("   ")
        win.append_pair(None)
        assert win.text.toPlainText() == ""
    finally:
        win.hide()


def test_draft_occupies_last_block_and_gets_replaced_by_pair():
    win = _shown_tv()
    try:
        win.append_pair("正式一")
        win.update_draft("草稿中")
        assert win.text.toPlainText() == "正式一\n草稿中"
        # 草稿只更新不叠加
        win.update_draft("草稿更长了")
        assert win.text.toPlainText() == "正式一\n草稿更长了"
        # 正式句对到达：草稿退场，被正式文本替换
        win.append_pair("正式二")
        assert win.text.toPlainText() == "正式一\n正式二"
        # 空草稿 = 清掉草稿行
        win.update_draft("又一条草稿")
        win.update_draft("")
        assert win.text.toPlainText() == "正式一\n正式二"
    finally:
        win.hide()


def test_hidden_window_drops_content():
    _app()
    win = TVWindow()  # 不 show
    win.append_pair("看不见就别攒")
    win.update_draft("草稿也别攒")
    assert win.text.toPlainText() == ""


def test_backfill_replaces_document():
    win = _shown_tv()
    try:
        win.append_pair("旧的")
        win.backfill(["一", "二", "三"])
        assert win.text.toPlainText() == "一\n二\n三"
    finally:
        win.hide()


def test_warm_up_leaves_window_hidden_with_full_opacity():
    """预热首次全屏转场开销（真实开销见调试记录：进程内第一次 showFullScreen()
    要吃掉 400-500ms，opacity=0 时用户看不到）；预热完必须恢复隐藏+不透明，
    不能让用户下次看到一个残留可见或半透明的窗口。"""
    _app()
    win = TVWindow()
    win.warm_up()
    assert win.isHidden()
    assert win.windowOpacity() == 1.0


def test_bottom_anchor_pads_sparse_content_to_viewport_bottom():
    """内容不够填满一屏时，顶部留白把文字推到贴近底部（"从下往上滚动"的观感）；
    留白必须 > 0 但不能真的把内容推出可视区（pad 应严格小于 viewport 高度）。"""
    win = _shown_tv()
    try:
        win.resize(400, 600)
        win.append_pair("只有一行")
        pad = win.text.document().rootFrame().frameFormat().topMargin()
        viewport_h = win.text.viewport().height()
        # 默认文档边距只有几像素；真正生效的留白应占掉视口大半高度
        assert viewport_h * 0.5 < pad < viewport_h
    finally:
        win.hide()


def test_bottom_anchor_clears_once_content_fills_viewport():
    """内容一旦超过一屏高度，顶部留白必须归零，让出空间给正常滚动接管
    （留白不清零会让 scrollbar 的 maximum 算错，滚不到真正的底部）。"""
    win = _shown_tv()
    try:
        win.resize(400, 200)  # 矮窗口，几行就能撑满
        for i in range(20):
            win.append_pair(f"第{i}行足够长的中文字幕内容撑满窗口宽度测试用")
        pad = win.text.document().rootFrame().frameFormat().topMargin()
        assert pad == 0
    finally:
        win.hide()


def test_adjust_font_clamps_and_syncs_config():
    _app()
    snap = config.TV_FONT_SIZE
    try:
        win = TVWindow()
        config.TV_FONT_SIZE = config.TV_FONT_SIZE_MAX - 2
        win._adjust_font(+1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MAX  # +4 被钳到上限
        win._adjust_font(+1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MAX  # 不越界
        config.TV_FONT_SIZE = config.TV_FONT_SIZE_MIN + 2
        win._adjust_font(-1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MIN
        win._adjust_font(-1)
        assert config.TV_FONT_SIZE == config.TV_FONT_SIZE_MIN
    finally:
        config.TV_FONT_SIZE = snap


def test_clamp_screen_index():
    _app()
    win = TVWindow()
    n = len(QApplication.screens())
    assert win._clamp_screen_index(0) == 0
    assert win._clamp_screen_index(-3) == 0
    assert win._clamp_screen_index(n + 5) == n - 1  # 拔了屏的持久化值钳回
    assert win._clamp_screen_index(None) == 0


def test_subtitle_window_warms_up_tv_window_at_startup():
    """SubtitleWindow.__init__ 必须预热 tv_window，否则用户第一次点📺就撞上
    进程内首次全屏转场的 400-500ms 卡顿（见 warm_up 的调试记录）。"""
    import os
    import tempfile
    import subtitle_window as sw_mod
    from subtitle_window import SubtitleWindow

    _app()
    tmpdir = tempfile.mkdtemp()
    orig_state = sw_mod.STATE_FILE
    sw_mod.STATE_FILE = os.path.join(tmpdir, "window_state.json")
    orig_warm_up = TVWindow.warm_up
    calls = []
    TVWindow.warm_up = lambda self: calls.append(self)
    try:
        win = SubtitleWindow()
        assert calls == [win.tv_window]
        win.container.close()
    finally:
        sw_mod.STATE_FILE = orig_state
        TVWindow.warm_up = orig_warm_up


def test_subtitle_window_integration_state_and_wiring():
    """SubtitleWindow 建出 tv_window；state 含 tv 段；_add_pair/_update_draft 接线到位。"""
    import os
    import json
    import tempfile
    import subtitle_window as sw_mod
    from subtitle_window import SubtitleWindow

    _app()
    tmpdir = tempfile.mkdtemp()
    orig_state = sw_mod.STATE_FILE
    snap_font = config.TV_FONT_SIZE
    sw_mod.STATE_FILE = os.path.join(tmpdir, "window_state.json")
    try:
        win = SubtitleWindow()
        assert isinstance(win.tv_window, TVWindow)
        assert win.tv_btn.toolTip()  # 📺 按钮存在

        # 接线：句对/草稿都转发到 tv_window（tv 窗显示时）
        win.tv_window.resize(400, 300)
        win.tv_window.show()
        win._add_pair("Hallo", "你好")
        assert "你好" in win.tv_window.text.toPlainText()
        win._update_draft("草稿")
        assert "草稿" in win.tv_window.text.toPlainText()
        win.tv_window.hide()

        # 持久化：tv 段写进 state 文件
        config.TV_FONT_SIZE = 72
        win.tv_window.screen_index = 0
        win._save_state_if_changed()
        with open(sw_mod.STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
        assert state["tv"] == {"font_size": 72, "screen_index": 0}

        win.container.close()
    finally:
        sw_mod.STATE_FILE = orig_state
        config.TV_FONT_SIZE = snap_font


def test_state_restore_tv_font_size():
    """启动时 window_state.json 里的 tv.font_size 恢复进 config。"""
    import os
    import json
    import tempfile
    import subtitle_window as sw_mod
    from subtitle_window import SubtitleWindow

    _app()
    tmpdir = tempfile.mkdtemp()
    orig_state = sw_mod.STATE_FILE
    snap_font = config.TV_FONT_SIZE
    sw_mod.STATE_FILE = os.path.join(tmpdir, "window_state.json")
    try:
        with open(sw_mod.STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"tv": {"font_size": 96, "screen_index": 99}}, f)
        win = SubtitleWindow()
        assert config.TV_FONT_SIZE == 96
        # 越界 screen_index 原样存着，_go_fullscreen 时才钳制（拔屏不崩）
        assert win.tv_window.screen_index == 99
        win.container.close()
    finally:
        sw_mod.STATE_FILE = orig_state
        config.TV_FONT_SIZE = snap_font


def test_main_window_font_cap_raised_to_72():
    """主窗字号上限放宽：滑块最大值 72（Ctrl+滚轮钳制与之共用同一上限，grep 核对）。"""
    from settings_window import SettingsWindow

    _app()
    snap = config.FONT_SIZE
    try:
        win = SettingsWindow()
        s = win.font_size_slider
        assert round(s['slider'].maximum() * s['step']) == 72
    finally:
        config.FONT_SIZE = snap
