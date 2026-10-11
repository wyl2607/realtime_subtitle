"""面板上的云端 key / 节点开关。不连网络，key 只用假字符串。"""
import json
import os
import stat
import sys

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import torch  # noqa: E402,F401  先于 PyQt6
from PyQt6.QtWidgets import QApplication, QLineEdit  # noqa: E402

import realtime_subtitle.ui.settings_window as sw  # noqa: E402
from realtime_subtitle.ui.settings_window import SettingsWindow  # noqa: E402

_APP = None
FAKE_KEY = "fake-groq-key-for-test"


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    monkeypatch.setattr(sw, "config_dir", lambda: tmp_path)
    return tmp_path


def _window():
    _app()
    return SettingsWindow()


def test_node_switch_disabled_without_nodes_json(cfg):
    win = _window()
    assert not win.node_cb.isEnabled()
    assert not win.node_cb.isChecked()
    assert "nodes.json" in win.node_reason.text()
    assert "本机" in win.route_label.text()


def test_node_switch_defaults_off_and_readback_keeps_it(cfg):
    (cfg / "nodes.json").write_text(json.dumps([
        {"id": "desk", "node_id": "n" * 16, "url": "ws://100.64.0.8:8791", "token_file": "x"},
    ]), encoding="utf-8")
    win = _window()
    assert win.node_cb.isEnabled()
    assert win.node_reason.text() == ""
    assert not win.node_cb.isChecked()
    modes = []
    win.on_mode_change = modes.append
    seen = []
    win.on_remote_change = lambda: seen.append("node")
    win.node_cb.setChecked(True)
    win.refresh_from_config()
    assert win.node_cb.isChecked()
    assert sw.read_node_enabled() is True
    assert modes == []
    assert seen
    (cfg / "nodes.json").unlink()
    win.refresh_from_config()
    assert not win.node_cb.isEnabled()
    assert not win.node_cb.isChecked()
    # 文件还没到，不能把已经写下的偏好改回关。
    assert sw.read_node_enabled() is True


def test_cloud_key_is_write_only_and_defaults_on(cfg, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fake-env-key-not-a-real-key")
    win = _window()
    assert win.groq_key_edit.echoMode() == QLineEdit.EchoMode.Password
    assert win.groq_key_edit.text() == ""
    assert not win.cloud_cb.isChecked()
    assert "粘贴" in win.groq_key_edit.placeholderText()
    assert "fake-env-key-not-a-real-key" not in win.groq_key_edit.text()

    modes = []
    win.on_mode_change = modes.append
    win.groq_key_edit.setText(FAKE_KEY)
    win._on_save_groq_key()
    key_path = cfg / "groq.key"
    assert key_path.read_text(encoding="utf-8").strip() == FAKE_KEY
    if sys.platform != "win32":  # Windows 上权限靠 icacls，没有 POSIX 模式位
        assert stat.S_IMODE(key_path.stat().st_mode) == 0o600
    assert list(cfg.glob(".*.tmp")) == []
    assert win.groq_key_edit.text() == ""
    assert "已保存" in win.groq_key_edit.placeholderText()
    assert win.cloud_cb.isChecked()
    assert "云端·Groq" in win.route_label.text()
    assert modes == []

    win.groq_key_edit.setText("")
    win._on_save_groq_key()
    assert key_path.read_text(encoding="utf-8").strip() == FAKE_KEY

    win._on_clear_groq_key()
    assert not key_path.exists()
    assert not win.cloud_cb.isChecked()
    assert "本机" in win.route_label.text()


def test_posix_key_write_does_not_call_icacls(cfg, monkeypatch):
    if sys.platform == "win32":
        pytest.skip("这一支只锁 POSIX 的 0600；Windows 的 icacls 留给真机")

    def boom(*args, **kwargs):
        raise AssertionError(args)

    monkeypatch.setattr(sw.subprocess, "run", boom)
    sw.write_groq_key(FAKE_KEY)
    assert stat.S_IMODE((cfg / "groq.key").stat().st_mode) == 0o600


def test_route_label_uses_node_when_cloud_is_off(cfg):
    (cfg / "nodes.json").write_text(json.dumps([
        {"id": "desk", "url": "ws://100.64.0.8:8791"},
    ]), encoding="utf-8")
    sw.write_groq_key(FAKE_KEY)
    sw.write_cloud_enabled(False)
    sw.write_node_enabled(True)
    win = _window()
    assert not win.cloud_cb.isChecked()
    assert win.node_cb.isChecked()
    assert "混合·desk" in win.route_label.text()
    win._reset_defaults()
    assert (cfg / "groq.key").read_text(encoding="utf-8").strip() == FAKE_KEY


def test_cloud_toggle_does_not_mark_a_scene_mode(cfg):
    sw.write_groq_key(FAKE_KEY)
    win = _window()
    modes = []
    win.on_mode_change = modes.append
    assert win.cloud_cb.isChecked()
    win.cloud_cb.setChecked(False)
    win.refresh_from_config()
    assert not win.cloud_cb.isChecked()
    assert modes == []
    assert sw.read_cloud_enabled() is False


def test_windows_icacls_locks_temp_before_replace(cfg, monkeypatch):
    """C1：NTFS 同卷 rename 会带走 DACL，icacls 必须打在临时文件上，且发生在替换之前。"""
    import subprocess

    monkeypatch.setattr(sw.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "tester")
    events = []
    real_replace = os.replace

    def spy_replace(src, dst):
        events.append(("replace", os.path.basename(src)))
        return real_replace(src, dst)

    def fake_run(args, check, shell):
        assert shell is False
        assert args[0] == "icacls"
        target = args[1]
        name = os.path.basename(target)
        events.append(("icacls", name))
        assert name.startswith(".") and name.endswith(".tmp")
        dest_name = name[1:].rsplit(".", 2)[0]
        assert os.path.exists(target)
        assert not (cfg / dest_name).exists()
        return subprocess.CompletedProcess(args, 0)

    monkeypatch.setattr(os, "replace", spy_replace)
    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    sw.write_groq_key(FAKE_KEY)
    sw.write_cloud_enabled(False)
    sw.write_node_enabled(True)
    assert [kind for kind, _name in events] == [
        "icacls", "replace", "icacls", "replace", "icacls", "replace",
    ]
    assert (cfg / "groq.key").read_text(encoding="utf-8").strip() == FAKE_KEY
    assert (cfg / "cloud.json").is_file()
    assert (cfg / "node.json").is_file()
    assert list(cfg.glob(".*.tmp")) == []


def test_windows_icacls_failure_removes_temp_and_skips_target(cfg, monkeypatch):
    import subprocess

    monkeypatch.setattr(sw.sys, "platform", "win32")
    monkeypatch.setenv("USERNAME", "tester")

    def fake_run(args, check, shell):
        raise subprocess.CalledProcessError(1, args)

    monkeypatch.setattr(sw.subprocess, "run", fake_run)
    with pytest.raises(OSError, match="icacls failed"):
        sw.write_groq_key(FAKE_KEY)
    assert not (cfg / "groq.key").exists()
    assert list(cfg.glob(".*.tmp")) == []

    monkeypatch.delenv("USERNAME", raising=False)
    with pytest.raises(OSError, match="icacls failed"):
        sw.write_cloud_enabled(True)
    assert not (cfg / "cloud.json").exists()
    assert list(cfg.glob(".*.tmp")) == []


def test_settings_import_does_not_pull_remote_modules():
    """BUG-06：import 设置面板不得把 cloud_client / requests 拉进进程。"""
    import subprocess

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    script = (
        "import sys\n"
        "import realtime_subtitle.ui.settings_window\n"
        "bad = [name for name in ("
        "'realtime_subtitle.remote.cloud_client', 'requests'"
        ") if name in sys.modules]\n"
        "print(','.join(bad))\n"
        "raise SystemExit(1 if bad else 0)\n"
    )
    env = dict(os.environ)
    env["QT_QPA_PLATFORM"] = "offscreen"
    env["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
