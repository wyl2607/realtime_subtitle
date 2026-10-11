"""设计令牌生成物必须与 design/tokens.json 同步（spec 006）。"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_generated_design_tokens_are_in_sync():
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "design" / "gen_tokens.py"), "--check"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_python_tokens_import_without_qt():
    from realtime_subtitle.ui import design_tokens as t

    assert t.COLOR_SUBTITLE_TRANSLATION.startswith("#")
    assert t.TYPE_SUBTITLE_MIN < t.TYPE_SUBTITLE_DEFAULT < t.TYPE_SUBTITLE_MAX
    assert len(t.PALETTE_TEXT) == 5 and len(t.PALETTE_BACKGROUND) == 5
