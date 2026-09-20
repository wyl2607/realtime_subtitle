r"""清掉 venv 里已经没人要的包。

为什么需要它：依赖指纹（realtime_subtitle/deps_fingerprint.py）只认
requirements.txt 的**文本**，而 `pip install -r` 从不卸载东西。从 requirements
里删掉一行之后，那个包和它拖来的一串传递依赖会在所有已装好的机器上永远留着。

判据不是"我觉得哪些包没用"，而是 pip 自己解出来的依赖闭包：

    pip install --dry-run --ignore-installed --report <json> -r requirements.txt

装了、但不在闭包里 → 孤儿。requirements-dev.txt 存在时一并算进闭包，
免得把改代码的人装的 pytest/ruff 当成孤儿删掉。

☠️ 故意**不按 CPU/GPU 档位过滤 nvidia-***：闭包一律按完整的 requirements.txt
算，这样 CPU 档机器上万一留着 GPU 档的 CUDA 运行库也不会被这个脚本删——
删错一个 CUDA 库的代价（程序起不来）远大于多留几百兆。

第二类垃圾：**`~` 开头的 pip 残留目录**（2026-09-20 加）。pip 在升级/卸载
一个包时会先把原目录改名成 `~xxx`，装完再删；中途失败、被杀毒软件拦、或者
Windows 上文件被占用，这个改了名的目录就永远留下了。本机实测 `~orch` 315MB
+ `~translate2` 60MB = **375MB 纯垃圾**。

☠️ 上面那套孤儿判据**抓不到它们**：判据走的是 pip 自己的元数据，而这些目录
没有 dist-info，`pip list` 里根本不出现。所以只能按目录名单独找一遍。
安全性来自名字本身——`~` 不是合法的 Python 标识符，这种目录**永远不可能被
import**，删了不会让任何东西少一个模块。

用法：
    venv\Scripts\python scripts\prune_venv.py            # 只看，不动
    venv\Scripts\python scripts\prune_venv.py --yes      # 真删
    venv\Scripts\python scripts\prune_venv.py --check    # 给更新脚本用，只打一行提示
"""
from __future__ import annotations

import argparse
import importlib.metadata
import json
import re
import shutil
import subprocess
import sys
import sysconfig
import tempfile
from pathlib import Path

# 控制台可能默认是 cp1252/gbk，中文和 emoji 会让 print 直接抛异常（同 app.py 开头）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realtime_subtitle.deps_fingerprint import canonical_name, orphan_packages  # noqa: E402
from realtime_subtitle.paths import REPO_ROOT  # noqa: E402


def _pip(*args):
    return subprocess.run(
        [sys.executable, "-m", "pip", *args],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )


def resolve_closure(req_files) -> set[str]:
    """pip 解出来的完整依赖闭包（包名集合）。只拉 metadata，不下载 wheel。"""
    names: set[str] = set()
    for req in req_files:
        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "report.json"
            args = ["install", "--dry-run", "--ignore-installed", "--quiet",
                    "--report", str(report), "-r", str(req)]
            proc = _pip(*args)
            if proc.returncode != 0 or not report.is_file():
                raise RuntimeError(
                    f"解析 {req.name} 失败（多半是网络问题，稍后重试）：\n"
                    f"{proc.stderr.strip() or proc.stdout.strip()}"
                )
            # ☠️ 必须指定 utf-8：报告里有非 ASCII 字符（作者名/摘要），
            # Windows 默认 cp1252/gbk 读会 UnicodeDecodeError
            data = json.loads(report.read_text(encoding="utf-8"))
        names |= {item["metadata"]["name"] for item in data.get("install", [])}
    return names


def installed_packages() -> list[str]:
    proc = _pip("list", "--format=json")
    if proc.returncode != 0:
        raise RuntimeError(f"pip list 失败：\n{proc.stderr.strip()}")
    return [item["name"] for item in json.loads(proc.stdout)]


def installed_requirement_graph() -> dict[str, set[str]]:
    """装着的每个包**自己声明**要什么：{规范名: {规范名, ...}}。

    ☠️ 一律不看 extra / 环境标记，全都算成依赖。这个方向的错误是"多留几个
    包"，反方向的错误是"删掉一个还在被用的包"——后者的代价大得多（同本文件
    开头对 nvidia-* 的处理）。
    """
    graph: dict[str, set[str]] = {}
    for dist in importlib.metadata.distributions():
        name = dist.metadata["Name"]
        if not name:
            continue
        deps = set()
        for req in (dist.requires or []):
            # "typer<0.26.0,>=0.20.0" / "pytest ; extra == 'test'" → "typer" / "pytest"
            head = re.split(r"[\s\[<>=!~;(]", req.strip(), maxsplit=1)[0]
            if head:
                deps.add(canonical_name(head))
        graph[canonical_name(name)] = deps
    return graph


def expand_with_installed_requirements(keep, graph=None) -> set[str]:
    """从 keep 出发，沿着**已安装**包的依赖声明传递闭包。

    ☠️ 这一步是 2026-09-20 补的，补的是一个会把 venv 搞坏的真 bug：

    `resolve_closure()` 算的是"**全新安装**会装成什么样"，而 venv 里装着的
    往往是**更老的版本**，老版本的依赖可能更多。本机实测：

        装着的   huggingface_hub 1.21.0  → 硬依赖 typer（Requires 里就有）
        闭包解出 huggingface_hub 1.32.0  → 不再依赖 typer

    于是 typer/rich/markdown-it-py/mdurl/shellingham 被判成孤儿。真删了，
    **装着的那个 1.21.0 就 import 不了** —— 而 faster-whisper 正是靠它去
    HuggingFace 下 Whisper 模型。症状还特别难归因：pip 不会降级
    huggingface_hub，所以问题要等到"换台机器/清了模型缓存、第一次下模型"
    才爆，而那时没人会想到是几周前跑过一次清理。

    判据因此改成：**保留集 = 闭包 ∪ 从闭包出发在已安装依赖图上能走到的一切**。
    """
    if graph is None:
        graph = installed_requirement_graph()
    seen = {canonical_name(n) for n in keep}
    stack = list(seen)
    while stack:
        for dep in graph.get(stack.pop(), ()):
            if dep not in seen:
                seen.add(dep)
                stack.append(dep)
    return seen


def find_orphans() -> list[str]:
    req_files = [REPO_ROOT / "requirements.txt"]
    dev = REPO_ROOT / "requirements-dev.txt"
    if dev.is_file():
        req_files.append(dev)
    keep = expand_with_installed_requirements(resolve_closure(req_files))
    return orphan_packages(installed_packages(), keep)


# --- pip 残留目录（`~` 开头） ---------------------------------------------

def site_packages_dir() -> Path | None:
    """当前解释器的 site-packages。不在 venv 里时返回 None。

    ☠️ 返回 None 是硬闸门，不是"取不到路径"的兜底：本函数的调用方会 rmtree
    它给出的目录下的东西。真有人拿**系统 Python** 跑这个脚本，那就是在系统
    site-packages 里递归删目录——所以宁可什么都不做。
    """
    if sys.prefix == sys.base_prefix:      # 不在虚拟环境里
        return None
    path = sysconfig.get_paths().get("purelib")
    return Path(path) if path else None


def find_leftover_dirs(site_dir: Path | None = None) -> list[Path]:
    """pip 留下的 `~xxx` 残留目录，按体积从大到小。

    只认**目录**且名字以 `~` 开头。判据就这么窄，因为它同时也是安全性的
    全部来源：`~` 开头的名字不是合法 Python 标识符，这种东西永远不会被
    import，删掉不可能让任何 import 失败。别把判据放宽成"看起来像垃圾"。
    """
    if site_dir is None:
        site_dir = site_packages_dir()
    if site_dir is None or not site_dir.is_dir():
        return []
    found = [p for p in site_dir.iterdir() if p.is_dir() and p.name.startswith("~")]
    return sorted(found, key=lambda p: -_dir_size(p))


def _dir_size(path: Path) -> int:
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            pass                            # 权限/符号链接坏了都不该中断统计
    return total


def _human(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024.0
    return f"{size:.1f}GB"


def remove_leftover_dirs(dirs) -> tuple[int, list[str]]:
    """删掉残留目录，返回 (删成功几个, 失败原因列表)。

    失败不抛：Windows 上目录被别的进程占着是常事（杀毒软件正在扫、
    上一个 python 还没退干净），而这只是清理，没删掉不影响任何功能。
    """
    removed, failed = 0, []
    for path in dirs:
        try:
            shutil.rmtree(path)
            removed += 1
        except OSError as e:
            failed.append(f"{path.name}: {e}")
    return removed, failed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清掉 venv 里不在依赖闭包中的包")
    ap.add_argument("--yes", action="store_true", help="真的卸载（默认只列出）")
    ap.add_argument("--check", action="store_true",
                    help="给更新脚本用：有孤儿就打一行提示，没有则静默")
    args = ap.parse_args(argv)

    # 残留目录先找：它不联网、不会失败，就算下面解闭包因为断网挂了，
    # 这部分结果照样有用。
    leftovers = find_leftover_dirs()
    leftover_bytes = sum(_dir_size(p) for p in leftovers)

    try:
        orphans = find_orphans()
    except RuntimeError as e:
        # 检查模式下失败不该把更新流程弄红——这只是个清理建议
        print(f"⚠️ 无法检查多余依赖：{e}", file=sys.stderr)
        if not leftovers:
            return 0 if args.check else 1
        orphans = []            # 闭包算不出来，但残留目录仍然可以报/可以删

    if not orphans and not leftovers:
        if not args.check:
            print("✅ venv 干净：没有多余的包，也没有 pip 残留目录。")
        return 0

    if args.check:
        # ☠️ 这句话是印给最终用户看的，**只许提真实存在的入口**。原文写的是
        # "更新字幕时加 -Prune 参数"，而 2026-09-20 合并桌面入口之后
        # 「更新字幕.bat」已经不在桌面上了（第 4 节第 45 条）——照那句话做的人
        # 会先去找一个不存在的图标。这里只留一条任何人都能照抄的命令。
        bits = []
        if orphans:
            bits.append(f"{len(orphans)} 个包不在依赖清单里了"
                        f"（{', '.join(orphans[:3])} 等）")
        if leftovers:
            bits.append(f"{len(leftovers)} 个 pip 残留目录"
                        f"（{_human(leftover_bytes)}）")
        print(f"ℹ️ venv 里有 {'、'.join(bits)}，留着不影响使用，只占磁盘。"
              f"想清掉就在程序目录里跑："
              f"venv\\Scripts\\python scripts\\prune_venv.py --yes")
        return 0

    if orphans:
        print(f"发现 {len(orphans)} 个不在依赖闭包里的包：")
        for name in orphans:
            print(f"   - {name}")
    if leftovers:
        print(f"发现 {len(leftovers)} 个 pip 残留目录（共 {_human(leftover_bytes)}）："
              f"\n   pip 升级/卸载时改名留下的，没有 dist-info、也不可能被 import")
        for path in leftovers:
            print(f"   - {path.name}  {_human(_dir_size(path))}")

    if not args.yes:
        print("\n只是列出来看看。真要删：加 --yes 重跑。")
        return 0

    failed_hard = False
    if orphans:
        proc = _pip("uninstall", "-y", *orphans)
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            print("❌ 卸载失败。venv 还是可用的，重跑 install.ps1 可以补回任何缺失的包。")
            failed_hard = True
        else:
            print(f"✅ 已清掉 {len(orphans)} 个包。")

    if leftovers:
        removed, failed = remove_leftover_dirs(leftovers)
        if removed:
            print(f"✅ 已删掉 {removed} 个 pip 残留目录，回收 {_human(leftover_bytes)}。")
        for line in failed:
            # 删不掉不算失败：多半是被占用，下次再跑就好了
            print(f"   ⚠️ 删不掉（多半被占用，下次再跑即可）：{line}")

    return 1 if failed_hard else 0


if __name__ == "__main__":
    raise SystemExit(main())
