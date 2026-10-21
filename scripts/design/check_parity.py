"""跨端一致性检查：夹具副本、设计令牌副本必须与主仓库逐字节相同（spec 010 C3/C4）。

各端（iOS 测试、Android 仓库）都不引用本仓库的文件，而是各自存一份副本；
副本一旦漂移，行为就和主仓库分叉，而单看每一端都像是自洽的。所以这里只做
**只读**比对，不改任何文件；drift 报第一处不同的行号，方便直接跳过去看。

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
IOS_FIXTURE_NAMES = ("route_cases.json", "p5_cases.json")


def _display(path: Path, root: Path) -> str:
    try:
        return str(path.relative_to(root))
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
    left = expected.read_bytes()
    right = actual.read_bytes()
    if left == right:
        return True
    print(f"DRIFT {shown} 首处不同在第 {_first_diff_line(left, right)} 行")
    return False


def _check_repo(root: Path, allow_missing: bool) -> bool:
    ok = True
    for name in IOS_FIXTURE_NAMES:
        ok = _compare_file(
            root / "tests" / "fixtures" / name,
            root / "ios" / "Tests" / "fixtures" / name,
            root,
            allow_missing,
        ) and ok

    actual = root / "ios" / "Shared" / "DesignTokens.swift"
    tokens = json.loads((root / "design" / "tokens.json").read_text(encoding="utf-8"))
    expected = gen_tokens.render_swift(tokens).encode("utf-8")
    if not actual.exists():
        if allow_missing:
            print(f"WARN MISSING {_display(actual, root)}（副本缺失，--allow-missing 忽略）")
        else:
            print(f"MISSING {_display(actual, root)}")
            ok = False
    elif actual.read_bytes() != expected:
        ok = False
        print(
            f"DRIFT {_display(actual, root)} 首处不同在第 "
            f"{_first_diff_line(expected, actual.read_bytes())} 行"
        )
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
