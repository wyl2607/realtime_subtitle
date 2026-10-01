"""仅 macOS 替换 Windows 字体名，保留用户选择的其它字体。"""
import sys


def platform_font_family(family):
    if sys.platform != "darwin":
        return family
    result = family or "PingFang SC, Helvetica"
    for old, new in (("Microsoft YaHei UI", "PingFang SC"),
                     ("Microsoft YaHei", "PingFang SC"),
                     ("Segoe UI", "Helvetica"), ("Arial", "Helvetica")):
        result = result.replace(old, new)
    return result
