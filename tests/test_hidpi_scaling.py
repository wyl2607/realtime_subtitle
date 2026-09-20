"""HiDPI：缩放倍率的语义，以及存档跨坐标空间的换算。

PyQt6 迁移第 2 步留下的东西。核心事实（本机实测，见下面的参数化用例）：

    Qt5 @100%   logicalDPI 96    DPR 1.0   screen_scale_factor() = 1.00
    Qt5 @150%   logicalDPI 144   DPR 1.0   screen_scale_factor() = 1.50  ← 我们乘
    Qt6 @150%   logicalDPI 96    DPR 1.5   screen_scale_factor() = 1.00  ← Qt 乘

也就是说 `screen_scale_factor()` 在 Qt6 下**自己就归 1**，不会双重缩放，
这个公式不需要为迁移改动。真正会坏的是存盘的几何和像素字号：Qt5 存的是
物理像素，Qt6 会把同样的数字当逻辑像素用。
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from realtime_subtitle.ui.window_geometry import rescale_state_for_dpr

# 用 PyQt5 开/关 AA_EnableHighDpiScaling 来模拟 Qt6 的行为：这两种模式下
# Qt 报告 DPI 的方式，正是 Qt5 和 Qt6 的差别所在。QT_FONT_DPI / QT_SCALE_FACTOR
# 让本机（100% 屏）也能测出 150% 屏的表现。
_PROBE = textwrap.dedent("""
    import json, sys
    from PyQt5.QtCore import Qt, QCoreApplication
    if sys.argv[1] == "on":
        QCoreApplication.setAttribute(Qt.ApplicationAttribute.AA_EnableHighDpiScaling, True)
    from PyQt5.QtWidgets import QApplication
    app = QApplication([])
    screen = app.primaryScreen()
    print(json.dumps({"logical_dpi": screen.logicalDotsPerInch(),
                      "dpr": screen.devicePixelRatio()}))
""")


def _probe(hidpi, env_extra):
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE, hidpi],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **env_extra}, timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"子进程起不了 QApplication：{proc.stderr.strip()[-200:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("label, hidpi, env, want_factor, want_dpr", [
    ("Qt5 @100%", "off", {}, 1.0, 1.0),
    ("Qt5 @150%", "off", {"QT_FONT_DPI": "144"}, 1.5, 1.0),
    ("Qt6 @150%", "on", {"QT_SCALE_FACTOR": "1.5"}, 1.0, 1.5),
])
def test_scale_factor_semantics(label, hidpi, env, want_factor, want_dpr):
    """☠️ 这三行是整个迁移设计的地基：Qt 接管缩放后我们这边必须自动归 1。"""
    got = _probe(hidpi, env)
    factor = min(max(got["logical_dpi"] / 96.0, 1.0), 2.0)
    assert factor == pytest.approx(want_factor, abs=0.01), f"{label}: {got}"
    assert got["dpr"] == pytest.approx(want_dpr, abs=0.01), f"{label}: {got}"


# --- 存档换算 -----------------------------------------------------------

_STATE = {
    "x": 1356, "y": 1085, "w": 1054, "h": 307,
    "font_size": 24, "bg_opacity": 235,
    "settings_geo": [1400, 207, 894, 1160],
    "tv": {"font_size": 64, "screen_index": 0},
    "tuning": {"CINEMA_FONT_SIZE": 34, "ENERGY_THRESHOLD_SPEECH": 0.01},
}


def test_same_dpr_is_a_noop():
    assert rescale_state_for_dpr(dict(_STATE), 1.0) == _STATE


def test_legacy_state_without_field_is_treated_as_dpr_1():
    """老存档没有 coord_dpr。Windows 上不开 HiDPI 时 DPR 恒为 1.0，假设成立。"""
    out = rescale_state_for_dpr(dict(_STATE), 1.5)
    assert out["w"] == round(1054 / 1.5)
    assert out["coord_dpr"] == 1.5


def test_geometry_and_pixel_fonts_scale_together():
    out = rescale_state_for_dpr(dict(_STATE), 1.5)
    assert out["x"] == round(1356 / 1.5)
    assert out["h"] == round(307 / 1.5)
    assert out["font_size"] == round(24 / 1.5)
    assert out["settings_geo"] == [round(v / 1.5) for v in _STATE["settings_geo"]]
    assert out["tv"]["font_size"] == round(64 / 1.5)
    assert out["tuning"]["CINEMA_FONT_SIZE"] == round(34 / 1.5)


def test_non_pixel_values_are_left_alone():
    """☠️ tuning 里绝大多数值不是像素，一起乘会把用户的调参毁掉。"""
    out = rescale_state_for_dpr(dict(_STATE), 1.5)
    assert out["tuning"]["ENERGY_THRESHOLD_SPEECH"] == 0.01
    assert out["bg_opacity"] == 235
    assert out["tv"]["screen_index"] == 0


def test_negative_positions_survive():
    """多屏时左边/上方那块屏的 x/y 是负数，不能被当成尺寸夹成 1。"""
    out = rescale_state_for_dpr({"x": -1920, "y": -300, "w": 800, "h": 200}, 2.0)
    assert out["x"] == -960 and out["y"] == -150
    assert out["w"] == 400 and out["h"] == 100


def test_round_trip_back_to_original_space():
    """Qt5 → Qt6 → 再回 Qt5，不该越算越小。"""
    up = rescale_state_for_dpr(dict(_STATE), 1.5)
    back = rescale_state_for_dpr(up, 1.0)
    for key in ("w", "h", "font_size"):
        assert back[key] == pytest.approx(_STATE[key], abs=1)


def test_bad_values_are_left_for_existing_int_guards():
    """坏值不在这里抛，交给 _restore_main_geo 那套 int() 容错。"""
    out = rescale_state_for_dpr({"x": "oops", "w": None, "h": 200}, 2.0)
    assert out["x"] == "oops" and out["w"] is None and out["h"] == 100


def test_non_dict_state_passes_through():
    assert rescale_state_for_dpr(None, 2.0) is None
    assert rescale_state_for_dpr([1, 2], 2.0) == [1, 2]
