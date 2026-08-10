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

import realtime_subtitle.config as config
from realtime_subtitle.ui.subtitle_window import (
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


def test_collect_tuning_persists_current_values():
    """模式值就是用户当前状态，一律照常持久化。

    以前这里有"游戏模式期间豁免 CHUNK_SUBMIT_SECONDS/DRAFT_TRANSLATION"的分支，
    那是"临时开关+退出还原"语义的产物；模式系统合并后四个模式平等并列，
    留着豁免会让重启后面板值和恢复出来的模式高亮对不上。
    """
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

        # 「性能」模式把值改成 1.0/False → 存的就是这个，不再豁免
        config.CHUNK_SUBMIT_SECONDS = 1.0
        config.DRAFT_TRANSLATION = False
        after = collect_tuning()
        assert after["CHUNK_SUBMIT_SECONDS"] == 1.0
        assert after["DRAFT_TRANSLATION"] is False
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


def test_state_file_atomic_write_and_bak_recovery(tmp_path, monkeypatch):
    """强杀撞上写盘会留半截 JSON → 布局/字号/tuning 全丢。现在原子写 + .bak 兜底。

    ☠️ 这里 patch 的是 subtitle_window 模块的 STATE_FILE 属性——持久化函数
    必须留在那个模块里，搬走就会读到别的模块的全局，patch 失效（见文件头注释）。
    """
    import subtitle_window as sw

    state_path = tmp_path / "window_state.json"
    monkeypatch.setattr(sw, "STATE_FILE", str(state_path))

    good = {"x": 1, "y": 2, "w": 300, "h": 200}
    # 走真实写入路径（_save_state_if_changed 内部就是调这个）
    sw._atomic_write_json(str(state_path), good)
    sw._atomic_write_json(str(state_path), {"x": 9, "y": 9, "w": 300, "h": 200})
    assert os.path.exists(str(state_path) + ".bak"), "上一份必须留成 .bak"
    assert not os.path.exists(str(state_path) + ".tmp"), ".tmp 不该残留"

    # 主文件被写坏（模拟强杀撞上写入）→ 从 .bak 恢复上一份
    with open(state_path, "w", encoding="utf-8") as f:
        f.write('{"x": 9, "y":')  # 半截 JSON
    loaded = sw.SubtitleWindow._load_state()
    assert loaded == good, loaded

    # 主文件和 .bak 都没有 → 空 dict（退回出厂布局，不抛）
    os.remove(str(state_path))
    os.remove(str(state_path) + ".bak")
    assert sw.SubtitleWindow._load_state() == {}


def test_main_geo_restore_rejects_bad_values():
    """☠️ window_state.json 里的窗口坐标必须 int() 容错。

    以前这四个值直接取原值（同函数里的 font_size/bg_opacity 一直都有 int()
    保护，唯独坐标没有）。"abc" / null / 浮点数都会让
    _clamp_geo_to_any_screen 抛 TypeError，__init__ 失败，main.py 捕获后
    sys.exit(1)——悬浮窗根本不出现。.bak 兜底在这里帮不上忙：文件本身是
    合法 dict，坏的只是里面某个值。
    """
    import subtitle_window as sw

    good = {"x": 120, "y": 240, "w": 800, "h": 300}
    assert sw.SubtitleWindow._restore_main_geo(good) == (120, 240, 800, 300)
    # 字符串数字照收（JSON 手改后常见）
    assert sw.SubtitleWindow._restore_main_geo(
        {"x": "10", "y": "20", "w": "600", "h": "200"}) == (10, 20, 600, 200)

    fallback = sw.SubtitleWindow._default_main_geo()
    for bad in ({"x": "abc", "y": 0, "w": 600, "h": 200},
                {"x": None, "y": 0, "w": 600, "h": 200},
                {"x": 0, "y": 0, "w": [], "h": 200},
                {"x": 0, "y": 0, "w": 600}):            # 缺键
        got = sw.SubtitleWindow._restore_main_geo(bad)
        assert got == fallback, f"{bad} 应退回默认布局，实际 {got}"
        assert all(isinstance(v, int) for v in got), got

    # 浮点：_clamp_geo_to_any_screen 里 QPoint(float) 会抛，必须先截成 int
    assert sw.SubtitleWindow._restore_main_geo(
        {"x": 10.5, "y": 20.9, "w": 600.1, "h": 200.7}) == (10, 20, 600, 200)


def test_state_file_non_dict_json_falls_back_to_bak(tmp_path, monkeypatch):
    """☠️ null / [] / "x" / 123 都是【合法】JSON，json.load 不抛 ValueError。

    以前 _load_state 会把它们原样返回，__init__ 紧接着 self._state.get(...)
    就抛 AttributeError，main.py 捕获后 sys.exit(1)——悬浮窗根本不出现。
    更要命的是这类损坏【绕过了 .bak 兜底】，而那套兜底的全部意义就是扛住
    state 文件损坏。现在非 dict 一律当损坏，继续找下一份。
    """
    import subtitle_window as sw

    state_path = tmp_path / "window_state.json"
    monkeypatch.setattr(sw, "STATE_FILE", str(state_path))

    good = {"x": 1, "y": 2, "w": 300, "h": 200}
    sw._atomic_write_json(str(state_path), good)
    sw._atomic_write_json(str(state_path), {"x": 9, "y": 9, "w": 300, "h": 200})

    for bad in ("null", "[]", '"hello"', "123", "true"):
        with open(state_path, "w", encoding="utf-8") as f:
            f.write(bad)
        loaded = sw.SubtitleWindow._load_state()
        assert isinstance(loaded, dict), f"{bad} 应该被当成损坏，实际返回 {loaded!r}"
        assert loaded == good, f"{bad} 时应从 .bak 恢复，实际 {loaded!r}"

    # 主文件和 .bak 都是非 dict → 空 dict，绝不能抛
    with open(state_path, "w", encoding="utf-8") as f:
        f.write("null")
    with open(str(state_path) + ".bak", "w", encoding="utf-8") as f:
        f.write("[]")
    assert sw.SubtitleWindow._load_state() == {}


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
    from realtime_subtitle.ui.subtitle_window import SubtitleWindow

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
