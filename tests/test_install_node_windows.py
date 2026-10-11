"""install_node_windows.sh 的离线检查：语法、参数校验、dry-run 计划（不联网、不碰目标机）。

真机安装流程靠在 Windows 节点上实跑验收（见 docs/node-acceptance.md），这里只锁住
不需要目标机就能判定的部分。
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "node" / "install_node_windows.sh"
PS1 = ROOT / "scripts" / "node" / "windows" / "node_setup.ps1"


def run(*args):
    return subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, timeout=30)


def test_bash_syntax():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    assert subprocess.run(["bash", "-n", str(SCRIPT)]).returncode == 0


def test_dry_run_lists_all_steps_without_token():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    r = run("winbox", "--dry-run")
    assert r.returncode == 0, r.stderr
    for step in range(1, 10):
        assert f"{step}. " in r.stdout
    assert "<redacted>" in r.stdout
    assert "100.64.0.0/10" in r.stdout


def test_rejects_option_like_host():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    r = run("-oProxyCommand=touch /tmp/pwned", "--dry-run")
    assert r.returncode != 0


def test_requires_host():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    assert run().returncode != 0


def test_ps1_is_ascii_only():
    # Windows PowerShell 5.1 把无 BOM 文件按 ANSI 读，中文会乱码（CLAUDE.md 第 4 节第 5 条）
    PS1.read_bytes().decode("ascii")


def test_ps1_never_kills_by_image_name():
    # 目标机上还有用户自己的 python / ollama 进程，只能按命令行精确匹配
    text = PS1.read_text(encoding="ascii")
    assert "taskkill" not in text.lower()
    assert "Stop-Process -Name" not in text


def test_dry_run_client_mentions_push_without_renumbering():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    r = run("winbox", "--dry-run", "--client", "desktop")
    assert r.returncode == 0, r.stderr
    for step in range(1, 10):
        assert f"{step}. " in r.stdout
    assert "PushClient" in r.stdout
    assert "<redacted>" in r.stdout
    assert "scp" in r.stdout


def test_windows_client_flag_requires_alias():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    assert run("winbox", "--client").returncode != 0


def test_windows_help_mentions_client():
    if sys.platform == "win32":
        pytest.skip("需要 bash")
    r = run("--help")
    assert r.returncode == 0
    assert "--client" in r.stdout


def test_ps1_pushclient_locks_down_and_merges_by_id():
    text = PS1.read_text(encoding="ascii")
    assert "'PushClient'" in text
    assert "Lock-Down" in text
    assert "ReadLine" in text
    assert "ConvertTo-Json" in text
    assert "$e.id -eq $NodeAlias" in text
    assert "taskkill" not in text.lower()
    assert "Stop-Process -Name" not in text


def test_pushclient_idempotent_when_icacls_exists(tmp_path):
    import shutil
    if shutil.which("pwsh") is None or shutil.which("icacls") is None:
        pytest.skip("需要 pwsh 和 icacls，留给 Windows 真机")
    appdata = tmp_path / "AppData"
    appdata.mkdir()
    env = dict(**{"APPDATA": str(appdata), "USERNAME": "tester"})
    token = "ab" * 32
    node_id = "c" * 16
    cmd = [
        "pwsh", "-NoProfile", "-File", str(PS1),
        "-Action", "PushClient", "-NodeAlias", "desk", "-NodeId", node_id,
        "-UrlHost", "desk.tail.ts.net", "-Port", "8791",
    ]
    first = subprocess.run(cmd, input=token + "\n", capture_output=True, text=True, env={**os.environ, **env})
    assert first.returncode == 0, first.stderr
    second = subprocess.run(cmd, input=token + "\n", capture_output=True, text=True, env={**os.environ, **env})
    assert second.returncode == 0, second.stderr
    import json
    entries = json.loads((appdata / "realtime_subtitle" / "nodes.json").read_text(encoding="utf-8"))
    assert [entry["id"] for entry in entries] == ["desk"]

