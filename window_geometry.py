"""
窗口几何工具：屏幕定位 + 坐标钳制 + 首次运行的默认布局。
被 popups.py（WordPopup 定位）和 subtitle_window.py（主窗/辅助窗定位）共用。
"""
from PyQt5.QtWidgets import QApplication

import config


def _screen_area_at(global_pos):
    """global_pos 所在屏的 availableGeometry；screenAt 失败时退回主屏。"""
    screen = QApplication.screenAt(global_pos)
    if screen is None:
        screen = QApplication.primaryScreen()
    return screen.availableGeometry() if screen else None


def _clamp_geo_to_area(x, y, w, h, area):
    """把窗口几何钳进给定 QRect（availableGeometry）。"""
    w = min(max(1, w), area.width())
    h = min(max(1, h), area.height())
    x = max(area.left(), min(x, area.right() - w + 1))
    y = max(area.top(), min(y, area.bottom() - h + 1))
    return x, y, w, h


def _clamp_geo_to_any_screen(x, y, w, h):
    """按窗口中心/左上角找屏，钳进该屏；无屏则原样返回。"""
    from PyQt5.QtCore import QPoint
    screen = (QApplication.screenAt(QPoint(x + w // 2, y + h // 2))
              or QApplication.screenAt(QPoint(x, y))
              or QApplication.primaryScreen())
    if screen is None:
        return x, y, w, h
    return _clamp_geo_to_area(x, y, w, h, screen.availableGeometry())


def default_geometry(area_x, area_y, area_w, area_h, scale=1.0):
    """首次运行（还没有 window_state.json）时的默认几何：水平居中、贴近底边。

    config 里的 WINDOW_X/Y/WIDTH/HEIGHT 是按开发机（1080p 以上桌面）写死的
    绝对坐标。换台机器就不成立了：1366x768 的小笔记本上 y=750 已经超出屏幕，
    整个窗口掉在可视区外，只能靠 _clamp_geo_to_any_screen 硬拽回来——拽回来
    的落点是屏幕正中间，而字幕本来该待在底部（挡视频画面最少的地方）。
    所以首次运行不用那组绝对坐标，按实际屏幕现算。

    纯算术、不碰 Qt，方便直接测。返回 (x, y, w, h)。
    """
    w = min(int(config.WINDOW_WIDTH * scale), int(area_w * 0.92))
    # 高度上限取半屏：小屏上字幕窗不该吃掉一半以上的画面
    h = min(int((config.WINDOW_HEIGHT + 40) * scale), int(area_h * 0.5))
    x = area_x + (area_w - w) // 2
    # area 是 availableGeometry（已经排除任务栏），这里只是再留一点呼吸空间
    y = max(area_y, area_y + area_h - h - int(56 * scale))
    return x, y, w, h


def screen_scale_factor():
    """主屏的显示缩放倍率（逻辑 DPI / 96），限制在 1.0–2.0。

    ☠️ 本项目所有字号都是像素单位（`setPixelSize` / 样式表 `font-size: Npx`），
    而 Qt5 的 AA_EnableHighDpiScaling 是默认关闭的——悬浮窗是无 QLayout 的手动
    setGeometry 布局 + WM_NCHITTEST 原生命中测试，全局开缩放会改坐标空间、
    动到命中测试（见 CLAUDE.md 第 17 条），风险远大于收益。
    代价是在 150% 缩放的笔记本上（笔记本几乎都不是 100%）字会小三分之一。
    折中：首次运行时按这个倍率放大默认字号和默认窗口尺寸，用户之后
    Ctrl+滚轮调过就以他调的为准。按钮条等 chrome 的字号仍未跟随缩放。
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return 1.0
    try:
        factor = screen.logicalDotsPerInch() / 96.0
    except (AttributeError, ZeroDivisionError):
        return 1.0
    return min(max(factor, 1.0), 2.0)
