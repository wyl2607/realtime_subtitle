"""HiDPI：缩放倍率的语义，以及存档跨坐标空间的换算。

PyQt6 迁移第 2 步留下的东西，第 3 步真把 PyQt6 装进来之后重写过一次。

☠️ **第 2 步写这个文件时机器上还只有 PyQt5**，当时是拿「PyQt5 + 打开
`AA_EnableHighDpiScaling`」去**模拟** Qt6。那个办法现在既不可能也不必要：
`AA_EnableHighDpiScaling` 在 PyQt6 里连枚举成员都不存在了，而 Qt6 的 HiDPI
本来就一直开着——直接量真货就行。

真货量出来的结论和当初模拟的一致（PyQt6 6.11 / Qt 6.11）：

    无环境变量           logicalDPI 96   DPR 1.0   screen_scale_factor() = 1.00
    QT_SCALE_FACTOR=1.5  logicalDPI 96   DPR 1.5   screen_scale_factor() = 1.00
    QT_FONT_DPI=144      logicalDPI 96   DPR 1.5   screen_scale_factor() = 1.00

**Qt6 下缩放只进 DPR，绝不进 logicalDPI**，于是 `screen_scale_factor()` 自己
就归 1、不会双重缩放。⚠️ 这比第 2 步的推断还强一点：连 QT_FONT_DPI 都落进
DPR，Qt6/Windows 上没有让 logicalDPI 偏离基线的路子。

真正会坏的是存盘的几何和像素字号：Qt5 存的是物理像素，Qt6 会把同样的数字
当逻辑像素用——下半个文件测的就是那个换算。
"""
import json
import os
import subprocess
import sys
import textwrap

import pytest

from realtime_subtitle.ui.window_geometry import rescale_state_for_dpr

# 必须开子进程：QT_SCALE_FACTOR 这类只在 QApplication 构造那一刻读一次，
# 而 pytest 进程里早就有一个 QApplication 了（第 4 节第 16 条）。
_PROBE = textwrap.dedent("""
    import json
    from PyQt6.QtWidgets import QApplication
    app = QApplication([])
    screen = app.primaryScreen()
    print(json.dumps({"logical_dpi": screen.logicalDotsPerInch(),
                      "dpr": screen.devicePixelRatio()}))
""")


def _probe(env_extra):
    proc = subprocess.run(
        [sys.executable, "-c", _PROBE],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        env={**os.environ, **env_extra}, timeout=120,
    )
    if proc.returncode != 0:
        pytest.skip(f"子进程起不了 QApplication：{proc.stderr.strip()[-200:]}")
    return json.loads(proc.stdout.strip().splitlines()[-1])


def _factor(probe):
    """screen_scale_factor() 的算法，喂探针数据。"""
    return min(max(probe["logical_dpi"] / 96.0, 1.0), 2.0)


@pytest.mark.parametrize("knob", ["QT_SCALE_FACTOR", "QT_FONT_DPI"])
def test_scaling_lands_in_dpr_never_in_logical_dpi(knob):
    """☠️ 整个 HiDPI 设计的地基：Qt6 的缩放只进 DPR，不进 logicalDPI。

    这条一旦不成立，`screen_scale_factor()` 就会跟着屏幕缩放一起涨，而 Qt
    那边**也**已经放大过一次——首次运行的默认字号和窗口尺寸直接双重缩放。

    ☠️ 断言的是不变式，不是具体数字。第 2 步的第一版写死了"基线 logicalDPI
    = 96"，结果 GitHub 的 Windows runner 报 100，`100/96 = 1.04` 让 CI 当场
    变红——而代码一行问题都没有（CLAUDE.md 第 43 条）。同理**不要**断言
    `base["dpr"] == 1.0`：那是"开发机屏幕是 100%"，不是本项目的性质，
    在 150% 的笔记本上跑就会红。所以下面一律比**比值**。

    两个旋钮都测：QT_FONT_DPI 在 Qt5 时代是进 logicalDPI 的，Qt6 下改成了
    进 DPR——它比 QT_SCALE_FACTOR 更能抓住"Qt 又把缩放挪回 logicalDPI"的回归。
    """
    base = _probe({})
    scaled = _probe({knob: "1.5" if knob == "QT_SCALE_FACTOR" else "144"})

    # 反向对照：旋钮真的起作用了（否则下面两条断言全是空转）
    assert scaled["dpr"] > base["dpr"] * 1.4, (base, scaled)
    assert scaled["dpr"] == pytest.approx(base["dpr"] * 1.5, rel=0.02), (base, scaled)

    # 正题：缩放没有渗进 logicalDPI
    assert scaled["logical_dpi"] == pytest.approx(base["logical_dpi"], abs=0.5), \
        (base, scaled)

    # 于是我们自己那份倍率纹丝不动——不会双重缩放
    assert _factor(scaled) == pytest.approx(_factor(base), abs=0.01)


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
