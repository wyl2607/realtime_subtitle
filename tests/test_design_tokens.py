"""设计令牌生成物必须与 design/tokens.json 同步（spec 006）。"""
import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _load_generator():
    path = ROOT / "scripts" / "design" / "gen_tokens.py"
    spec = importlib.util.spec_from_file_location("_design_gen_tokens", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


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


def test_render_kotlin_shape():
    generator = _load_generator()
    tokens = {
        "color": {"subtitleSource": "#F0F0F0"},
        "opacity": {"backgroundDefault": 0.72},
        "type": {"subtitleDefault": 28},
        "space": {"pad": 4},
        "radius": {"none": 0},
        "motion": {"on": True},
        "text": {"micSubtitleTitle": "麦克风字幕"},
        "palette": {
            "text": [{"name": "白", "hex": "#F0F0F0"}, {"name": "暖黄", "hex": "#FFE894"}],
            "background": [{"name": "黑", "hex": "#000000"}, {"name": "深灰", "hex": "#1E1E1E"}],
        },
    }
    lines = generator.render_kotlin(tokens).splitlines()

    assert lines[0] == "// 由 scripts/gen_design_tokens.py 从 design/tokens.json 生成，不要手改。"
    assert lines[1] == "// 来源：与 Mac/Windows 共用的 design/tokens.json（spec 006 / spec 008）。"
    assert lines[2] == "// 同步：改 design/tokens.json 后运行 python3 scripts/gen_design_tokens.py。"
    assert "        const val pad: Double = 4.0" in lines
    assert "        const val backgroundDefault: Double = 0.72" in lines
    assert "        const val on: Boolean = true" in lines
    assert "        const val micSubtitleTitle: String = \"麦克风字幕\"" in lines
    assert "            PaletteSwatch(name = \"暖黄\", hex = \"#FFE894\")" in lines
    assert "            PaletteSwatch(name = \"暖黄\", hex = \"#FFE894\")," not in lines


def test_ios_tokens_are_platform_neutral():
    # iOS 的 Shared/ 同时被 App 与广播扩展编译；生成物只能是 String/Double 常量，
    # 一旦混进 AppKit/NSColor 就会在 iOS target 上直接编译失败。
    text = (ROOT / "ios" / "Shared" / "DesignTokens.swift").read_text(encoding="utf-8")
    assert "enum DesignTokens" in text
    assert "AppKit" not in text
    assert "UIKit" not in text
    assert "NSColor" not in text
    assert '"#FFE894"' in text

