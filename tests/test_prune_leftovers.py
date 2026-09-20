"""pip 的 `~xxx` 残留目录：找得到、不误伤、没有 venv 时绝不动手。

背景（2026-09-20）：量 venv 体积时发现 `~orch` 315MB + `~translate2` 60MB
= 375MB 纯垃圾。pip 升级/卸载一个包时会先把原目录改名成 `~xxx`，装完再删；
中途失败或文件被占用，改了名的目录就永远留下了。

☠️ `prune_venv` 原来那套孤儿判据**抓不到它们**——判据走 pip 元数据，而这些
目录没有 dist-info，`pip list` 里根本不出现。所以是单独一条路径，也就需要
单独的测试。

这里全部在 tmp_path 上跑，不碰真 venv。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

from prune_venv import (  # noqa: E402
    find_leftover_dirs, remove_leftover_dirs, site_packages_dir, _human,
)


def _make_site(tmp_path):
    site = tmp_path / "site-packages"
    site.mkdir()
    return site


def test_finds_tilde_dirs(tmp_path):
    site = _make_site(tmp_path)
    (site / "~orch").mkdir()
    (site / "~translate2").mkdir()
    (site / "torch").mkdir()
    found = {p.name for p in find_leftover_dirs(site)}
    assert found == {"~orch", "~translate2"}


def test_never_touches_real_packages(tmp_path):
    """☠️ 判据只认名字以 `~` 开头，别放宽成"看起来像垃圾"。"""
    site = _make_site(tmp_path)
    for name in ("torch", "ctranslate2", "PyQt6", "_distutils_hack",
                 "numpy.libs", "torch-2.14.0.dist-info"):
        (site / name).mkdir()
    assert find_leftover_dirs(site) == []


def test_files_are_not_dirs(tmp_path):
    """只删目录。名字以 `~` 开头的**文件**（编辑器备份之类）不归我们管。"""
    site = _make_site(tmp_path)
    (site / "~notes.txt").write_text("x", encoding="utf-8")
    assert find_leftover_dirs(site) == []


def test_sorted_biggest_first(tmp_path):
    site = _make_site(tmp_path)
    (site / "~small").mkdir()
    (site / "~small" / "a.bin").write_bytes(b"x" * 10)
    (site / "~big").mkdir()
    (site / "~big" / "a.bin").write_bytes(b"x" * 5000)
    assert [p.name for p in find_leftover_dirs(site)] == ["~big", "~small"]


def test_missing_dir_is_not_an_error(tmp_path):
    assert find_leftover_dirs(tmp_path / "nope") == []


def test_remove_deletes_and_reports(tmp_path):
    site = _make_site(tmp_path)
    (site / "~orch").mkdir()
    (site / "~orch" / "deep").mkdir()
    (site / "~orch" / "deep" / "f.bin").write_bytes(b"x" * 100)
    keep = site / "torch"
    keep.mkdir()

    removed, failed = remove_leftover_dirs(find_leftover_dirs(site))
    assert (removed, failed) == (1, [])
    assert not (site / "~orch").exists()
    assert keep.exists(), "真包被删了——判据出问题了"


def test_site_packages_dir_refuses_outside_a_venv(monkeypatch):
    """☠️ 这是硬闸门：调用方会 rmtree 它返回的目录下的东西。

    真有人拿**系统 Python** 跑这个脚本，那就是在系统 site-packages 里递归
    删目录。宁可返回 None 什么都不做。
    """
    monkeypatch.setattr(sys, "prefix", "/usr")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert site_packages_dir() is None
    # 而且上层拿到 None 之后必须是"什么都不找到"，不是抛异常
    assert find_leftover_dirs(None) == []


def test_human_sizes():
    assert _human(512) == "512B"
    assert _human(1536).endswith("KB")
    assert _human(315 * 1024 * 1024) == "315.0MB"
