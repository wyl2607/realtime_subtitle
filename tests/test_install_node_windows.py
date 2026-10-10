"""install_node_windows.sh 的离线检查：语法、参数校验、dry-run 计划（不联网、不碰目标机）。

真机安装流程靠在 Windows 节点上实跑验收（见 docs/node-acceptance.md），这里只锁住
不需要目标机就能判定的部分。
"""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "node" / "install_node_windows.sh"
PS1 = ROOT / "scripts" / "node" / "windows" / "node_setup.ps1"


def run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_bash_syntax():
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_dry_run_lists_all_steps_without_token():
    r = run("winbox", "--dry-run")
    assert r.returncode == 0, r.stderr
    for step in range(1, 10):
        assert f"{step}. " in r.stdout
    assert "<redacted>" in r.stdout
    assert "100.64.0.0/10" in r.stdout


def test_rejects_option_like_host():
    r = run("-oProxyCommand=touch /tmp/pwned", "--dry-run")
    assert r.returncode != 0


def test_requires_host():
    assert run().returncode != 0


def test_ps1_is_ascii_only():
    # Windows PowerShell 5.1 把无 BOM 文件按 ANSI 读，中文会乱码（CLAUDE.md 第 4 节第 5 条）
    PS1.read_bytes().decode("ascii")


def test_ps1_never_kills_by_image_name():
    # 目标机上还有用户自己的 python / ollama 进程，只能按命令行精确匹配
    text = PS1.read_text(encoding="ascii")
    assert "taskkill" not in text.lower()
    assert "Stop-Process -Name" not in text
