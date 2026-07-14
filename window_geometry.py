"""
窗口几何工具：屏幕定位 + 坐标钳制。
被 popups.py（WordPopup 定位）和 subtitle_window.py（主窗/辅助窗定位）共用。
"""
from PyQt5.QtWidgets import QApplication


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
