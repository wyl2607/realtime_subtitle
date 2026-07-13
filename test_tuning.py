"""面板自定义设置 + tuning 持久化单测。

运行: venv\\Scripts\\python.exe -m pytest test_tuning.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114。
⚠️ QApplication 必须持有模块级引用，否则被 GC 后建 QWidget 触发 qFatal。
"""
import torch  # noqa: F401  先于 PyQt5
import sys
import json
import os

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication

import config
from subtitle_window import (
    SettingsWindow,
    TUNING_KEYS,
    apply_tuning,
    collect_tuning,
    apply_text_color,
    snapshot_defaults,
)


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


def _snap_keys(*keys):
    return {k: getattr(config, k) for k in keys}


def _restore(snap):
    for k, v in snap.items():
        setattr(config, k, v)


# ---------------------------------------------------------------------------
# tuning round-trip
# ---------------------------------------------------------------------------
def test_apply_tuning_writes_config_values():
    keys = list(TUNING_KEYS)
    snap = _snap_keys(*keys)
    try:
        fake = {
            "CHUNK_SUBMIT_SECONDS": 1.1,
            "BUFFER_TRIM_SEC": 15.0,
            "IDLE_FLUSH_SEC": 3.0,
            "ENERGY_THRESHOLD_SPEECH": 0.02,
            "MAX_SUBTITLE_LENGTH": 200,
            "MAX_SENTENCE_PAIRS": 7,
            "LOOPBACK_DEVICE_NAME": "FiiO",
            "SHOW_BILINGUAL": False,
            "DRAFT_TRANSLATION": False,
            "CHINESE_TEXT_COLOR": "#aabbcc",
            "DRAFT_TEXT_COLOR": "#112233",
            "UNSTABLE_TEXT_COLOR": "#445566",
            "FONT_FAMILY": "Consolas, Arial",
        }
        apply_tuning(fake)
        for k, v in fake.items():
            assert getattr(config, k) == v, f"{k}: {getattr(config, k)!r} != {v!r}"
    finally:
        _restore(snap)


def test_apply_tuning_skips_bad_values():
    snap = _snap_keys("CHUNK_SUBMIT_SECONDS", "SHOW_BILINGUAL")
    try:
        good = config.CHUNK_SUBMIT_SECONDS
        apply_tuning({"CHUNK_SUBMIT_SECONDS": "not-a-number", "SHOW_BILINGUAL": False})
        assert config.CHUNK_SUBMIT_SECONDS == good  # 坏值跳过
        assert config.SHOW_BILINGUAL is False
    finally:
        _restore(snap)


def test_collect_tuning_complete_and_game_mode_exempt():
    keys = list(TUNING_KEYS)
    snap = _snap_keys(*keys)
    try:
        config.CHUNK_SUBMIT_SECONDS = 0.7
        config.DRAFT_TRANSLATION = True
        config.SHOW_BILINGUAL = False
        full = collect_tuning()
        assert set(full.keys()) == set(TUNING_KEYS)
        assert full["CHUNK_SUBMIT_SECONDS"] == 0.7
        assert full["DRAFT_TRANSLATION"] is True
        assert full["SHOW_BILINGUAL"] is False

        # 模拟游戏模式：config 被热键改成临时值
        config.CHUNK_SUBMIT_SECONDS = 1.0
        config.DRAFT_TRANSLATION = False
        prev = {"CHUNK_SUBMIT_SECONDS": 0.7, "DRAFT_TRANSLATION": True}
        exempt = collect_tuning(game_mode_active=True, previous_tuning=prev)
        assert exempt["CHUNK_SUBMIT_SECONDS"] == 0.7
        assert exempt["DRAFT_TRANSLATION"] is True
        # 其它键仍取当前 config
        assert exempt["SHOW_BILINGUAL"] is False

        # 无旧值则跳过这两个键
        partial = collect_tuning(game_mode_active=True, previous_tuning={})
        assert "CHUNK_SUBMIT_SECONDS" not in partial
        assert "DRAFT_TRANSLATION" not in partial
    finally:
        _restore(snap)


def test_tuning_roundtrip_via_state_file(tmp_path, monkeypatch):
    """伪造 state → 应用；改 config → 组装 state → 断言完整。绝不写真实 window_state.json。"""
    import subtitle_window as sw

    state_path = tmp_path / "window_state.json"
    monkeypatch.setattr(sw, "STATE_FILE", str(state_path))

    keys = list(TUNING_KEYS)
    snap = _snap_keys(*keys)
    try:
        fake_tuning = {
            "CHUNK_SUBMIT_SECONDS": 0.9,
            "BUFFER_TRIM_SEC": 14.0,
            "IDLE_FLUSH_SEC": 2.5,
            "ENERGY_THRESHOLD_SPEECH": 0.015,
            "MAX_SUBTITLE_LENGTH": 300,
            "MAX_SENTENCE_PAIRS": 9,
            "LOOPBACK_DEVICE_NAME": "Speakers",
            "SHOW_BILINGUAL": False,
            "DRAFT_TRANSLATION": True,
            "CHINESE_TEXT_COLOR": "#d0d0d0",
            "DRAFT_TEXT_COLOR": "#90b0d0",
            "UNSTABLE_TEXT_COLOR": "#888888",
            "FONT_FAMILY": "Segoe UI, Arial",
        }
        # 正向：state dict → apply
        apply_tuning(fake_tuning)
        for k, v in fake_tuning.items():
            assert getattr(config, k) == v

        # 反向：改 config → 组装
        config.MAX_SENTENCE_PAIRS = 11
        assembled = {"tuning": collect_tuning()}
        assert assembled["tuning"]["MAX_SENTENCE_PAIRS"] == 11
        assert set(assembled["tuning"].keys()) == set(TUNING_KEYS)

        # 写临时文件再读回
        with open(state_path, "w", encoding="utf-8") as f:
            json.dump(assembled, f)
        loaded = sw.SubtitleWindow._load_state()
        assert loaded["tuning"]["MAX_SENTENCE_PAIRS"] == 11
        # 确认没动到仓库里的真文件
        real = os.path.join(os.path.dirname(os.path.abspath(sw.__file__)), "window_state.json")
        assert os.path.normpath(str(state_path)) != os.path.normpath(real)
    finally:
        _restore(snap)


# ---------------------------------------------------------------------------
# checkbox / game mode / reset / color
# ---------------------------------------------------------------------------
def test_chinese_only_and_draft_checkboxes_sync_config():
    _app()
    snap = _snap_keys("SHOW_BILINGUAL", "DRAFT_TRANSLATION")
    try:
        config.SHOW_BILINGUAL = True
        config.DRAFT_TRANSLATION = True
        win = SettingsWindow()

        # 勾「只显中文」→ SHOW_BILINGUAL=False
        win.chinese_only_cb.setChecked(True)
        assert config.SHOW_BILINGUAL is False
        win.chinese_only_cb.setChecked(False)
        assert config.SHOW_BILINGUAL is True

        win.draft_cb.setChecked(False)
        assert config.DRAFT_TRANSLATION is False
        win.draft_cb.setChecked(True)
        assert config.DRAFT_TRANSLATION is True

        # refresh 反向同步
        config.SHOW_BILINGUAL = False
        config.DRAFT_TRANSLATION = False
        win.refresh_from_config()
        assert win.chinese_only_cb.isChecked() is True
        assert win.draft_cb.isChecked() is False
        # blockSignals：refresh 不该把值写乱
        assert config.SHOW_BILINGUAL is False
        assert config.DRAFT_TRANSLATION is False
    finally:
        _restore(snap)


def test_set_game_mode_disables_draft_cb_and_chunk_slider():
    _app()
    win = SettingsWindow()
    assert win.chunk_submit_slider["slider"].isEnabled()
    assert win.draft_cb.isEnabled()

    win.set_game_mode(True)
    assert not win.chunk_submit_slider["slider"].isEnabled()
    assert not win.draft_cb.isEnabled()
    assert "游戏模式" in (win.chunk_submit_slider["slider"].toolTip() or "")
    assert "游戏模式" in (win.draft_cb.toolTip() or "")

    win.set_game_mode(False)
    assert win.chunk_submit_slider["slider"].isEnabled()
    assert win.draft_cb.isEnabled()


def test_reset_defaults_restores_passed_snapshot():
    _app()
    keys = list(TUNING_KEYS) + ["FONT_SIZE", "BACKGROUND_OPACITY"]
    snap = _snap_keys(*keys)
    try:
        factory = snapshot_defaults()
        # 改乱若干值
        config.CHUNK_SUBMIT_SECONDS = 1.7
        config.SHOW_BILINGUAL = False
        config.DRAFT_TRANSLATION = False
        config.CHINESE_TEXT_COLOR = "#010101"
        config.FONT_FAMILY = "Comic Sans MS, Arial"
        config.FONT_SIZE = 30
        config.MAX_SENTENCE_PAIRS = 3

        win = SettingsWindow(defaults=factory)
        # 面板初值来自当前（已改乱的）config；恢复应回到 factory
        win._reset_defaults()

        assert config.CHUNK_SUBMIT_SECONDS == factory["CHUNK_SUBMIT_SECONDS"]
        assert config.SHOW_BILINGUAL == factory["SHOW_BILINGUAL"]
        assert config.DRAFT_TRANSLATION == factory["DRAFT_TRANSLATION"]
        assert config.CHINESE_TEXT_COLOR == factory["CHINESE_TEXT_COLOR"]
        assert config.FONT_FAMILY == factory["FONT_FAMILY"]
        assert config.FONT_SIZE == factory["FONT_SIZE"]
        assert config.MAX_SENTENCE_PAIRS == factory["MAX_SENTENCE_PAIRS"]
        # 控件勾选态一致
        assert win.chinese_only_cb.isChecked() == (not factory["SHOW_BILINGUAL"])
        assert win.draft_cb.isChecked() == bool(factory["DRAFT_TRANSLATION"])
    finally:
        _restore(snap)


def test_apply_color_writes_config_without_dialog():
    _app()
    snap = _snap_keys("CHINESE_TEXT_COLOR", "DRAFT_TEXT_COLOR", "UNSTABLE_TEXT_COLOR")
    try:
        # 模块级可测函数
        assert apply_text_color("CHINESE_TEXT_COLOR", "#AbCdEf") == "#abcdef"
        assert config.CHINESE_TEXT_COLOR == "#abcdef"

        # 3 位缩写扩展
        assert apply_text_color("DRAFT_TEXT_COLOR", "#0f8") == "#00ff88"
        assert config.DRAFT_TEXT_COLOR == "#00ff88"

        win = SettingsWindow()
        out = win.apply_color("UNSTABLE_TEXT_COLOR", "#123456")
        assert out == "#123456"
        assert config.UNSTABLE_TEXT_COLOR == "#123456"
    finally:
        _restore(snap)


def test_pair_html_uses_chinese_text_color():
    """_pair_html 读 config.CHINESE_TEXT_COLOR，不再硬编码 #c8c8c8。"""
    _app()
    from subtitle_window import SubtitleWindow

    snap = _snap_keys("CHINESE_TEXT_COLOR", "SHOW_BILINGUAL")
    try:
        config.SHOW_BILINGUAL = True
        config.CHINESE_TEXT_COLOR = "#ff00aa"
        # 不建完整窗口，直接调静态逻辑：需要实例以调 _clip/_pair_html
        # 用 SettingsWindow 无关；构造最小 stub
        class _Stub:
            def _clip(self, t):
                return SubtitleWindow._clip(t)

            def _pair_html(self, g, c):
                return SubtitleWindow._pair_html(self, g, c)

        html = _Stub()._pair_html("Hallo", "你好")
        assert "#ff00aa" in html
        assert "你好" in html
        assert "Hallo" in html
    finally:
        _restore(snap)


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-q"]))
