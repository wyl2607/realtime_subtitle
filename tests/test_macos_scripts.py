import os
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS = REPO_ROOT / "scripts" / "macos"

pytestmark = pytest.mark.skipif(os.uname().sysname != "Darwin", reason="macOS 脚本只在 darwin 上测试")


def _write_exe(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _fake_bin(tmp_path, mem_gb, ollama_list=""):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    _write_exe(
        bindir / "sysctl",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"-n\" && \"$2\" == \"hw.memsize\" ]]; then\n"
        f"  echo $(({mem_gb} * 1024 * 1024 * 1024))\n"
        "else\n"
        "  exit 1\n"
        "fi\n",
    )
    _write_exe(bindir / "brew", "#!/usr/bin/env bash\nexit 0\n")
    _write_exe(bindir / "uv", "#!/usr/bin/env bash\necho \"uv $*\"\n")
    _write_exe(
        bindir / "ollama",
        "#!/usr/bin/env bash\n"
        "if [[ \"$1\" == \"list\" ]]; then\n"
        f"  cat <<'EOF'\n{ollama_list}\nEOF\n"
        "  exit 0\n"
        "fi\n"
        "echo \"ollama $*\"\n",
    )
    return bindir


def _run_install(tmp_path, mem_gb, *args, ollama_list=""):
    env = os.environ.copy()
    env["PATH"] = f"{_fake_bin(tmp_path, mem_gb, ollama_list)}:{env['PATH']}"
    return subprocess.run(
        ["bash", str(MACOS / "install.sh"), "--dry-run", *args],
        cwd=REPO_ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )


def test_macos_scripts_parse_with_bash_n():
    for name in ("install.sh", "start.sh", "stop.sh"):
        result = subprocess.run(["bash", "-n", str(MACOS / name)], text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("mem_gb", "whisper", "model", "tier"),
    [
        (8, "mlx-community/whisper-large-v3-turbo-q4", "qwen3.5:2b", "<12GB 轻量档"),
        (16, "mlx-community/whisper-large-v3-turbo", "qwen3.5:4b", "12-31GB 均衡档"),
        (32, "mlx-community/whisper-large-v3-turbo", "qwen3.5:9b", "≥32GB 高配档"),
    ],
)
def test_install_dry_run_selects_memory_tier(tmp_path, mem_gb, whisper, model, tier):
    result = _run_install(tmp_path, mem_gb, "--skip-models")
    assert result.returncode == 0, result.stdout + result.stderr
    assert tier in result.stdout
    assert f'WHISPER_MLX_REPO = "{whisper}"' in result.stdout
    assert f'OLLAMA_MODEL = "{model}"' in result.stdout
    assert "[dry-run] uv venv" in result.stdout
    assert "[dry-run] 将写入 config_local.py" in result.stdout or "[dry-run] 若加 --force-config，将写入" in result.stdout


def test_install_lean_forces_whisper_q4_but_keeps_translation_tier(tmp_path):
    result = _run_install(tmp_path, 16, "--lean", "--skip-models")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "12-31GB 均衡档 + --lean" in result.stdout
    assert 'WHISPER_MLX_REPO = "mlx-community/whisper-large-v3-turbo-q4"' in result.stdout
    assert 'OLLAMA_MODEL = "qwen3.5:4b"' in result.stdout


def test_install_reports_reusable_same_family_models(tmp_path):
    result = _run_install(tmp_path, 16, "--skip-models", ollama_list="qwen3.5:2b latest abc\n")
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已有同系列小/同档模型" in result.stdout
    assert "qwen3.5:2b" in result.stdout


def test_scripts_do_not_use_localhost_or_ollama_stop():
    joined = "\n".join((MACOS / name).read_text(encoding="utf-8") for name in ("install.sh", "start.sh", "stop.sh"))
    assert "localhost:11434" not in joined
    assert "ollama stop" not in joined


def test_install_dry_run_does_not_write_config_local(tmp_path):
    cfg = REPO_ROOT / "config_local.py"
    before = cfg.read_bytes() if cfg.exists() else None
    result = _run_install(tmp_path, 8, "--force-config", "--skip-models")
    assert result.returncode == 0, result.stdout + result.stderr
    after = cfg.read_bytes() if cfg.exists() else None
    assert after == before


def test_stop_waits_for_exit_before_unloading_models():
    """避坑第 13 条：先等进程退出再卸模型，否则在飞请求会把模型拉回留驻 2h。"""
    text = (MACOS / "stop.sh").read_text(encoding="utf-8")
    assert text.index("kill -0") < text.index("\"keep_alive\": 0")


def test_stop_survives_bash32_with_no_pid_and_no_ollama(tmp_path):
    """macOS 自带 bash 3.2 + set -u：空数组/空变量展开不能炸。"""
    import shutil
    import subprocess

    repo = tmp_path / "repo"
    (repo / "scripts" / "macos").mkdir(parents=True)
    shutil.copy(MACOS / "stop.sh", repo / "scripts" / "macos" / "stop.sh")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "curl").write_text("#!/bin/sh\nexit 7\n")
    (fake_bin / "curl").chmod(0o755)
    env = {"PATH": f"{fake_bin}:/usr/bin:/bin"}
    r = subprocess.run(["/bin/bash", str(repo / "scripts" / "macos" / "stop.sh")],
                       capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    assert (repo / ".stop").exists()
