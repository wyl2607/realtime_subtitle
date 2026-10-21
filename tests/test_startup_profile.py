"""startup_profile.py 的解析/对齐逻辑（不跑真实启动）。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts" / "bench"))

import startup_profile as sp  # noqa: E402

SAMPLE = """\
import time: self [us] | cumulative | imported package
import time:       198 |        198 |   _io
import time:     24322 |      52951 |     PyQt6.QtWidgets
import time:     53809 |      56068 |           sounddevice
import time:      8537 |     246184 | realtime_subtitle.app
"""


def test_parse_importtime_handles_indented_and_flat_names():
    parsed = sp.parse_importtime(SAMPLE)
    assert parsed["PyQt6.QtWidgets"] == {"self_us": 24322, "cumulative_us": 52951}
    assert parsed["sounddevice"]["cumulative_us"] == 56068
    assert parsed["realtime_subtitle.app"]["cumulative_us"] == 246184


def test_summarize_importtime_reports_missing_modules_as_none():
    rows = sp.summarize_importtime(sp.parse_importtime(SAMPLE),
                                   ("PyQt6.QtWidgets", "mlx"))
    assert rows[0]["cumulative_ms"] == 53.0
    assert rows[1] == {"module": "mlx", "self_ms": None, "cumulative_ms": None}


def test_log_stages_aligns_profile_marks_with_parent_clock():
    result = {
        "log_times": [
            {"t": 0.30, "line": "==== 实时字幕软件 ===="},
            {"t": 0.32, "line": "🔧 正在初始化组件..."},
            {"t": 0.60, "line": "✅ 字幕窗口已创建"},
            {"t": 1.90, "line": "✅ 所有组件初始化完成"},
        ],
        "marks": [
            {"name": "import_app", "t": 0.20, "detail": ""},
            {"name": "ready", "t": 1.80, "detail": ""},
        ],
    }
    stages = {item["name"]: item["t_parent"] for item in sp._log_stages(result)}
    # offset = 0.30 - 0.20 = 0.10；ready 1.80 + 0.10 = 1.90
    assert stages["init_components"] == 0.32
    assert stages["window_created"] == 0.60
    assert stages["components_done"] == 1.90
    assert stages["ready"] == 1.90


def test_log_stages_without_marks_still_reports_log_lines():
    result = {
        "log_times": [{"t": 0.2, "line": "✅ 字幕窗口已创建"}],
        "marks": [],
    }
    stages = sp._log_stages(result)
    assert [item["name"] for item in stages] == ["window_created"]
