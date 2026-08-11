r"""运行时文件必须落在仓库根 —— 跨进程约定的回归测试。

☠️ 这组用例存在的理由（2026-08-11 修）：包化重构把运行时代码挪进
`realtime_subtitle/` 之后，各模块里的
`os.path.dirname(os.path.abspath(__file__))` 一个都没跟着改，于是：

    .paused      -> realtime_subtitle\capture\.paused
    .stop        -> realtime_subtitle\capture\.stop
    transcripts\ -> realtime_subtitle\translate\transcripts\

而 pause_subtitles.ps1 / stop_subtitles.ps1 写的是 `<root>\`。后果是
**暂停脚本完全失效**、**停止脚本的优雅退出从此再没被触发过**（每次都走满
5 秒宽限再强杀，顺带丢掉查词缓存和 Ollama 显存卸载）。

这类 bug 读代码看不出来（每一处单独看都"对"），只有把两边对起来才暴露，
所以必须有测试盯着。下面第一条是**静态**扫描：它防的是整个类别，而不是
今天这六个具体位置——以后新增运行时文件时照样会被拦下。
"""
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG_ROOT = REPO_ROOT / "realtime_subtitle"

# 就是那个反模式：模块目录 ≠ 仓库根，包化之后一律推错
_FILE_RELATIVE_PATH = re.compile(
    r"os\.path\.dirname\s*\(\s*os\.path\.(?:abspath|realpath)\s*\(\s*__file__\s*\)\s*\)"
)


def test_no_module_derives_runtime_paths_from_dunder_file():
    """运行时路径一律走 realtime_subtitle.paths，不许再用 __file__ 推。

    paths.py 自己是唯一豁免（它就是那个真相源）。
    """
    offenders = []
    for py in sorted(PKG_ROOT.rglob("*.py")):
        if py.name == "paths.py":
            continue
        text = py.read_text(encoding="utf-8")
        for lineno, line in enumerate(text.splitlines(), 1):
            if _FILE_RELATIVE_PATH.search(line):
                offenders.append(f"{py.relative_to(REPO_ROOT)}:{lineno}")
    assert not offenders, (
        "这些位置按模块目录推路径，包化后会落在包里而不是仓库根，"
        "和 scripts\\windows\\*.ps1 的约定对不上：\n  " + "\n  ".join(offenders)
        + "\n改用 realtime_subtitle.paths.repo_path()。"
    )


def test_repo_root_is_the_directory_containing_main_py():
    """REPO_ROOT 必须是放着 main.py / config_local.py 的那一层。"""
    from realtime_subtitle.paths import REPO_ROOT as root

    assert (root / "main.py").is_file()
    assert (root / "realtime_subtitle").is_dir()
    assert root == REPO_ROOT


@pytest.mark.parametrize("parts, expected", [
    ((".paused",), ".paused"),
    ((".stop",), ".stop"),
    (("subtitle.pid",), "subtitle.pid"),
    (("transcripts",), "transcripts"),
    (("window_state.json",), "window_state.json"),
    (("lookup_cache.json",), "lookup_cache.json"),
])
def test_repo_path_lands_at_root(parts, expected):
    from realtime_subtitle.paths import repo_path

    p = Path(repo_path(*parts))
    assert p.parent == REPO_ROOT, f"{expected} 应该在仓库根，实际在 {p.parent}"
    assert p.name == expected


def test_pause_and_stop_flags_match_the_powershell_contract():
    """audio_capture 的两个标志文件必须和 .ps1 写的是同一个路径。

    pyaudiowpatch 只在 Windows 上装得了（ubuntu CI 没有），打个桩再导入——
    这两个常量在模块顶层算好，和音频后端没有任何关系。
    """
    pytest.importorskip("numpy")
    import sys
    import types

    for name in ("pyaudiowpatch", "soxr"):
        sys.modules.setdefault(name, types.ModuleType(name))

    from realtime_subtitle.capture import audio_capture

    assert Path(audio_capture.PAUSE_FLAG_FILE) == REPO_ROOT / ".paused"
    assert Path(audio_capture.STOP_FLAG_FILE) == REPO_ROOT / ".stop"

    # 反向对账：.ps1 里写死的路径就是 <root>\.paused / <root>\.stop
    pause_ps1 = (REPO_ROOT / "scripts/windows/pause_subtitles.ps1").read_text(
        encoding="utf-8-sig")
    stop_ps1 = (REPO_ROOT / "scripts/windows/stop_subtitles.ps1").read_text(
        encoding="utf-8-sig")
    assert '"$RepoRoot\\.paused"' in pause_ps1
    assert '"$RepoRoot\\.stop"' in stop_ps1


def test_lookup_cache_and_transcripts_land_at_root():
    """查词缓存/字幕存档的位置是 README 和 uninstall.ps1 告诉用户的那个。"""
    pytest.importorskip("requests")
    from realtime_subtitle import config
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    # 不构造真实实例（会加载 Whisper 模型），只调那个纯路径方法
    fake = object.__new__(WhisperQueueTranslator)
    cache = WhisperQueueTranslator._lookup_cache_path(fake)
    assert Path(cache).parent == REPO_ROOT

    assert Path(config.TRANSCRIPT_DIR).is_absolute() is False  # 配置里是相对名
