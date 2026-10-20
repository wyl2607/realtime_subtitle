import os
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MACOS = REPO_ROOT / "scripts" / "macos"
GIT = shutil.which("git")

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS 脚本只在 darwin 上测试")


def _write_exe(path, body):
    path.write_text(body, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _git(cwd, *args):
    done = subprocess.run(
        [GIT, "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert done.returncode == 0, done.stderr
    return done.stdout.strip()


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
    for name in ("install.sh", "start.sh", "stop.sh", "update.sh"):
        result = subprocess.run(["bash", "-n", str(MACOS / name)], text=True, capture_output=True, check=False)
        assert result.returncode == 0, result.stderr


def _fake_update_bin(tmp_path, fail_pip=False):
    bindir = tmp_path / "bin"
    bindir.mkdir(exist_ok=True)
    _write_exe(
        bindir / "uv",
        "#!/usr/bin/env bash\n"
        "echo \"uv $*\" >> \"$RS_TEST_LOG\"\n"
        "if [[ \"$1\" == \"venv\" ]]; then mkdir -p venv/bin; touch venv/bin/python; chmod +x venv/bin/python; fi\n"
        f"if [[ \"$1\" == \"pip\" && \"{int(fail_pip)}\" == \"1\" ]]; then exit 42; fi\n"
        "exit 0\n",
    )
    _write_exe(bindir / "swift", "#!/usr/bin/env bash\necho \"swift $*\" >> \"$RS_TEST_LOG\"\nexit 0\n")
    return bindir


def _commit(work, requirements, version, marker, msg):
    (work / "requirements.txt").write_text(requirements, encoding="utf-8")
    (work / "realtime_subtitle" / "version.py").write_text(f'__version__ = "{version}"\n', encoding="utf-8")
    if marker:
        (work / marker).parent.mkdir(parents=True, exist_ok=True)
        (work / marker).write_text(msg, encoding="utf-8")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", msg)
    _git(work, "push", "-q", "origin", "feat/macos-native")
    return _git(work, "rev-parse", "HEAD")


@pytest.fixture
def macos_update_repo(tmp_path):
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "feat/macos-native", str(origin))
    pub = tmp_path / "pub"
    _git(tmp_path, "clone", "-q", str(origin), str(pub))
    (pub / "scripts" / "macos").mkdir(parents=True)
    shutil.copyfile(MACOS / "update.sh", pub / "scripts" / "macos" / "update.sh")
    (pub / "realtime_subtitle").mkdir()
    (pub / "realtime_subtitle" / "__init__.py").write_text("", encoding="utf-8")
    (pub / ".gitignore").write_text("venv/\n", encoding="utf-8")
    (pub / "notes.txt").write_text("base\n", encoding="utf-8")
    base = _commit(pub, "pip\n", "1.0.0", "v1.txt", "v1")

    user = tmp_path / "user"
    _git(tmp_path, "clone", "-q", str(origin), str(user))
    log = tmp_path / "commands.log"
    env = os.environ.copy()
    env["PATH"] = f"{_fake_update_bin(tmp_path)}:{env['PATH']}"
    env["RS_TEST_LOG"] = str(log)
    return {"origin": origin, "pub": pub, "user": user, "base": base, "env": env, "log": log, "tmp": tmp_path}


def _run_update(s, env=None):
    done = subprocess.run(
        ["/bin/bash", str(s["user"] / "scripts" / "macos" / "update.sh")],
        cwd=s["user"],
        env=env or s["env"],
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    return done.returncode, done.stdout + done.stderr


def _head(path):
    return _git(path, "rev-parse", "HEAD")


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_reports_already_latest_after_fingerprint_is_stored(macos_update_repo):
    s = macos_update_repo
    code, out = _run_update(s)
    assert code == 0, out
    s["log"].write_text("", encoding="utf-8")

    code, out = _run_update(s)
    assert code == 0, out
    assert "已经是最新版本" in out
    assert s["log"].read_text(encoding="utf-8") == ""


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_fast_forwards_current_upstream_branch(macos_update_repo):
    s = macos_update_repo
    assert _run_update(s)[0] == 0
    new = _commit(s["pub"], "pip\n", "1.1.0", "v2.txt", "v2")

    code, out = _run_update(s)
    assert code == 0, out
    assert _head(s["user"]) == new
    assert (s["user"] / "v2.txt").is_file()
    assert "版本：v1.0.0 → v1.1.0" in out


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_refuses_dirty_worktree_with_stash_hint(macos_update_repo):
    s = macos_update_repo
    assert _run_update(s)[0] == 0
    (s["user"] / "notes.txt").write_text("local edit\n", encoding="utf-8")

    code, out = _run_update(s)
    assert code == 1
    assert "git stash" in out
    assert "config_local.py" in out
    assert (s["user"] / "notes.txt").read_text(encoding="utf-8") == "local edit\n"


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_syncs_dependencies_when_requirements_change(macos_update_repo):
    s = macos_update_repo
    assert _run_update(s)[0] == 0
    s["log"].write_text("", encoding="utf-8")
    _commit(s["pub"], "pip\n# changed\n", "1.0.0", "req.txt", "requirements changed")

    code, out = _run_update(s)
    assert code == 0, out
    log = s["log"].read_text(encoding="utf-8")
    assert "uv pip install -r requirements.txt" in log


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_reuses_existing_venv_instead_of_recreating(macos_update_repo):
    # 真实 uv 遇到已存在的 venv 会报错退出：update 必须跳过 uv venv，只补装依赖
    s = macos_update_repo
    assert _run_update(s)[0] == 0
    py = s["user"] / "venv" / "bin" / "python"
    py.parent.mkdir(parents=True, exist_ok=True)
    py.write_text("#!/bin/sh\n", encoding="utf-8")
    py.chmod(0o755)
    s["log"].write_text("", encoding="utf-8")
    _commit(s["pub"], "pip\n# changed again\n", "1.0.0", "req2.txt", "requirements changed")

    code, out = _run_update(s)
    assert code == 0, out
    log = s["log"].read_text(encoding="utf-8")
    assert "uv venv" not in log
    assert "uv pip install -r requirements.txt" in log


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_rebuilds_rstranslate_when_macos_native_changes(macos_update_repo):
    s = macos_update_repo
    assert _run_update(s)[0] == 0
    s["log"].write_text("", encoding="utf-8")
    _commit(s["pub"], "pip\n", "1.0.0", "macos-native/Package.swift", "native helper changed")

    code, out = _run_update(s)
    assert code == 0, out
    assert "swift build -c release --product rstranslate" in s["log"].read_text(encoding="utf-8")


@pytest.mark.skipif(not GIT, reason="需要 git")
def test_update_rolls_back_code_when_dependency_sync_fails(macos_update_repo):
    s = macos_update_repo
    assert _run_update(s)[0] == 0
    new = _commit(s["pub"], "rs-test-package-that-does-not-exist==0\n", "1.2.0", "v2.txt", "bad deps")
    env = os.environ.copy()
    env.update(s["env"])
    env["PATH"] = f"{_fake_update_bin(s['tmp'], fail_pip=True)}:{os.environ['PATH']}"

    code, out = _run_update(s, env=env)
    assert code == 1, out
    assert _head(s["user"]) == s["base"]
    assert _head(s["user"]) != new
    assert not (s["user"] / "v2.txt").exists()
    assert "代码已退回" in out


def _start_repo(tmp_path, update_body):
    repo = tmp_path / "start-repo"
    (repo / "scripts" / "macos").mkdir(parents=True)
    shutil.copyfile(MACOS / "start.sh", repo / "scripts" / "macos" / "start.sh")
    shutil.copyfile(MACOS / "stop.sh", repo / "scripts" / "macos" / "stop.sh")
    _write_exe(repo / "scripts" / "macos" / "update.sh", update_body)
    (repo / "venv" / "bin").mkdir(parents=True)
    _write_exe(repo / "venv" / "bin" / "python", "#!/usr/bin/env bash\necho \"python $*\" >> \"$RS_TEST_LOG\"\n")
    bindir = tmp_path / "start-bin"
    bindir.mkdir()
    _write_exe(bindir / "ollama", "#!/usr/bin/env bash\necho \"ollama $*\" >> \"$RS_TEST_LOG\"\n")
    _write_exe(bindir / "curl", "#!/usr/bin/env bash\nexit 0\n")
    return repo, bindir


def _read_log_wait(path, needle, timeout=5):
    deadline = time.time() + timeout
    last = ""
    while time.time() < deadline:
        if path.exists():
            last = path.read_text(encoding="utf-8")
            if needle in last:
                return last
        time.sleep(0.05)
    return last


def test_start_continues_when_update_fails(tmp_path):
    repo, bindir = _start_repo(
        tmp_path,
        "#!/usr/bin/env bash\necho update >> \"$RS_TEST_LOG\"\nexit 7\n",
    )
    log = tmp_path / "start.log"
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["RS_TEST_LOG"] = str(log)
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "macos" / "start.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = _read_log_wait(log, "python -u main.py")
    assert "update" in text
    assert "python -u main.py" in text
    assert "改用当前已装好的版本继续启动" in result.stdout


def test_start_no_update_skips_update_subprocess(tmp_path):
    repo, bindir = _start_repo(
        tmp_path,
        "#!/usr/bin/env bash\necho update >> \"$RS_TEST_LOG\"\nexit 99\n",
    )
    log = tmp_path / "start.log"
    env = os.environ.copy()
    env["PATH"] = f"{bindir}:{env['PATH']}"
    env["RS_TEST_LOG"] = str(log)
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "macos" / "start.sh"), "--no-update"],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    text = _read_log_wait(log, "python -u main.py")
    assert "update" not in text
    assert "python -u main.py" in text
    assert "已跳过启动前更新" in result.stdout


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


def test_start_polls_ollama_quickly_instead_of_fixed_two_second_sleeps(tmp_path):
    """Ollama 冷启动要好几秒；固定 sleep 2 会在每次未就绪后白等 2 秒。

    用第 4 次 curl 才成功模拟"服务需要一点时间才起来"：旧逻辑 3 次
    sleep 2 = 6 秒，200ms 轮询约 0.6 秒。
    """
    repo = tmp_path / "repo"
    (repo / "scripts" / "macos").mkdir(parents=True)
    shutil.copy(MACOS / "start.sh", repo / "scripts" / "macos" / "start.sh")
    (repo / "venv" / "bin").mkdir(parents=True)
    _write_exe(repo / "venv" / "bin" / "python", "#!/usr/bin/env bash\nexit 0\n")
    (repo / "main.py").write_text("", encoding="utf-8")

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    counter = tmp_path / "curl.count"
    _write_exe(
        fake_bin / "curl",
        "#!/usr/bin/env bash\n"
        f"n=$(cat {counter} 2>/dev/null || echo 0)\n"
        "n=$((n + 1))\n"
        f"echo $n > {counter}\n"
        "[ \"$n\" -ge 4 ]\n",
    )
    _write_exe(fake_bin / "ollama", "#!/usr/bin/env bash\nexit 0\n")

    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}:{env['PATH']}"
    env["RS_NO_UPDATE"] = "1"
    started = time.monotonic()
    result = subprocess.run(
        ["/bin/bash", str(repo / "scripts" / "macos" / "start.sh")],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    elapsed = time.monotonic() - started
    assert result.returncode == 0, result.stdout + result.stderr
    assert "已启动" in result.stdout
    assert elapsed < 2.0, f"启动脚本等了 {elapsed:.1f}s，轮询粒度太粗（{result.stdout}）"
