"""README 里写的版本号必须和 version.py 对得上。

☠️ 这条是真漂过的：2026-09-20 查的时候 `version.py` 是 2.6.0（而且 v2.6.0
的 tag 都打出去了），README 第一屏还写着 **v2.5.0**，CLAUDE.md 第 3 节也写着
"当前 2.5.0"。也就是说**落后了整整一个已发布版本，没有任何人发现**。

为什么值得一条测试：README 第一屏那个版本号是给**陌生人**看的第一眼信息，
而发布流程（改 version.py → commit → tag → push）里没有任何一步会路过它。
漂了不会报错、不会有人抱怨，只会让人拿错版本号去开 issue。

判据只钉一个方向：README 提到的版本必须**等于** version.py。CLAUDE.md 那处
已经改成"以 version.py 为准"不再写死数字，所以不在这里钉。
"""
import pathlib
import re

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]


def _declared_version():
    """不 import，用最朴素的正则读——和 install.ps1 / update_subtitles.ps1
    读它的方式一致（version.py 必须保持纯常量、零 import，见 CLAUDE.md 第 3 节）。"""
    text = (REPO_ROOT / "realtime_subtitle" / "version.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', text, re.M)
    assert match, "version.py 里读不到 __version__——写法变了？改这里之前先看第 3 节"
    return match.group(1)


def test_readme_version_matches_version_py():
    version = _declared_version()
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    mentioned = re.findall(r"当前版本 \*\*v([0-9]+\.[0-9]+\.[0-9]+)\*\*", readme)
    assert mentioned, "README.md 里找不到「当前版本 **vX.Y.Z**」这句——它被改写了就同步改本测试"
    wrong = [v for v in mentioned if v != version]
    assert not wrong, (
        f"README.md 写的是 v{wrong[0]}，version.py 是 {version}。"
        "发布流程不会路过 README，只能靠这条测试拦住。")


def test_version_is_a_plain_semver_triple():
    """`主.次.修` 三段纯数字。tag 名是 v+这个值，别塞 rc/dev 后缀进来——
    update_subtitles.ps1 拿 Select-String 正则读它，多一段会解析不出来。"""
    version = _declared_version()
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", version), \
        f"版本号不是纯三段数字：{version!r}"


def test_version_py_stays_import_free():
    """☠️ version.py 必须保持零 import（CLAUDE.md 第 3 节）。

    update_subtitles.ps1 和 issue 模板都用 Select-String 正则读它——这样
    venv 坏掉、甚至还没建 venv 的时候也读得到版本号。往里加 import 会把
    那条路径弄坏，而且坏的时候正是"环境出问题、最需要知道版本号"的时候。
    """
    text = (REPO_ROOT / "realtime_subtitle" / "version.py").read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines()
                     if not line.lstrip().startswith("#"))
    offenders = re.findall(r"^\s*(?:import|from)\s+\S+", code, re.M)
    assert not offenders, f"version.py 里出现了 import：{offenders}"
