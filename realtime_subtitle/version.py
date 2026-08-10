"""版本号的单一真相源。

为什么单独一个文件：以前版本号写死在 main.py 的启动横幅里（"实时字幕软件 v2.0"），
改了没人知道、也没有 git tag 对得上，用户报 bug 只能贴 commit hash。现在
启动日志、更新脚本、issue 模板都读这里。

☠️ 只放版本号，不要往这里加别的东西——install.ps1 / update_subtitles.ps1
都会用最朴素的方式读它（见 read_version），加了 import 会把那条路径弄坏。

版本规则见 CLAUDE.md 第 3 节「版本号怎么改」。
"""

__version__ = "2.2.0"

# 这一版的代号/日期，只用于展示（打 tag 时顺手更新）
__version_date__ = "2026-08-10"


def version_string():
    """给启动横幅和日志用的短字符串。"""
    return f"v{__version__}"
