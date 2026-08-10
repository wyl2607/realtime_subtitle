"""首次运行的默认布局（换台机器才会暴露的那批问题）单测。

运行: venv\\Scripts\\python.exe -m pytest test_first_run_layout.py -q

config 里的 WINDOW_X/Y/WIDTH/HEIGHT 是按开发机屏幕写死的绝对坐标，
小屏笔记本上整窗会掉出可视区；字号又全是像素单位，125%/150% 缩放的
笔记本上会小一大截。这两条都只在**没有 window_state.json** 时才走到，
所以开发机上永远测不到——这个文件专门盯它们。

⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114。
⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
"""
import torch  # noqa: F401  先于 PyQt5
import sys

sys.stdout.reconfigure(encoding="utf-8")

import realtime_subtitle.config as config
from realtime_subtitle.ui.window_geometry import default_geometry


# 真实机型的可用区（availableGeometry，已排除任务栏）
SCREENS = {
    "1366x768 小笔记本": (0, 0, 1366, 728),
    "1920x1080 主流笔记本": (0, 0, 1920, 1040),
    "2560x1440": (0, 0, 2560, 1400),
    "3840x2160 4K": (0, 0, 3840, 2120),
    "副屏在左（负坐标）": (-1920, 0, 1920, 1040),
}


def test_default_geometry_always_inside_screen():
    """任何屏幕尺寸下，算出来的窗口必须完整落在可用区内。

    这是这次改动的核心诉求：1366x768 上旧的 WINDOW_Y=750 已经超出屏幕高度。
    """
    for name, (ax, ay, aw, ah) in SCREENS.items():
        for scale in (1.0, 1.25, 1.5, 2.0):
            x, y, w, h = default_geometry(ax, ay, aw, ah, scale)
            assert x >= ax, f"{name} scale={scale}: 左边越界"
            assert y >= ay, f"{name} scale={scale}: 上边越界"
            assert x + w <= ax + aw, f"{name} scale={scale}: 右边越界"
            assert y + h <= ay + ah, f"{name} scale={scale}: 下边越界"
            assert w > 0 and h > 0


def test_default_geometry_is_bottom_centered():
    """字幕窗该贴着底边居中——挡视频画面最少的位置。

    旧行为在小屏上是"掉出屏幕→被钳制拽回屏幕正中间"，正好挡在画面中央。
    """
    ax, ay, aw, ah = SCREENS["1366x768 小笔记本"]
    x, y, w, h = default_geometry(ax, ay, aw, ah)
    # 水平居中：左右留白差不超过 1px（整除误差）
    assert abs((x - ax) - (ax + aw - (x + w))) <= 1
    # 贴底：窗口底边离可用区底边不超过屏幕高度的 10%
    assert (ay + ah) - (y + h) <= ah * 0.1


def test_default_geometry_never_eats_half_the_screen():
    """小屏上高度要收着点，别把画面吃掉一半以上。"""
    for name, (ax, ay, aw, ah) in SCREENS.items():
        _, _, w, h = default_geometry(ax, ay, aw, ah, 2.0)
        assert h <= ah * 0.5, f"{name}: 高度吃掉了半屏以上"
        assert w <= aw * 0.92 + 1, f"{name}: 宽度没留边距"


def test_default_geometry_scales_up_on_hidpi():
    """同一块屏上，缩放倍率越高默认窗口越大（字也更大，得装得下）。"""
    ax, ay, aw, ah = SCREENS["3840x2160 4K"]  # 够大，不会撞上限
    _, _, w1, h1 = default_geometry(ax, ay, aw, ah, 1.0)
    _, _, w2, h2 = default_geometry(ax, ay, aw, ah, 1.5)
    assert w2 > w1 and h2 > h1


def test_default_geometry_matches_config_on_a_big_screen():
    """大屏上不该无端偏离 config 里的默认尺寸（只是位置改成贴底居中）。"""
    ax, ay, aw, ah = SCREENS["2560x1440"]
    _, _, w, h = default_geometry(ax, ay, aw, ah, 1.0)
    assert w == config.WINDOW_WIDTH
    assert h == config.WINDOW_HEIGHT + 40


def test_screen_scale_factor_is_bounded():
    """没有屏幕/拿不到 DPI 时必须安全退回 1.0，且不会放大到离谱。"""
    from PyQt5.QtWidgets import QApplication  # noqa: F401  确认导入顺序没问题
    import window_geometry

    class _FakeScreen:
        def __init__(self, dpi):
            self._dpi = dpi

        def logicalDotsPerInch(self):
            return self._dpi

    real = window_geometry.QApplication
    try:
        for dpi, expect in ((96, 1.0), (120, 1.25), (144, 1.5), (480, 2.0), (48, 1.0)):
            window_geometry.QApplication = type(
                "_A", (), {"primaryScreen": staticmethod(lambda d=dpi: _FakeScreen(d))}
            )
            assert window_geometry.screen_scale_factor() == expect, f"{dpi}dpi"
        # 拿不到屏幕
        window_geometry.QApplication = type(
            "_A", (), {"primaryScreen": staticmethod(lambda: None)}
        )
        assert window_geometry.screen_scale_factor() == 1.0
    finally:
        window_geometry.QApplication = real
