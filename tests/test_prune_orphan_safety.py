"""☠️ 孤儿判据不许删掉"还在被装着的包依赖着"的东西。

2026-09-20 差点出的事（发现时 CLAUDE.md 第 44 条已经在教用户跑
`prune_venv.py --yes` 清 PyQt5 了，也就是说这条建议已经发出去了）：

    装着的   huggingface_hub 1.21.0  → Requires 里**硬依赖** typer
    闭包解出 huggingface_hub 1.32.0  → 新版不再依赖 typer

`resolve_closure()` 算的是"全新安装会装成什么样"，于是
typer / rich / markdown-it-py / mdurl / shellingham 全被判成孤儿。真删了，
**装着的 1.21.0 就 import 不了**——而 faster-whisper 正是靠 huggingface_hub
去下 Whisper 模型。

这个 bug 的恶劣之处在归因难度：pip 不会自动降级 huggingface_hub，所以删完
当场一切正常，要等到"换台机器/清了模型缓存、第一次下模型"才爆，那时没人
会想起几周前跑过一次清理。

修法：保留集 = 闭包 ∪ 从闭包出发在**已安装**依赖图上能走到的一切。
下面两组用例分别钉"别多删"和"别少删"——只有前者会让人不敢再跑这个脚本，
只有后者会让脚本变成空转。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from prune_venv import expand_with_installed_requirements  # noqa: E402
from realtime_subtitle.deps_fingerprint import orphan_packages  # noqa: E402

# 本机真实形状的缩影：闭包里有 huggingface_hub，但装着的那个老版本还要 typer，
# typer 又要 rich，rich 又要 markdown-it-py → mdurl。
_GRAPH = {
    "huggingface-hub": {"typer", "tqdm", "filelock"},
    "typer": {"rich", "click", "shellingham"},
    "rich": {"markdown-it-py"},
    "markdown-it-py": {"mdurl"},
    "mdurl": set(),
    "tqdm": set(), "filelock": set(), "click": set(), "shellingham": set(),
    "pyqt5": set(), "pyqt5-qt5": set(), "pyqt5-sip": set(),
    "librosa": {"numba"}, "numba": set(),
}


def test_keeps_what_an_installed_package_still_needs():
    """☠️ 正题：typer 那一串必须被保下来。"""
    keep = expand_with_installed_requirements({"huggingface_hub"}, _GRAPH)
    for name in ("typer", "rich", "markdown-it-py", "mdurl", "shellingham", "click"):
        assert name in keep, f"{name} 被漏掉了——装着的 huggingface_hub 会 import 不了"


def test_still_finds_genuinely_unreachable_packages():
    """反向：别因为怕删错就谁也不删——PyQt5 必须照样判成孤儿。

    这正是第 44 条让用户跑这个脚本的原因。保下 typer 的同时如果把 PyQt5
    也保了，那条建议就失效了。
    """
    keep = expand_with_installed_requirements({"huggingface_hub"}, _GRAPH)
    installed = ["huggingface_hub", "typer", "rich", "PyQt5", "PyQt5-Qt5", "PyQt5_sip"]
    orphans = orphan_packages(installed, keep)
    assert orphans == ["PyQt5", "PyQt5-Qt5", "PyQt5_sip"]


def test_unrelated_leftovers_are_still_orphans():
    """librosa 是 faster-whisper ≤1.0 时代的残留（见 deps_fingerprint 注释）。
    它自己有依赖（numba），但**没人依赖它**，所以两个都该走。"""
    keep = expand_with_installed_requirements({"huggingface_hub"}, _GRAPH)
    assert orphan_packages(["librosa", "numba"], keep) == ["librosa", "numba"]


def test_expansion_terminates_on_cycles():
    """依赖图里出现环不能把脚本转死（真实 metadata 里循环依赖并不罕见）。"""
    cyclic = {"a": {"b"}, "b": {"c"}, "c": {"a"}}
    assert expand_with_installed_requirements({"a"}, cyclic) == {"a", "b", "c"}


def test_names_are_canonicalised_on_the_way_in():
    """闭包里是 huggingface_hub（下划线），图里是 huggingface-hub（连字符）。
    不做 PEP 503 规范化的话这一步会直接走不进去，整个扩展变成空操作。"""
    keep = expand_with_installed_requirements({"HuggingFace_Hub"}, _GRAPH)
    assert "typer" in keep


def test_real_metadata_graph_is_not_empty():
    """☠️ 上面全是构造的图。这一条确认**真实**的 importlib.metadata 路径也能
    走通——否则单元测试全绿而线上那一步是空的，等于没修。"""
    from prune_venv import installed_requirement_graph
    graph = installed_requirement_graph()
    assert len(graph) > 20, "读不到已安装包的依赖图"
    assert any(deps for deps in graph.values()), "每个包的依赖都是空集，解析坏了"
