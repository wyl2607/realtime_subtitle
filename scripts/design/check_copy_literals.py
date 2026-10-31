"""检查用户可见状态文案没有脱离设计令牌而各端写死副本（spec 010）。

design/tokens.json 的 text 段是跨端状态文案的唯一真相源。生成物提供了每个平台
应引用的常量；若业务代码重新写入相同文案，令牌更新时就会悄悄漂移。因此本脚本
只读扫描各端的字符串字面量，并以非零退出码让 CI 能证明不存在未经说明的副本。
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from collections.abc import Iterable
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# 格式：相对路径:令牌键。这里只容纳无法简单改为令牌常量的非 UI 文案；每项必须说明理由。
ALLOW: dict[str, str] = {
    "macos-native/Sources/rslite/CloudSegmenter.swift:statusCloudKeyInvalid": (
        "内置自测的失败说明，不是显示给用户的状态值。"
    ),
    "realtime_subtitle/capture/audio_capture.py:micStatusIdle": (
        "Process Tap 启动失败的终端诊断日志，不是 UI 状态文案。"
    ),
    "realtime_subtitle/ui/subtitle_render.py:untranslated": (
        "重试处理函数的 docstring，未作为 UI 文案显示。"
    ),
}

GENERATED = {
    Path("realtime_subtitle/ui/design_tokens.py"),
    Path("macos-native/Sources/rslite/DesignTokens.swift"),
    Path("ios/Shared/DesignTokens.swift"),
}
ROUTE_STATUS_VALUES = {"本机", "混合·", "云端·Groq"}
DOUBLE_QUOTED_LITERAL = re.compile(r'"(?:\\.|[^"\\])*"')


def _tokens(root: Path) -> list[tuple[str, str, bool]]:
    """返回 (令牌键, 文案, 是否只能完整匹配)，保留 tokens.json 中的定义顺序。"""
    try:
        values = json.loads((root / "design" / "tokens.json").read_text(encoding="utf-8"))["text"]
    except (FileNotFoundError, json.JSONDecodeError, KeyError, TypeError) as exc:
        print(f"ERROR 无法读取 {root / 'design' / 'tokens.json'}: {exc}", file=sys.stderr)
        return []

    result = []
    for key, raw_value in values.items():
        if not isinstance(raw_value, str):
            continue
        value = raw_value.strip().removeprefix("· ").strip()
        exact = value in ROUTE_STATUS_VALUES
        if len(value) < 3 and not exact:
            continue
        result.append((key, value, exact))
    return result


def _is_excluded(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if relative in GENERATED:
        return True
    parts = relative.parts
    return (
        "tests" in parts
        or (parts[:2] == ("ios", "Tests"))
        or (parts[:2] == ("macos-native", "Tests"))
        or ".build" in parts
    )


def _source_files(root: Path, android: Path | None) -> Iterable[Path]:
    for directory, suffix in (
        (root / "realtime_subtitle", ".py"),
        (root / "macos-native" / "Sources", ".swift"),
        (root / "ios", ".swift"),
    ):
        if directory.exists():
            yield from (path for path in directory.rglob(f"*{suffix}") if not _is_excluded(path, root))
    if android is not None:
        kotlin_root = android / "app" / "src" / "main"
        if kotlin_root.exists():
            yield from kotlin_root.rglob("*.kt")


def _python_literals(path: Path) -> Iterable[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            yield node.lineno, node.value


def _double_quoted_literals(path: Path) -> Iterable[tuple[int, str]]:
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.lstrip().startswith("//"):
            continue
        for match in DOUBLE_QUOTED_LITERAL.finditer(line):
            yield number, match.group()[1:-1]


def _literals(path: Path) -> Iterable[tuple[int, str]]:
    if path.suffix == ".py":
        return _python_literals(path)
    return _double_quoted_literals(path)


def _fragment(value: str, limit: int = 80) -> str:
    shown = value.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')
    return shown if len(shown) <= limit else f"{shown[:limit - 1]}…"


def _matches(value: str, token: str, exact: bool) -> bool:
    return value == token if exact else token in value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="检查各端没有写死的设计状态文案副本")
    parser.add_argument("--root", type=Path, default=ROOT, help="仓库根目录（默认：脚本所在仓库）")
    parser.add_argument("--android", type=Path, help="额外扫描的 Android 仓库路径")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    if args.android is not None and not args.android.exists():
        print(f"ERROR Android 路径不存在: {args.android}", file=sys.stderr)
        return 2

    tokens = _tokens(root)
    if not tokens:
        return 2

    findings = 0
    files = 0
    try:
        for path in sorted(_source_files(root, args.android)):
            files += 1
            display = str(path.relative_to(root)) if path.is_relative_to(root) else str(path)
            for line, literal in _literals(path):
                for key, token, exact in tokens:
                    if not _matches(literal, token, exact):
                        continue
                    allow_key = f"{display}:{key}"
                    if allow_key in ALLOW:
                        continue
                    print(f'{display}:{line}  {key}  "{_fragment(literal)}"')
                    findings += 1
    except (OSError, SyntaxError, UnicodeDecodeError) as exc:
        print(f"ERROR 扫描失败: {exc}", file=sys.stderr)
        return 2

    if findings:
        print(f"COPY LITERALS FAILED: {findings} 处命中（扫描 {files} 个文件）")
        return 1
    print(f"COPY LITERALS OK（扫描 {files} 个文件）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
