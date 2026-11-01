"""从 design/tokens.json 生成 Swift 与 Python 常量（spec 006 / 010）。

为什么是生成而不是运行期读 JSON：rslite 是单个可执行文件、Windows 版从仓库就地跑，
两边都不想多一个运行期文件依赖；生成的常量编译进去，tests/test_design_tokens.py
盯着生成物与 tokens.json 同步，改了 JSON 忘了重新生成会红。

iOS 端（ios/Shared/DesignTokens.swift）与 rslite 共用同一份渲染结果：令牌全是
String/Double 字面量，不碰 AppKit/NSColor，两个平台的产物逐字节相同才能被
scripts/design/check_parity.py 直接比对。

用法：python3 scripts/design/gen_tokens.py [--check]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "design" / "tokens.json"
SWIFT_OUT = ROOT / "macos-native" / "Sources" / "rslite" / "DesignTokens.swift"
IOS_SWIFT_OUT = ROOT / "ios" / "Shared" / "DesignTokens.swift"
PY_OUT = ROOT / "realtime_subtitle" / "ui" / "design_tokens.py"
ANDROID_KT_REL = Path("core/src/main/kotlin/com/wyl26/dezhsubtitle/core/DesignTokens.kt")
HEADER = "由 scripts/design/gen_tokens.py 从 design/tokens.json 生成，不要手改。"
# Android 仓库的生成器（scripts/gen_design_tokens.py）用的是自己的源路径，头注释照抄。
KT_HEADER = (
    "由 scripts/gen_design_tokens.py 从 design/tokens.json 生成，不要手改。\n"
    "来源：与 Mac/Windows 共用的 design/tokens.json（spec 006 / spec 008）。\n"
    "同步：改 design/tokens.json 后运行 python3 scripts/gen_design_tokens.py。"
)
KT_GROUPS = (
    ("color", "Color"),
    ("opacity", "Opacity"),
    ("type", "TypeScale"),
    ("space", "Space"),
    ("radius", "Radius"),
    ("motion", "Motion"),
    ("text", "Text"),
)


def _swift_value(v):
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(float(v)) if isinstance(v, float) else str(v)
    return json.dumps(v, ensure_ascii=False)


def _swift_type(v):
    if isinstance(v, float):
        return "Double"
    if isinstance(v, int):
        return "Double"
    return "String"


def render_swift(tokens: dict) -> str:
    lines = [f"// {HEADER}", "", "enum DesignTokens {"]
    for group in ("color", "opacity", "type", "space", "radius", "motion", "text"):
        lines.append(f"    enum {group.capitalize() if group != 'type' else 'TypeScale'} {{")
        for key, value in tokens[group].items():
            lit = _swift_value(float(value) if isinstance(value, int) else value)
            lines.append(f"        static let {key}: {_swift_type(value)} = {lit}")
        lines.append("    }")
    lines.append("    enum Palette {")
    for kind in ("text", "background"):
        items = ", ".join(
            f"(name: {json.dumps(i['name'], ensure_ascii=False)}, hex: {json.dumps(i['hex'])})"
            for i in tokens["palette"][kind]
        )
        lines.append(f"        static let {kind}: [(name: String, hex: String)] = [{items}]")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def render_python(tokens: dict) -> str:
    lines = [f'"""{HEADER}"""', ""]
    for group in ("color", "opacity", "type", "space", "radius", "motion", "text"):
        for key, value in tokens[group].items():
            name = f"{group.upper()}_{_snake(key).upper()}"
            lines.append(f"{name} = {value!r}")
        lines.append("")
    for kind in ("text", "background"):
        items = ", ".join(f"({i['name']!r}, {i['hex']!r})" for i in tokens["palette"][kind])
        lines.append(f"PALETTE_{kind.upper()} = ({items},)")
    return "\n".join(lines) + "\n"


def _kt_literal(value):
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return repr(float(value))
    if isinstance(value, float):
        return repr(value)
    return json.dumps(value, ensure_ascii=False)


def _kt_type(value):
    if isinstance(value, bool):
        return "Boolean"
    if isinstance(value, (int, float)):
        return "Double"
    return "String"


def render_kotlin(tokens: dict) -> str:
    """Android 仓库 core/DesignTokens.kt 的内容，必须与那边生成器逐字节相同。"""
    lines = [f"// {part}" for part in KT_HEADER.split("\n")]
    lines += ["", "package com.wyl26.dezhsubtitle.core", "",
              "data class PaletteSwatch(val name: String, val hex: String)", "", "object DesignTokens {"]
    for group, type_name in KT_GROUPS:
        lines.append(f"    object {type_name} {{")
        for key, value in tokens[group].items():
            number = float(value) if isinstance(value, int) and not isinstance(value, bool) else value
            lines.append(f"        const val {key}: {_kt_type(value)} = {_kt_literal(number)}")
        lines.append("    }")
    lines.append("    object Palette {")
    for kind in ("text", "background"):
        lines.append(f"        val {kind}: List<PaletteSwatch> = listOf(")
        items = tokens["palette"][kind]
        for index, item in enumerate(items):
            name = json.dumps(item["name"], ensure_ascii=False)
            hex_value = json.dumps(item["hex"], ensure_ascii=False)
            comma = "," if index < len(items) - 1 else ""
            lines.append(f"            PaletteSwatch(name = {name}, hex = {hex_value}){comma}")
        lines.append("        )")
    lines.append("    }")
    lines.append("}")
    return "\n".join(lines) + "\n"


def _snake(name: str) -> str:
    out = []
    for ch in name:
        if ch.isupper():
            out.append("_")
            out.append(ch.lower())
        else:
            out.append(ch)
    return "".join(out)


def main(argv: list[str]) -> int:
    tokens = json.loads(SRC.read_text(encoding="utf-8"))
    swift = render_swift(tokens)
    outputs = {SWIFT_OUT: swift, IOS_SWIFT_OUT: swift, PY_OUT: render_python(tokens)}
    if "--check" in argv:
        stale = [str(p.relative_to(ROOT)) for p, text in outputs.items()
                 if not p.exists() or p.read_text(encoding="utf-8") != text]
        if stale:
            print("设计令牌生成物过期，请运行 python3 scripts/design/gen_tokens.py：", ", ".join(stale))
            return 1
        return 0
    for path, text in outputs.items():
        path.write_text(text, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
