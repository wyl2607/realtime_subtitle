"""仓库根目录是有清单的：每个文件都要说得出为什么必须在那儿。

为什么值得一条测试：根目录是**唯一没有主人的地方**。包里的东西有模块归属，
`docs/` 有语言目录，`tests/` 有命名约定——只有根目录是"临时放一下"的默认
落点，而临时放的东西没有人会再挪走。这条用例把"根目录该有什么"从口头约定
变成会报错的清单。

☠️ 加文件到根目录前先想清楚它能不能进 `realtime_subtitle/`、`scripts/`、
`docs/` 或 `tests/`。确实必须在根上（构建工具只认根、或者老安装写死了路径），
就往下面的表里加一行**并写清理由**——理由那一列不是装饰，它是下一个人判断
"这个还能不能删"的唯一依据。
"""
import pathlib
import subprocess

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# 文件名 → 为什么它必须在根目录
_ALLOWED_ROOT_FILES = {
    # --- 入口 ---
    "main.py": "实时字幕入口。☠️ 停止脚本按**入口脚本名**识别进程，改名/挪位置会让它认不出自己的进程",
    "download_subtitle.py": "离线视频入口；.ps1 和三语文档里都写死了这个路径",

    # --- 依赖与工具配置（构建/工具只认根） ---
    "requirements.txt": "依赖唯一真相源：install.ps1、deps_fingerprint、CI 的 deps-resolve 都只认它",
    "requirements-dev.txt": "测试依赖，和上面分开是为了不让最终用户白装 pytest",
    "pyproject.toml": "pytest + ruff 配置（2026-09-21 由 pytest.ini + ruff.toml 合并而来）",

    # --- 文档（GitHub 约定：README 必须在根） ---
    "README.md": "GitHub 首页，中文",
    "README.en.md": "英文 README",
    "README.de.md": "德文 README",
    "README.zh.md": "老中文链接的跳转桩，指回 README.md",
    "CLAUDE.md": "给 AI 助手的完整工作手册；约定就在仓库根",
    "LICENSE": "开源许可证，GitHub 在根目录识别",
    ".gitignore": "git 只认根目录这一份",

    # --- 兼容转发壳 ---
    "install.ps1": "转发壳",
    "start_subtitles.ps1": "转发壳",
    "stop_subtitles.ps1": "转发壳",
    "pause_subtitles.ps1": "转发壳",
    "update_subtitles.ps1": "转发壳",
    "uninstall.ps1": "转发壳",
    "download_subtitle.ps1": "转发壳",
}

# ☠️ 这 7 个必须在**根目录**，不能挪进 scripts\windows\。2026-08 之前装的人，
# 桌面 .bat 里内嵌的是仓库根路径——删掉转发壳，他们每一个快捷方式都会报
# "找不到文件"，**包括更新脚本自己**，于是连"更新一下就好了"这条路都没有。
_SHIMS = [name for name, why in _ALLOWED_ROOT_FILES.items() if why == "转发壳"]


def _tracked_root_files():
    out = subprocess.run(["git", "ls-files"], cwd=REPO_ROOT,
                         capture_output=True, text=True, encoding="utf-8")
    assert out.returncode == 0, f"git ls-files 失败：{out.stderr}"
    return sorted(p for p in out.stdout.splitlines() if p and "/" not in p)


def test_no_unexplained_files_at_repo_root():
    unexpected = [f for f in _tracked_root_files() if f not in _ALLOWED_ROOT_FILES]
    assert not unexpected, (
        "根目录多了没有理由的文件：\n  " + "\n  ".join(unexpected)
        + "\n\n先想它能不能进 realtime_subtitle/ 、scripts/ 、docs/ 或 tests/。"
          "确实必须在根上，就往本文件的 _ALLOWED_ROOT_FILES 里加一行并写清理由。")


def test_inventory_has_no_stale_entries():
    """清单里列着、实际却不存在的文件——说明有人删了文件没删清单。"""
    tracked = set(_tracked_root_files())
    stale = [name for name in _ALLOWED_ROOT_FILES if name not in tracked]
    assert not stale, f"清单里这些文件已经不在根目录了：{stale}"


def test_every_shim_forwards_to_a_real_script():
    """☠️ 转发壳指向的脚本必须真实存在，否则老用户双击就是"找不到文件"。"""
    broken = []
    for name in _SHIMS:
        text = (REPO_ROOT / name).read_text(encoding="utf-8-sig")
        target = REPO_ROOT / "scripts" / "windows" / name
        if not target.is_file():
            broken.append(f"{name} → scripts/windows/{name} 不存在")
        elif f"scripts\\windows\\{name}" not in text:
            broken.append(f"{name} 没有转发到 scripts\\windows\\{name}")
    assert not broken, "转发壳坏了：\n  " + "\n  ".join(broken)


def test_shims_stay_thin():
    """转发壳只许转发，不许自己长出业务逻辑——两份行为会漂移。

    放宽到 40 行是因为每个壳里都有一段"为什么别删我"的注释，那段值得留。
    """
    fat = []
    for name in _SHIMS:
        lines = (REPO_ROOT / name).read_text(encoding="utf-8-sig").splitlines()
        code = [ln for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]
        if len(code) > 10:
            fat.append(f"{name}: {len(code)} 行非注释代码")
    assert not fat, ("转发壳里长出逻辑了，挪回 scripts\\windows\\：\n  " + "\n  ".join(fat))
