"""文档里的相对链接必须指向真实存在的文件。

☠️ 这条是踩出来的：2026-09-21 把 `docs/WINDOWS-RUNBOOK.md` 挪进 `docs/en/`
的时候，四个 README + README-i18n + STRUCTURE 一共六处链接全部失效。没有这个
测试的话，它们会静静地 404 下去——**坏链接不会让任何东西报错**，只会让第一次
点进来的人以为项目没人维护。

判据只管**仓库内的相对链接**：
- `http(s)://` 外链不查（要联网，而且别人的站挂了不该让我们的 CI 红）
- `#anchor` 纯锚点不查
- 链接里的 `#anchor` 部分会被切掉再查文件

顺带钉住三语 runbook 互相指得到——那是"专业小项目"和"一堆散文件"的区别。
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 只查人会点的文档；docs/design/ 是历史快照，里面的链接允许指向已经没有的东西
_DOC_FILES = [
    "README.md", "README.en.md", "README.de.md", "README.zh.md",
    "CLAUDE.md",
    "docs/STRUCTURE.md", "docs/README-i18n.md", "docs/TODO.md",
    "docs/zh/WINDOWS-RUNBOOK.md",
    "docs/en/WINDOWS-RUNBOOK.md",
    "docs/de/WINDOWS-RUNBOOK.md",
]

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _relative_links(path: pathlib.Path):
    text = path.read_text(encoding="utf-8")
    # ``` 代码块里的东西不是链接，别把示例路径当成坏链接
    text = re.sub(r"```.*?```", "", text, flags=re.S)
    for target in _LINK.findall(text):
        target = target.strip()
        if target.startswith(("http://", "https://", "#", "mailto:")):
            continue
        yield target.split("#", 1)[0]


def test_every_relative_doc_link_resolves():
    broken = []
    for rel in _DOC_FILES:
        src = REPO_ROOT / rel
        assert src.is_file(), f"文档清单里的 {rel} 自己就不存在"
        for target in _relative_links(src):
            if not target:
                continue
            if not (src.parent / target).resolve().exists():
                broken.append(f"{rel} → {target}")
    assert not broken, "这些相对链接指向不存在的文件：\n  " + "\n  ".join(broken)


def test_three_runbooks_exist_and_cross_link():
    """三语操作手册必须互相指得到，否则等于只有一种语言的人找得到路。"""
    books = {lang: REPO_ROOT / "docs" / lang / "WINDOWS-RUNBOOK.md"
             for lang in ("zh", "en", "de")}
    missing = [lang for lang, p in books.items() if not p.is_file()]
    assert not missing, f"缺少这些语言的操作手册：{missing}"

    for lang, path in books.items():
        text = path.read_text(encoding="utf-8")
        for other in books:
            if other == lang:
                continue
            assert f"../{other}/WINDOWS-RUNBOOK.md" in text, \
                f"{lang} 版没有指向 {other} 版的链接"


def test_readmes_point_at_their_own_language_runbook():
    """☠️ 英文 README 链到中文手册是真出过的事（当时只有 EN/ZH 两本）。"""
    expected = {"README.en.md": "docs/en/WINDOWS-RUNBOOK.md",
                "README.de.md": "docs/de/WINDOWS-RUNBOOK.md",
                "README.md": "docs/zh/WINDOWS-RUNBOOK.md",
                "README.zh.md": "docs/zh/WINDOWS-RUNBOOK.md"}
    for name, target in expected.items():
        text = (REPO_ROOT / name).read_text(encoding="utf-8")
        assert target in text, f"{name} 没有链到 {target}"
