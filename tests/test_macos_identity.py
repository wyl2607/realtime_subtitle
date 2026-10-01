"""macOS 上「实时字幕开着吗」和「是不是本仓库 venv 的解释器」两处判断。"""
import fcntl
import os
import sys

import pytest

pytestmark = pytest.mark.skipif(sys.platform != "darwin", reason="macOS 专用")


def test_realtime_lock_held_reflects_flock(tmp_path, monkeypatch):
    from realtime_subtitle import offline, paths
    lock = tmp_path / ".subtitle.lock"
    monkeypatch.setattr(paths, "repo_path", lambda name: str(tmp_path / name))
    assert offline._realtime_lock_held() is False  # 文件都没有
    lock.touch()
    assert offline._realtime_lock_held() is False  # 有文件但没人锁
    with open(lock, "a") as holder:
        fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert offline._realtime_lock_held() is True
    assert offline._realtime_lock_held() is False  # 持有者退出后


def test_is_our_interpreter_venv_bin(tmp_path):
    from realtime_subtitle.instance_identity import is_our_interpreter
    bin_dir = tmp_path / "venv" / "bin"
    bin_dir.mkdir(parents=True)
    real = tmp_path / "base-python"
    real.touch()
    for name in ("python", "python3.12"):
        os.symlink(real, bin_dir / name)
        assert is_our_interpreter(str(bin_dir / name), tmp_path)
    backup = tmp_path / "venv_backup" / "bin"
    backup.mkdir(parents=True)
    os.symlink(real, backup / "python")
    assert not is_our_interpreter(str(backup / "python"), tmp_path)
    assert not is_our_interpreter(str(real), tmp_path)
