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

用法：
    venv\Scripts\python scripts\prune_venv.py            # 只看，不动
    venv\Scripts\python scripts\prune_venv.py --yes      # 真删
    venv\Scripts\python scripts\prune_venv.py --check    # 给更新脚本用，只打一行提示
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import tempfile
from pathlib import Path

# 控制台可能默认是 cp1252/gbk，中文和 emoji 会让 print 直接抛异常（同 app.py 开头）
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from realtime_subtitle.deps_fingerprint import orphan_packages  # noqa: E402
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


def find_orphans() -> list[str]:
    req_files = [REPO_ROOT / "requirements.txt"]
    dev = REPO_ROOT / "requirements-dev.txt"
    if dev.is_file():
        req_files.append(dev)
    return orphan_packages(installed_packages(), resolve_closure(req_files))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="清掉 venv 里不在依赖闭包中的包")
    ap.add_argument("--yes", action="store_true", help="真的卸载（默认只列出）")
    ap.add_argument("--check", action="store_true",
                    help="给更新脚本用：有孤儿就打一行提示，没有则静默")
    args = ap.parse_args(argv)

    try:
        orphans = find_orphans()
    except RuntimeError as e:
        # 检查模式下失败不该把更新流程弄红——这只是个清理建议
        print(f"⚠️ 无法检查多余依赖：{e}", file=sys.stderr)
        return 0 if args.check else 1

    if not orphans:
        if not args.check:
            print("✅ venv 干净：没有发现多余的包。")
        return 0

    if args.check:
        # ☠️ 这句话是印给最终用户看的，**只许提真实存在的入口**。原文写的是
        # "更新字幕时加 -Prune 参数"，而 2026-09-20 合并桌面入口之后
        # 「更新字幕.bat」已经不在桌面上了（第 4 节第 45 条）——照那句话做的人
        # 会先去找一个不存在的图标。这里只留一条任何人都能照抄的命令。
        print(f"ℹ️ venv 里有 {len(orphans)} 个包已经不在依赖清单里了"
              f"（{', '.join(orphans[:3])} 等），留着不影响使用，只占磁盘。"
              f"想清掉就在程序目录里跑："
              f"venv\\Scripts\\python scripts\\prune_venv.py --yes")
        return 0

    print(f"发现 {len(orphans)} 个不在依赖闭包里的包：")
    for name in orphans:
        print(f"   - {name}")

    if not args.yes:
        print("\n只是列出来看看。真要删：加 --yes 重跑。")
        return 0

    proc = _pip("uninstall", "-y", *orphans)
    sys.stdout.write(proc.stdout)
    if proc.returncode != 0:
        sys.stderr.write(proc.stderr)
        print("❌ 卸载失败。venv 还是可用的，重跑 install.ps1 可以补回任何缺失的包。")
        return 1
    print(f"\n✅ 已清掉 {len(orphans)} 个包。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
