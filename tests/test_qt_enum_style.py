"""Qt 枚举一律写全限定名（`Qt.AlignmentFlag.AlignLeft`，不是 `Qt.AlignLeft`）。

这是 PyQt5 → Qt6 迁移的第 1 步留下的守卫。PyQt6 **强制**作用域枚举，短名全部
消失；而 PyQt5 5.15 两种写法都认，所以这一步可以先在 PyQt5 上落地、单独验证。
没有这道守卫的话，新代码会继续按 PyQt5 的习惯写短名，等真正翻 import 的那天
又得重扫一遍。

判据不是一张手写的名单，而是 introspect 当前装着的 PyQt5：
`type(Qt.Vertical).__name__ == "Orientation"` → 正确写法是 `Qt.Orientation.Vertical`。

☠️ 用 tokenize 而不是正则扫：字符串字面量和注释里出现同名文本不算违规
（比如注释里写"以前是 Qt.AlignLeft"）。
"""
import io
import tokenize
from pathlib import Path

from PyQt5 import QtCore, QtGui, QtWidgets

from realtime_subtitle.paths import REPO_ROOT

_QT_MODULES = (QtCore, QtGui, QtWidgets)


def _scoped_enum_name(cls_name: str, attr: str):
    """`("Qt", "AlignLeft")` -> `"AlignmentFlag"`；不是作用域枚举则 None。"""
    for mod in _QT_MODULES:
        cls = getattr(mod, cls_name, None)
        if not isinstance(cls, type):
            continue
        try:
            value = getattr(cls, attr)
        except Exception:
            return None
        enum_type = type(value)
        enum_name = enum_type.__name__
        if enum_name == attr or getattr(cls, enum_name, None) is not enum_type:
            return None
        member = getattr(enum_type, attr, None)
        # 等值且同类型才算"同一个东西的两种写法"
        if member is None or member != value or type(member) is not enum_type:
            return None
        return enum_name
    return None


def _source_files():
    for folder in ("realtime_subtitle", "tests"):
        for path in sorted((REPO_ROOT / folder).rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path
    yield REPO_ROOT / "main.py"


def _short_enum_uses(path: Path):
    """代码里写成短名的枚举引用，返回 [(行号, 'Qt.AlignLeft', 'AlignmentFlag')]。"""
    with open(path, encoding="utf-8", newline="") as handle:
        source = handle.read()
    toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    found = []
    for i, tok in enumerate(toks):
        if tok.type != tokenize.NAME or not tok.string.startswith("Q"):
            continue
        if i + 2 >= len(toks):
            continue
        dot, attr = toks[i + 1], toks[i + 2]
        if dot.type != tokenize.OP or dot.string != "." or attr.type != tokenize.NAME:
            continue
        # a.Qt.X：前缀不是我们要判的那个类
        if i and toks[i - 1].type == tokenize.OP and toks[i - 1].string == ".":
            continue
        # 已经是全限定名（Qt.AlignmentFlag.AlignLeft）时，attr 本身是枚举类型
        if i + 4 < len(toks) and toks[i + 3].string == "." \
                and _scoped_enum_name(tok.string, toks[i + 4].string) == attr.string:
            continue
        enum_name = _scoped_enum_name(tok.string, attr.string)
        if enum_name:
            found.append((tok.start[0], f"{tok.string}.{attr.string}", enum_name))
    return found


def test_no_short_form_qt_enums():
    offenders = []
    for path in _source_files():
        for line, expr, enum_name in _short_enum_uses(path):
            cls = expr.split(".")[0]
            member = expr.split(".")[1]
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{line}  {expr}"
                f"  →  {cls}.{enum_name}.{member}"
            )
    assert not offenders, (
        "这些地方还在用 Qt 的短枚举名，PyQt6 下会 AttributeError：\n  "
        + "\n  ".join(offenders)
    )


def test_no_exec_underscore():
    """`exec_()` 在 PyQt6 里没了，`exec()` 在 PyQt5 上就能用。"""
    # 拼出来而不是写死字面量，否则本文件会把自己抓进去
    needle = ".exec" + "_("
    offenders = []
    for path in _source_files():
        with open(path, encoding="utf-8", newline="") as handle:
            for number, line in enumerate(handle, 1):
                if needle in line:
                    offenders.append(f"{path.relative_to(REPO_ROOT)}:{number}")
    assert not offenders, "改成 .exec()：\n  " + "\n  ".join(offenders)


def test_guard_actually_detects_short_names(tmp_path):
    """反向对照：守卫本身必须抓得到短名，否则它只是一直绿。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "from PyQt5.QtCore import Qt\n"
        "flag = Qt.AlignLeft\n"
        "ok = Qt.AlignmentFlag.AlignRight\n"
        "text = 'Qt.AlignBottom'  # 字符串里的不算\n",
        encoding="utf-8",
    )
    found = _short_enum_uses(sample)
    assert [(expr, enum) for _, expr, enum in found] == [("Qt.AlignLeft", "AlignmentFlag")]
