"""☠️ 含中文的 .ps1 必须 UTF-8 带 BOM（CLAUDE.md 第 4 节第 5 条）。

为什么值得单独一个测试文件：这条规则**只在中文 Windows 上才暴露**，
而且症状不是"中文显示成乱码"那么温柔——Windows PowerShell 5.1 没有 BOM 时
按 ANSI(GBK) 读，多字节序列被拆开，引号会被吃掉，直接变成语法错误：

    字符串缺少终止符: "。

2026-08-10 实测：目录重构把 6 个 .ps1 挪进 scripts\\windows\\ 时 BOM 全丢了，
在中文 Windows 上 `[Parser]::ParseFile` 报 install.ps1 19 处、uninstall.ps1
15 处、stop 6 处错误——也就是**全新安装和所有桌面快捷方式全部跑不起来**。
CI 跑在英文 locale 的 runner 上，这类问题一个都测不出来，只能靠这条静态断言。

纯字节检查，不需要 PowerShell，两个 CI job 都会跑到。
"""
import pathlib

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
UTF8_BOM = b"\xef\xbb\xbf"


def _has_cjk(text):
    return any("一" <= ch <= "鿿" for ch in text)


def _all_ps1():
    return sorted(REPO_ROOT.rglob("*.ps1"))


def test_ps1_files_exist_where_expected():
    """防止这个测试因为路径写错而静默通过（rglob 找不到文件也不会失败）。"""
    names = {p.name for p in _all_ps1()}
    assert {"install.ps1", "start_subtitles.ps1", "stop_subtitles.ps1"} <= names, names


def test_ps1_with_chinese_has_utf8_bom():
    offenders = []
    for path in _all_ps1():
        raw = path.read_bytes()
        if raw.startswith(UTF8_BOM):
            continue
        if _has_cjk(raw.decode("utf-8", "ignore")):
            offenders.append(str(path.relative_to(REPO_ROOT)))
    assert not offenders, (
        "这些 .ps1 含中文但没有 UTF-8 BOM，在中文 Windows 上会被按 ANSI 读、"
        "引号被吃掉变成语法错误（CLAUDE.md 第 4 节第 5 条）：\n  "
        + "\n  ".join(offenders)
    )


def test_ps1_is_valid_utf8():
    """顺带挡住"被某个编辑器存成 GBK"这种更彻底的坏法。"""
    bad = []
    for path in _all_ps1():
        raw = path.read_bytes()
        body = raw[len(UTF8_BOM):] if raw.startswith(UTF8_BOM) else raw
        try:
            body.decode("utf-8")
        except UnicodeDecodeError:
            bad.append(str(path.relative_to(REPO_ROOT)))
    assert not bad, f"这些 .ps1 不是合法 UTF-8：{bad}"
