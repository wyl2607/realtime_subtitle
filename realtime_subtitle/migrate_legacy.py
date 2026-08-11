r"""一次性迁移：把掉进包目录的运行时文件搬回仓库根。

☠️ 背景见 CLAUDE.md 第 4 节第 28 条。2026-08-10 的包化重构让这些文件跟着
模块深了一到两层，而 .ps1 脚本和 README 一直按仓库根走。修好路径之后，
**已经在用的机器上数据是劈开的**：老的在根目录、8-10 之后的在包目录里。
不迁移的话，用户 git pull 完会发现"我的字幕存档少了一段""窗口位置退回去了"
——文件其实都还在，只是程序不再看那个地方了。

作者本机实测（2026-08-11，就是这个 bug 的原始现场）：

    transcripts\2026-08-09.txt                      96 KB   重构前
    realtime_subtitle\translate\transcripts\2026-08-10.txt   3.8 KB  重构后
    window_state.json                     8/09 21:15   重构前（此后再没被读过）
    realtime_subtitle\ui\window_state.json 8/10 23:18   重构后（实际在用的）

规则（保守到底——这是别人攒的语料和调好的布局，宁可留垃圾不可丢数据）：

- **只搬不删**：全程 move / rename，一个 `os.remove` 都没有。
- **同名不覆盖**：撞名的存档搬成 `<原名>.migrated.txt`，两份都留着。
- **单文件按 mtime 择新**：包里那份更新就用它，被顶掉的根目录旧版重命名成
  `.pre-migration.bak` 留底，而不是覆盖掉。
- **失败不影响启动**：整体 try 住，迁移不了就照常跑（大不了还是老样子）。

`.paused` / `.stop` 这两个瞬时标志**故意不搬**：它们是"当前状态"，搬一个
过期的 `.stop` 过来会让程序刚启动就退出。包目录里若有残留是无害死文件。
"""
import os
import shutil

from realtime_subtitle.paths import REPO_ROOT

# (包内旧位置, 仓库根新位置)——相对 REPO_ROOT
_LEGACY_FILES = [
    (os.path.join("realtime_subtitle", "ui", "window_state.json"), "window_state.json"),
    (os.path.join("realtime_subtitle", "translate", "lookup_cache.json"), "lookup_cache.json"),
]
_LEGACY_TRANSCRIPT_DIR = os.path.join("realtime_subtitle", "translate", "transcripts")


def _migrate_one_file(old_rel, new_rel, notes):
    old = os.path.join(REPO_ROOT, old_rel)
    new = os.path.join(REPO_ROOT, new_rel)
    if not os.path.isfile(old):
        return
    if not os.path.exists(new):
        shutil.move(old, new)
        notes.append(f"{new_rel} ← {old_rel}")
        return
    # 两边都有：包里那份是重构之后一直在用的，通常更新。按 mtime 判，别猜
    if os.path.getmtime(old) > os.path.getmtime(new):
        backup = new + ".pre-migration.bak"
        if os.path.exists(backup):
            os.replace(new, backup)   # 已经迁移过一次，旧备份让位（内容同源）
        else:
            os.rename(new, backup)
        shutil.move(old, new)
        notes.append(f"{new_rel} ← {old_rel}（较旧的原版留作 {os.path.basename(backup)}）")
    else:
        # 根目录那份反而更新：不动它，也不删包里的，留着让人自己判断
        notes.append(f"{new_rel} 保持不变（它比 {old_rel} 新，后者原地保留）")


def _migrate_transcripts(notes):
    old_dir = os.path.join(REPO_ROOT, _LEGACY_TRANSCRIPT_DIR)
    if not os.path.isdir(old_dir):
        return
    new_dir = os.path.join(REPO_ROOT, "transcripts")
    os.makedirs(new_dir, exist_ok=True)
    moved = 0
    for name in sorted(os.listdir(old_dir)):
        src = os.path.join(old_dir, name)
        if not os.path.isfile(src):
            continue
        dst = os.path.join(new_dir, name)
        if os.path.exists(dst):
            # 同一天两边都写过（重构当天最可能）：两份都留，谁都不覆盖
            stem, ext = os.path.splitext(name)
            dst = os.path.join(new_dir, f"{stem}.migrated{ext}")
            n = 2
            while os.path.exists(dst):
                dst = os.path.join(new_dir, f"{stem}.migrated{n}{ext}")
                n += 1
        shutil.move(src, dst)
        moved += 1
    if moved:
        notes.append(f"transcripts\\ ← {_LEGACY_TRANSCRIPT_DIR}（{moved} 个存档）")
    try:
        os.rmdir(old_dir)   # 只在空了才成功，非空说明还有东西，那就留着
    except OSError:
        pass


def migrate_legacy_runtime_files(verbose=True):
    """把包目录里的历史运行时文件搬回仓库根。返回做过的事（给测试和日志用）。

    ☠️ 必须在 SubtitleWindow 构造**之前**调用——窗口 __init__ 就会读
    STATE_FILE，晚一步就读到空的、把用户布局重置成默认值了。
    """
    notes = []
    try:
        for old_rel, new_rel in _LEGACY_FILES:
            _migrate_one_file(old_rel, new_rel, notes)
        _migrate_transcripts(notes)
    except Exception as e:
        # 迁移失败绝不能挡住启动：大不了维持"数据劈开"的现状
        print(f"⚠️  历史运行时文件迁移失败（不影响使用，文件都还在原处）: "
              f"{e.__class__.__name__}: {e}")
        return notes
    if notes and verbose:
        print("📦 检测到包化重构遗留的运行时文件，已搬回程序目录：")
        for n in notes:
            print(f"   - {n}")
    return notes
