"""脚本靠 `python -c "…print(config.X)"` 的 stdout 读模型名——这条跨进程约定。

☠️ 事故形态：config_local.py 写坏时，config.py 的"加载失败"警告以前打在
**stdout**，start_subtitles.ps1 取 stdout 第 1 行当翻译模型名，于是去
`ollama pull "⚠️ config_local.py 加载失败…"`，再报"网络/模型名过期"——真正的
原因（配置文件写坏）被整个盖住。config_local.py 里随手一个 print 也是同一回事。

两层防线，各有一条用例：
1. config.py 的警告走 stderr；
2. 脚本只认 `RSCFG:` 前缀行，stdout 里混进什么都不怕。
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = REPO_ROOT / "scripts" / "windows"
POWERSHELL = shutil.which("powershell") or shutil.which("powershell.exe")


def _mini_repo(tmp_path, config_local_text):
    """tmp_path 里搭最小仓库：config.py 只依赖 paths/version，拷这几个就够。"""
    pkg = tmp_path / "realtime_subtitle"
    pkg.mkdir()
    for name in ("__init__.py", "version.py", "paths.py", "config.py"):
        shutil.copyfile(REPO_ROOT / "realtime_subtitle" / name, pkg / name)
    (tmp_path / "config_local.py").write_text(config_local_text, encoding="utf-8")
    return tmp_path


def _read_model(root):
    return subprocess.run(
        [sys.executable, "-c",
         "from realtime_subtitle import config; print(config.OLLAMA_MODEL)"],
        cwd=root, capture_output=True, text=True, encoding="utf-8",
        env={"PYTHONPATH": str(root), "PYTHONIOENCODING": "utf-8",
             "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", "")},
        timeout=60)


def test_broken_config_local_warns_on_stderr_not_stdout(tmp_path):
    root = _mini_repo(tmp_path, "OLLAMA_MODEL = 'oops\n")  # 语法错误
    done = _read_model(root)
    assert done.returncode == 0, done.stderr
    out_lines = [ln for ln in done.stdout.splitlines() if ln.strip()]
    assert len(out_lines) == 1, f"stdout 只该有模型名，混进了：{done.stdout!r}"
    assert "config_local" not in done.stdout
    assert "config_local.py 加载失败" in done.stderr, "警告不能被吞掉，只是换到 stderr"


def test_working_config_local_still_applies(tmp_path):
    root = _mini_repo(tmp_path, "OLLAMA_MODEL = 'my-model:1b'\n")
    done = _read_model(root)
    assert done.stdout.strip() == "my-model:1b"
    assert done.stderr.strip() == ""


# ------------------------------------------------------------------
# 脚本侧：只认 RSCFG: 前缀
# ------------------------------------------------------------------

_CONFIG_READ = re.compile(r'-c\s+"from realtime_subtitle import config;[^"]*"')


def test_every_script_config_read_uses_marker():
    """任何 ps1 里 `python -c "from realtime_subtitle import config; …"` 读值，
    每个 print 都必须带 RSCFG: 前缀——新加一处读配置的地方忘了，这里报红。"""
    hits = 0
    for ps1 in SCRIPTS.glob("*.ps1"):
        text = ps1.read_text(encoding="utf-8-sig")
        for m in _CONFIG_READ.finditer(text):
            hits += 1
            prints = re.findall(r"print\(([^;]*)\)", m.group(0))
            assert prints, f"{ps1.name}: {m.group(0)}"
            for arg in prints:
                assert arg.startswith("'RSCFG:' +"), (
                    f"{ps1.name} 读配置没带 RSCFG: 前缀：{m.group(0)}")
    assert hits >= 4, "start/stop/install/uninstall 至少各有一处"


@pytest.mark.skipif(not POWERSHELL, reason="需要 Windows PowerShell")
def test_start_script_filter_ignores_noise_on_stdout():
    """把 start_subtitles.ps1 里**原样**那行过滤表达式拿出来，喂一段混着
    config_local 里 print 输出和旧式警告的 stdout，只能拿到两个模型名。"""
    text = (SCRIPTS / "start_subtitles.ps1").read_text(encoding="utf-8-sig")
    line = next(ln for ln in text.splitlines() if ln.startswith("$cfg = @("))
    script = (
        "$cfgOut = @('hello from config_local', "
        "'⚠️  config_local.py 加载失败，本机配置【未生效】', "
        "'RSCFG:qwen3.5:9b', '  RSCFG:large-v3-turbo  ')\n"
        f"{line}\n"
        "Write-Output ($cfg -join '|')\n")
    done = subprocess.run(
        [POWERSHELL, "-NoProfile", "-NonInteractive", "-Command",
         "[Console]::OutputEncoding=[Text.Encoding]::UTF8; " + script],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "qwen3.5:9b|large-v3-turbo"
