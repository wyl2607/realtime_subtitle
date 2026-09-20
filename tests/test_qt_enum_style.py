"""Qt 枚举一律写全限定名（`Qt.AlignmentFlag.AlignLeft`，不是 `Qt.AlignLeft`）。

这是 PyQt5 → PyQt6 迁移第 1 步留下的守卫，第 3 步换上真 PyQt6 之后改写过判据。

原来的判据是 introspect PyQt5：`type(Qt.Vertical).__name__ == "Orientation"`
——**这套在 PyQt6 上整个失效**，因为 `Qt.Vertical` 已经不存在了，`getattr`
直接抛，于是每一处都被判成"不是枚举"，守卫会永远绿着（连它自己的反向对照
用例都会红）。现在反过来从枚举类型那一侧列成员：`"Vertical" in
Qt.Orientation.__members__` → 短名 `Qt.Vertical` 的正确写法是
`Qt.Orientation.Vertical`。PyQt6 的枚举就是标准库的 `enum.Enum`，可以直接问。

☠️ PyQt6 里写短名本来就是当场 AttributeError，那为什么还留着这道静态扫描：
**AttributeError 只在那行真被执行时才炸**。本项目一堆分支平时不走
（错误路径、某个模式下才有的按钮、虚函数重写——第 4 节第 27 条那种在
`changeEvent` 里 abort 掉整个进程的尤其难查）。静态扫描不挑执行路径。

☠️ 用 tokenize 而不是正则扫：字符串字面量和注释里出现同名文本不算违规
（比如注释里写"以前是 Qt.AlignLeft"）。
"""
import ast
import enum
import io
import tokenize
from pathlib import Path

from PyQt6 import QtCore, QtGui, QtWidgets

from realtime_subtitle.paths import REPO_ROOT

_QT_MODULES = (QtCore, QtGui, QtWidgets)


def _scoped_enum_name(cls_name: str, attr: str):
    """`("Qt", "AlignLeft")` -> `"AlignmentFlag"`；不是某个作用域枚举的成员则 None。"""
    for mod in _QT_MODULES:
        cls = getattr(mod, cls_name, None)
        if not isinstance(cls, type):
            continue
        # attr 本身就是枚举类型名（`Qt.AlignmentFlag`）——那是正确写法，不是违规
        if isinstance(getattr(cls, attr, None), type):
            return None
        # dir() 而不是 vars()：枚举可能定义在基类上（QWidget 继承 QObject 那一串）
        for name in dir(cls):
            try:
                candidate = getattr(cls, name)
            except Exception:
                continue
            if (isinstance(candidate, type) and issubclass(candidate, enum.Enum)
                    and attr in candidate.__members__):
                return candidate.__name__
        return None
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


# --- 第二道：实例上取短枚举名 -------------------------------------------

def _enum_members_by_name():
    """`{"End": {"QTextCursor.MoveOperation.End", ...}}`——所有枚举成员的反查表。"""
    table = {}
    for mod in _QT_MODULES:
        for cls_name in dir(mod):
            cls = getattr(mod, cls_name, None)
            if not isinstance(cls, type):
                continue
            for enum_name in dir(cls):
                try:
                    enum_type = getattr(cls, enum_name)
                except Exception:
                    continue
                if isinstance(enum_type, type) and issubclass(enum_type, enum.Enum):
                    for member in enum_type.__members__:
                        table.setdefault(member, set()).add(
                            f"{cls_name}.{enum_name}.{member}")
    return table


_ENUM_MEMBERS = _enum_members_by_name()


def _imported_names(source: str):
    """本文件里被 import 绑定的名字——`queue.Full` 这种不是枚举，要放过。"""
    names = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _instance_short_enum_uses(path: Path):
    """`cursor.End` 这种从**实例**上取短枚举名，返回 [(行号, 'cursor.End', 候选)]。"""
    with open(path, encoding="utf-8", newline="") as handle:
        source = handle.read()
    imported = _imported_names(source)
    toks = list(tokenize.generate_tokens(io.StringIO(source).readline))
    found = []
    for i, tok in enumerate(toks):
        # `Q…` 开头的前缀归上面那道管；这里专管小写的实例名
        if tok.type != tokenize.NAME or tok.string.startswith("Q"):
            continue
        if tok.string in imported:  # queue.Full / os.O_RDONLY…
            continue
        if i + 2 >= len(toks):
            continue
        dot, attr = toks[i + 1], toks[i + 2]
        if dot.type != tokenize.OP or dot.string != "." or attr.type != tokenize.NAME:
            continue
        # `Qt.WindowType.Tool` 的中段：前面跟着点，不是实例
        if i and toks[i - 1].type == tokenize.OP and toks[i - 1].string == ".":
            continue
        # 后面跟 "(" 的是方法调用，不是枚举成员
        if i + 3 < len(toks) and toks[i + 3].string == "(":
            continue
        if attr.string in _ENUM_MEMBERS:
            found.append((attr.start[0], f"{tok.string}.{attr.string}",
                          sorted(_ENUM_MEMBERS[attr.string])))
    return found


def test_no_short_enums_on_instances():
    """☠️ 上面那道守卫只看 `Q…` 开头的前缀，**实例上取短名它一个都抓不到**。

    第 3 步真装上 PyQt6 之后这个洞当场兑现：`popups.py` 里的
    `cursor.movePosition(cursor.End)`（`cursor` 不以 Q 开头）一路绿着过了
    整个第 1 步，换上 PyQt6 就是 7 个用例一起红。写法必须是
    `QTextCursor.MoveOperation.End`。

    ⚠️ 这道判据比上面那道松——只认成员名，不知道 `cursor` 到底是什么类型，
    所以理论上会误报（某个自己写的对象恰好有个属性叫 `End`）。真误报的时候
    别删这个用例，把那处改个名或者在这里留一条带理由的豁免。本仓库当前是
    零误报：唯一撞上的 `queue.Full` 已经被 import 名单放过了。
    """
    offenders = []
    for path in _source_files():
        for line, expr, candidates in _instance_short_enum_uses(path):
            offenders.append(
                f"{path.relative_to(REPO_ROOT)}:{line}  {expr}"
                f"  →  写全限定名，例如 {candidates[0]}")
    assert not offenders, (
        "这些地方从实例上取了 Qt 的短枚举名，PyQt6 下会 AttributeError：\n  "
        + "\n  ".join(offenders)
    )


def test_instance_guard_actually_detects(tmp_path):
    """反向对照：这道新守卫必须抓得到，否则它只是一直绿。"""
    sample = tmp_path / "sample.py"
    sample.write_text(
        "import queue\n"
        "cursor.movePosition(cursor.End)\n"        # 该抓
        "ok = QTextCursor.MoveOperation.End\n"     # 全限定名，不该抓
        "err = queue.Full\n",                      # import 进来的模块，不该抓
        encoding="utf-8",
    )
    assert [expr for _, expr, _ in _instance_short_enum_uses(sample)] == ["cursor.End"]


def test_no_exec_underscore():
    """`exec_()` 在 PyQt6 里没了（第 1 步就已经全改成 `exec()` 了）。"""
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
        "from PyQt6.QtCore import Qt\n"
        "flag = Qt.AlignLeft\n"
        "ok = Qt.AlignmentFlag.AlignRight\n"
        "text = 'Qt.AlignBottom'  # 字符串里的不算\n",
        encoding="utf-8",
    )
    found = _short_enum_uses(sample)
    assert [(expr, enum) for _, expr, enum in found] == [("Qt.AlignLeft", "AlignmentFlag")]
