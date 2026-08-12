r"""运行时文件的落点（唯一真相源）。

☠️ 这些文件是**跨进程约定**：`.paused` / `.stop` / `subtitle.pid` 由
scripts\windows\*.ps1 写、由 Python 读（或反过来），两边必须指同一个路径。
transcripts\ / window_state.json / lookup_cache.json 则是 README、
uninstall.ps1、update_subtitles.ps1 都按仓库根目录告诉用户的。

2026-08-10 包化重构时，运行时代码从仓库根挪进了 `realtime_subtitle/`，
但各模块里的 `os.path.dirname(os.path.abspath(__file__))` 一个都没跟着改，
于是这些文件跟着模块深了一到两层：`.paused` 掉进 `capture\`、transcripts
掉进 `translate\`。后果是暂停脚本完全失效、停止脚本的优雅退出路径再也没被
触发过（每次都走满 5 秒宽限再强杀，顺带丢掉查词缓存和显存卸载）。
CLAUDE.md 第 4 节第 26 条预告过这一类漏改，当时只修了 config.py 那一处。

所以路径一律从这里取，**不要再在模块里写 `__file__` 推路径**。
tests/test_runtime_paths.py 会盯着这件事。

☠️ 本模块必须保持零项目内 import（config.py 也要用它），别往里加东西。
"""
from pathlib import Path

# realtime_subtitle/paths.py -> realtime_subtitle/ -> <仓库根>
REPO_ROOT = Path(__file__).resolve().parents[1]


def repo_path(*parts):
    """拼一个仓库根下的绝对路径，返回 str（调用方多数是 os.path / open）。"""
    return str(REPO_ROOT.joinpath(*parts))
