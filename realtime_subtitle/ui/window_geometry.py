"""
窗口几何工具：屏幕定位 + 坐标钳制 + 首次运行的默认布局。
被 popups.py（WordPopup 定位）和 subtitle_window.py（主窗/辅助窗定位）共用。
"""
from PyQt5.QtWidgets import QApplication

import realtime_subtitle.config as config
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
    """**我们自己**还需要乘多少（逻辑 DPI / 96），限制在 1.0–2.0。

    ☠️ 本项目所有字号都是像素单位（`setPixelSize` / 样式表 `font-size: Npx`），
    而 Qt5 的 AA_EnableHighDpiScaling 是默认关闭的——悬浮窗是无 QLayout 的手动
    setGeometry 布局 + WM_NCHITTEST 原生命中测试，全局开缩放会改坐标空间、
    动到命中测试（见 CLAUDE.md 第 17 条），风险远大于收益。
    代价是在 150% 缩放的笔记本上（笔记本几乎都不是 100%）字会小三分之一。
    折中：首次运行时按这个倍率放大默认字号和默认窗口尺寸，用户之后
    Ctrl+滚轮调过就以他调的为准。按钮条等 chrome 的字号仍未跟随缩放。

    ☠️ **这个公式在 Qt6 下不需要改，别"顺手修"成除以 devicePixelRatio。**
    Qt 接管缩放之后会把 logicalDotsPerInch 报成 96，本函数自动归 1.0，
    缩放那份由 Qt 出——总倍率仍然是 1.5，不会双重缩放。本机实测
    （PyQt5 开/关 AA_EnableHighDpiScaling + QT_SCALE_FACTOR 模拟 150% 屏）：

        Qt5 @100%   logicalDPI 96    DPR 1.0   本函数 1.00
        Qt5 @150%   logicalDPI 144   DPR 1.0   本函数 1.50   ← 我们乘
        Qt6 @150%   logicalDPI 96    DPR 1.5   本函数 1.00   ← Qt 乘

    tests/test_hidpi_scaling.py 把这三行钉住了。真正会在 Qt6 下坏掉的不是
    这个函数，是**存盘的几何和像素字号**——见 qt_hidpi_scale/rescale_state_for_dpr。
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return 1.0
    try:
        factor = screen.logicalDotsPerInch() / 96.0
    except (AttributeError, ZeroDivisionError):
        return 1.0
    return min(max(factor, 1.0), 2.0)


def qt_hidpi_scale():
    """Qt **已经替我们做掉**的那部分缩放（主屏 devicePixelRatio）。

    Qt5 不开 AA_EnableHighDpiScaling 时恒为 1.0，Qt6 的 HiDPI 关不掉，
    150% 的屏上是 1.5。存盘时要把它一起记下来：同一组数字在两个坐标空间里
    含义不同（1054 在 Qt5 下是 1054 物理像素，在 Qt6 @150% 下是 1581 物理像素）。
    """
    screen = QApplication.primaryScreen()
    if screen is None:
        return 1.0
    try:
        dpr = float(screen.devicePixelRatio())
    except (AttributeError, TypeError, ValueError):
        return 1.0
    return dpr if dpr > 0 else 1.0


# 存档里按坐标空间走的键。☠️ 用显式名单而不是遍历整个 dict：tuning 里
# 绝大多数值（ENERGY_THRESHOLD_SPEECH、CHUNK_SUBMIT_SECONDS…）不是像素，
# 一起乘会把用户的调参毁掉。新增像素单位的持久化项时要加到这里。
# ☠️ 坐标和尺寸要分开：多屏时左边/上方那块屏的 x/y 是负数，尺寸和字号却
# 必须为正。以前把它们一起 max(1, …) 会把负坐标夹成 1，窗口跳到主屏左上角。
_DPR_SCALED_POSITIONS = ("x", "y")                     # 可以为负
_DPR_SCALED_SIZES = ("w", "h", "font_size")            # 必须 ≥ 1
_DPR_SCALED_GEO_LISTS = ("settings_geo", "history_geo")  # [x, y, w, h]
_DPR_SCALED_NESTED = (("tv", "font_size"), ("tuning", "CINEMA_FONT_SIZE"))


def rescale_state_for_dpr(state, current_dpr, saved_dpr=None):
    """把存档里的几何/像素字号换算到当前坐标空间。纯算术、不碰 Qt。

    ☠️ 这是 Qt5 → Qt6 升级时唯一会让**老用户**看出差别的地方。Qt5 存的是物理
    像素，Qt6 会把同样的数字当逻辑像素用：150% 的笔记本上窗口和字一起涨 50%，
    而且用户完全不知道发生了什么（日志里也没有任何异常）。

    老存档没有 coord_dpr 字段，一律按 1.0 算——本项目只支持 Windows
    （pyaudiowpatch 连 linux 轮子都没有），而 Windows 上不开 AA_EnableHighDpiScaling
    时 devicePixelRatio 恒为 1.0，所以这个假设对历史存档是准确的。

    ⚠️ 只按主屏的 DPR 算。多屏各自缩放不同时，窗口落点可能偏——但随后
    _clamp_geo_to_any_screen 会把它拽回某块屏内，不会丢窗。
    """
    if not isinstance(state, dict):
        return state
    if saved_dpr is None:
        saved_dpr = state.get("coord_dpr", 1.0)
    try:
        saved = float(saved_dpr)
        current = float(current_dpr)
    except (TypeError, ValueError):
        return state
    if saved <= 0 or current <= 0 or abs(saved - current) < 1e-6:
        return state

    factor = saved / current

    def _scaled(value, positive=True):
        try:
            scaled = int(round(float(value) * factor))
        except (TypeError, ValueError):
            return value          # 坏值原样留给各自的 int() 容错去处理
        return max(1, scaled) if positive else scaled

    out = dict(state)
    for key in _DPR_SCALED_POSITIONS:
        if key in out:
            out[key] = _scaled(out[key], positive=False)
    for key in _DPR_SCALED_SIZES:
        if key in out:
            out[key] = _scaled(out[key])
    for key in _DPR_SCALED_GEO_LISTS:
        geo = out.get(key)
        if isinstance(geo, (list, tuple)) and len(geo) == 4:
            x, y, w, h = geo
            out[key] = [_scaled(x, positive=False), _scaled(y, positive=False),
                        _scaled(w), _scaled(h)]
    for parent, child in _DPR_SCALED_NESTED:
        section = out.get(parent)
        if isinstance(section, dict) and child in section:
            section = dict(section)
            section[child] = _scaled(section[child])
            out[parent] = section
    out["coord_dpr"] = current
    return out


def settings_initial_geometry(area_x, area_y, area_w, area_h, scale=1.0):
    """设置面板初始几何：受可用屏幕约束，内容超出靠滚动。

    旧实现写死 520×1060，1366×768 上底部被切掉。纯算术、不碰 Qt。
    返回 (x, y, w, h)。
    """
    scale = min(max(float(scale), 1.0), 2.0)
    w = min(int(560 * scale), max(360, int(area_w * 0.92)))
    # 100% 下约一屏放下全部控件；小屏被 92% 可用高钳住，靠滚动看完
    preferred_h = int(1080 * scale)
    h = min(preferred_h, max(360, int(area_h * 0.92)))
    x = area_x + min(80, max(0, (area_w - w) // 8))
    y = area_y + min(60, max(0, (area_h - h) // 10))
    if x + w > area_x + area_w:
        x = area_x + max(0, area_w - w)
    if y + h > area_y + area_h:
        y = area_y + max(0, area_h - h)
    return max(area_x, x), max(area_y, y), w, h
