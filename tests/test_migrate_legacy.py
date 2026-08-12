r"""一次性迁移：把掉进包目录的运行时文件搬回仓库根。

这些用例的底线只有一条：**不许丢用户数据**。搬错位置最多是让人多找一会儿，
覆盖掉别人攒了几个月的德语语料是不可逆的。所以每条用例都在数"文件总数有没有
变少"，而不只是"目标位置有没有东西"。
"""
import os
import time

import pytest

from realtime_subtitle import migrate_legacy


@pytest.fixture
def fake_repo(tmp_path, monkeypatch):
    """造一个假仓库根，并把 migrate_legacy 的 REPO_ROOT 指过去。"""
    monkeypatch.setattr(migrate_legacy, "REPO_ROOT", str(tmp_path))
    (tmp_path / "realtime_subtitle" / "ui").mkdir(parents=True)
    (tmp_path / "realtime_subtitle" / "translate").mkdir(parents=True)
    return tmp_path


def _touch(path, text, mtime=None):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_moves_files_when_root_is_empty(fake_repo):
    """最常见的情况：根目录还没有，包里有 → 直接搬过去。"""
    _touch(fake_repo / "realtime_subtitle/ui/window_state.json", '{"x":1}')
    _touch(fake_repo / "realtime_subtitle/translate/lookup_cache.json", "[]")
    _touch(fake_repo / "realtime_subtitle/translate/transcripts/2026-08-10.txt", "guten tag")

    notes = migrate_legacy.migrate_legacy_runtime_files(verbose=False)

    assert (fake_repo / "window_state.json").read_text(encoding="utf-8") == '{"x":1}'
    assert (fake_repo / "lookup_cache.json").read_text(encoding="utf-8") == "[]"
    assert (fake_repo / "transcripts/2026-08-10.txt").read_text(encoding="utf-8") == "guten tag"
    assert not (fake_repo / "realtime_subtitle/ui/window_state.json").exists()
    assert notes


def test_reproduces_the_authors_machine(fake_repo):
    """精确复刻 2026-08-11 在作者本机看到的现场（就是这个 bug 的原始证据）：

        transcripts\\2026-08-09.txt              重构前，96KB
        ...\\translate\\transcripts\\2026-08-10.txt   重构后，3.8KB
        window_state.json                       8/09 21:15（此后再没被读过）
        ...\\ui\\window_state.json               8/10 23:18（实际在用的）

    期望：两个存档都在根目录（文件名不撞，各自保留）；window_state 用较新的
    那份，被顶掉的旧版留成 .pre-migration.bak——一个字节都不能少。
    """
    old_t = time.time() - 2 * 86400
    new_t = time.time() - 1 * 86400
    _touch(fake_repo / "transcripts/2026-08-09.txt", "A" * 100, old_t)
    _touch(fake_repo / "realtime_subtitle/translate/transcripts/2026-08-10.txt", "B" * 50, new_t)
    _touch(fake_repo / "window_state.json", '{"ver":"08-09"}', old_t)
    _touch(fake_repo / "realtime_subtitle/ui/window_state.json", '{"ver":"08-10"}', new_t)

    migrate_legacy.migrate_legacy_runtime_files(verbose=False)

    # 两个存档都在，内容原样
    assert (fake_repo / "transcripts/2026-08-09.txt").read_text(encoding="utf-8") == "A" * 100
    assert (fake_repo / "transcripts/2026-08-10.txt").read_text(encoding="utf-8") == "B" * 50
    # 窗口状态用 8/10 那份（较新 = 重构后一直在用的）
    assert (fake_repo / "window_state.json").read_text(encoding="utf-8") == '{"ver":"08-10"}'
    # 被顶掉的 8/09 那份没有蒸发
    assert (fake_repo / "window_state.json.pre-migration.bak").read_text(
        encoding="utf-8") == '{"ver":"08-09"}'
    # 包目录已清空
    assert not (fake_repo / "realtime_subtitle/ui/window_state.json").exists()
    assert not (fake_repo / "realtime_subtitle/translate/transcripts").exists()


def test_same_day_archive_collision_keeps_both(fake_repo):
    """同一天两边都写过（重构当天最可能）：绝不覆盖，搬成 .migrated.txt。"""
    _touch(fake_repo / "transcripts/2026-08-10.txt", "根目录写的")
    _touch(fake_repo / "realtime_subtitle/translate/transcripts/2026-08-10.txt", "包里写的")

    migrate_legacy.migrate_legacy_runtime_files(verbose=False)

    assert (fake_repo / "transcripts/2026-08-10.txt").read_text(encoding="utf-8") == "根目录写的"
    assert (fake_repo / "transcripts/2026-08-10.migrated.txt").read_text(
        encoding="utf-8") == "包里写的"


def test_newer_root_file_is_not_clobbered(fake_repo):
    """根目录那份反而更新：一个字节都不许动，包里的也原地留着让人自己判断。"""
    _touch(fake_repo / "window_state.json", '{"ver":"new"}', time.time())
    _touch(fake_repo / "realtime_subtitle/ui/window_state.json", '{"ver":"old"}',
           time.time() - 86400)

    migrate_legacy.migrate_legacy_runtime_files(verbose=False)

    assert (fake_repo / "window_state.json").read_text(encoding="utf-8") == '{"ver":"new"}'
    assert (fake_repo / "realtime_subtitle/ui/window_state.json").exists()


def test_transient_flags_are_never_migrated(fake_repo):
    """☠️ .paused / .stop 是"当前状态"不是数据：搬一个过期的 .stop 过来，
    程序会刚启动就自己退出。"""
    _touch(fake_repo / "realtime_subtitle/capture/.stop", "")
    _touch(fake_repo / "realtime_subtitle/capture/.paused", "")

    migrate_legacy.migrate_legacy_runtime_files(verbose=False)

    assert not (fake_repo / ".stop").exists(), "过期的 .stop 被搬到根目录 = 启动即退出"
    assert not (fake_repo / ".paused").exists()


def test_is_idempotent(fake_repo):
    """跑两遍和跑一遍结果一样（每次启动都会调，不能越跑越多）。"""
    _touch(fake_repo / "realtime_subtitle/ui/window_state.json", '{"x":1}')
    _touch(fake_repo / "realtime_subtitle/translate/transcripts/2026-08-10.txt", "x")

    migrate_legacy.migrate_legacy_runtime_files(verbose=False)
    after_first = sorted(p.name for p in fake_repo.rglob("*") if p.is_file())
    assert migrate_legacy.migrate_legacy_runtime_files(verbose=False) == []
    assert sorted(p.name for p in fake_repo.rglob("*") if p.is_file()) == after_first


def test_nothing_to_do_is_silent(fake_repo):
    """干净安装（全新 clone）不该打印任何迁移信息。"""
    assert migrate_legacy.migrate_legacy_runtime_files(verbose=False) == []


def test_never_loses_a_single_byte(fake_repo):
    """总体不变量：迁移前后，所有文件的内容集合必须完全相同。"""
    _touch(fake_repo / "transcripts/2026-08-09.txt", "alpha")
    _touch(fake_repo / "transcripts/2026-08-10.txt", "beta")
    _touch(fake_repo / "realtime_subtitle/translate/transcripts/2026-08-10.txt", "gamma")
    _touch(fake_repo / "realtime_subtitle/translate/transcripts/2026-08-11.txt", "delta")
    _touch(fake_repo / "window_state.json", "eps", time.time() - 86400)
    _touch(fake_repo / "realtime_subtitle/ui/window_state.json", "zeta", time.time())

    before = sorted(p.read_text(encoding="utf-8") for p in fake_repo.rglob("*") if p.is_file())
    migrate_legacy.migrate_legacy_runtime_files(verbose=False)
    after = sorted(p.read_text(encoding="utf-8") for p in fake_repo.rglob("*") if p.is_file())

    assert before == after, "迁移丢了或改了内容——这是绝对不能发生的"


def test_migration_failure_does_not_block_startup(fake_repo, monkeypatch):
    """迁移炸了也必须能启动：大不了维持"数据劈开"的现状。"""
    _touch(fake_repo / "realtime_subtitle/ui/window_state.json", "{}")

    def boom(*a, **k):
        raise OSError("disk on fire")
    monkeypatch.setattr(migrate_legacy.shutil, "move", boom)

    migrate_legacy.migrate_legacy_runtime_files(verbose=False)  # 不抛
    assert (fake_repo / "realtime_subtitle/ui/window_state.json").exists()
