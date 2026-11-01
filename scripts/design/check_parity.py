"""跨端一致性检查：夹具副本、设计令牌副本必须与主仓库逐字节相同（spec 010 C3/C4）。

各端（iOS 测试、Android 仓库）都不引用本仓库的文件，而是各自存一份副本；
副本一旦漂移，行为就和主仓库分叉，而单看每一端都像是自洽的。所以这里只做
**只读**比对，不改任何文件；drift 报第一处不同的行号，方便直接跳过去看。

四端各查什么：
    iOS     — ios/Tests/fixtures/*.json 与 tests/fixtures/ 逐字节相同；
              ios/Shared/DesignTokens.swift 与 render_swift(主仓库 tokens) 相同。
    rslite  — macos-native/Sources/rslite/DesignTokens.swift 与同一份
              render_swift(主仓库 tokens) 相同（它直接读主仓库 tests/fixtures/，
              不需要副本，所以夹具那项不查）。
    Python  — realtime_subtitle/ui/design_tokens.py 与 render_python(主仓库 tokens) 相同。
    Android — 主仓库 design/tokens.json、tests/fixtures/*.json 与 Android 仓库的副本相同；
              core/src/main/kotlin/.../DesignTokens.kt 与 render_kotlin(主仓库 tokens) 相同。
              期望值一律从**主仓库** tokens 渲染，所以 Android 的 tokens.json 和
              Kotlin 任一漂移都能抓到（自洽地一起改也一样会红）。

Android 部分只能在本地跑：那个仓库是私有的，CI 拿不到，所以 GitHub Actions
里只有 repo 侧（iOS / rslite / Python）这一段。

用法：
    python3 scripts/design/check_parity.py
    python3 scripts/design/check_parity.py --android <Android 仓库路径>
    python3 scripts/design/check_parity.py --allow-missing
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

# gen_tokens 与它同目录，脚本直接跑时不会被加入 sys.path；显式补上再 import。
sys.path.insert(0, str(Path(__file__).resolve().parent))
import gen_tokens  # noqa: E402

REPO_FIXTURES = ROOT / "tests" / "fixtures"
REPO_IOS_FIXTURES = ROOT / "ios" / "Tests" / "fixtures"
REPO_IOS_TOKENS = ROOT / "ios" / "Shared" / "DesignTokens.swift"


def _display(path: Path, root: Path) -> str:
    try:
        # 统一成 / 分隔：Windows 上也输出同一种路径，日志和测试断言不用分平台。
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def _first_diff_line(left: bytes, right: bytes) -> int:
    # keepends=True：只差一个行尾换行时也停在真正那一行，而不是报下一行。
    a = left.splitlines(keepends=True)
    b = right.splitlines(keepends=True)
    for number, (x, y) in enumerate(zip(a, b), 1):
        if x != y:
            return number
    return min(len(a), len(b)) + 1


def _read_normalized(path: Path) -> bytes:
    """读文件并把 CRLF 统一成 LF 再比。

    Windows 上 git 默认 core.autocrlf=true，签出的文本副本是 CRLF；gen_tokens.py --check
    用 read_text（通用换行）所以不在乎，这里也必须一样，否则 Windows 上永远报 DRIFT。
    只统一换行，其余仍逐字节比对。
    """
    return path.read_bytes().replace(b"\r\n", b"\n")


def _compare_file(expected: Path, actual: Path, root: Path, allow_missing: bool) -> bool:
    """返回 True 表示这项算通过（一致，或被 --allow-missing 放过）。"""
    if not expected.exists():
        # 主仓库的源文件缺失不是"副本还没建"，--allow-missing 不该把它盖掉。
        print(f"MISSING {_display(expected, root)}")
        return False
    shown = _display(actual, root)
    if not actual.exists():
        if allow_missing:
            print(f"WARN MISSING {shown}（副本缺失，--allow-missing 忽略）")
            return True
        print(f"MISSING {shown}")
        return False
    left = _read_normalized(expected)
    right = _read_normalized(actual)
    if left == right:
        return True
    print(f"DRIFT {shown} 首处不同在第 {_first_diff_line(left, right)} 行")
    return False


def _compare_text(expected_text: str, actual: Path, root: Path, allow_missing: bool) -> bool:
    """比对"本地渲染出来的期望文本"与实际文件。返回 True 表示此项算通过。"""
    shown = _display(actual, root)
    if not actual.exists():
        if allow_missing:
            print(f"WARN MISSING {shown}（副本缺失，--allow-missing 忽略）")
            return True
        print(f"MISSING {shown}")
        return False
    left = expected_text.encode("utf-8")
    right = _read_normalized(actual)
    if left == right:
        return True
    print(f"DRIFT {shown} 首处不同在第 {_first_diff_line(left, right)} 行")
    return False


def _check_repo(root: Path, allow_missing: bool) -> bool:
    ok = True
    # 与 Android 一样按主仓库夹具全量比对：新增夹具忘了同步到 iOS 也会报 MISSING。
    for expected in sorted((root / "tests" / "fixtures").glob("*.json")):
        ok = _compare_file(
            expected,
            root / "ios" / "Tests" / "fixtures" / expected.name,
            root,
            allow_missing,
        ) and ok

    tokens = json.loads((root / "design" / "tokens.json").read_text(encoding="utf-8"))
    swift = gen_tokens.render_swift(tokens)
    python_text = gen_tokens.render_python(tokens)
    for actual, expected in (
        (root / "ios" / "Shared" / "DesignTokens.swift", swift),
        (root / "macos-native" / "Sources" / "rslite" / "DesignTokens.swift", swift),
        (root / "realtime_subtitle" / "ui" / "design_tokens.py", python_text),
    ):
        ok = _compare_text(expected, actual, root, allow_missing) and ok
    return ok


def _check_android(root: Path, android: Path, allow_missing: bool) -> bool:
    ok = _compare_file(
        root / "design" / "tokens.json",
        android / "design" / "tokens.json",
        root,
        allow_missing,
    )
    fixtures = sorted((root / "tests" / "fixtures").glob("*.json"))
    for expected in fixtures:
        ok = _compare_file(
            expected,
            android / "core" / "src" / "test" / "resources" / "fixtures" / expected.name,
            root,
            allow_missing,
        ) and ok

    tokens = json.loads((root / "design" / "tokens.json").read_text(encoding="utf-8"))
    ok = _compare_text(
        gen_tokens.render_kotlin(tokens),
        android / gen_tokens.ANDROID_KT_REL,
        root,
        allow_missing,
    ) and ok
    return ok


def main(argv: list[str] | None = None, root: Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨端夹具/设计令牌逐字节一致性检查")
    parser.add_argument("--android", type=Path, help="Android 仓库路径（可选）")
    parser.add_argument("--allow-missing", action="store_true", help="副本缺失只警告，不算失败")
    args = parser.parse_args(argv)

    base = Path(root) if root is not None else ROOT
    ok = _check_repo(base, args.allow_missing)
    if args.android is not None:
        ok = _check_android(base, args.android, args.allow_missing) and ok

    if ok:
        print("PARITY OK")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
