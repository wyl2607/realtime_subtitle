"""F04：停止对象必须是当前实时字幕实例。测试不实际杀进程。"""
from pathlib import Path

from realtime_subtitle.instance_identity import (
    confirm_realtime_process,
    is_our_interpreter,
    is_realtime_command,
    parse_pid_file,
    should_force_stop,
)


def test_venv_backup_is_not_our_interpreter(tmp_path):
    repo = tmp_path / "realtime_subtitle"
    ours = repo / "venv" / "Scripts" / "python.exe"
    backup = repo / "venv_backup" / "Scripts" / "python.exe"
    ours.parent.mkdir(parents=True)
    backup.parent.mkdir(parents=True)
    ours.write_text("")
    backup.write_text("")
    assert is_our_interpreter(str(ours), repo) is True
    assert is_our_interpreter(str(backup), repo) is False


def test_prefix_without_separator_would_have_matched_backup(tmp_path):
    """回归：裸 StartsWith(venv) 会命中 venv_backup。"""
    repo = tmp_path / "proj"
    backup = repo / "venv_backup" / "Scripts" / "python.exe"
    backup.parent.mkdir(parents=True)
    backup.write_text("")
    assert str(backup).startswith(str(repo / "venv"))
    assert is_our_interpreter(str(backup), repo) is False


def test_offline_task_in_same_venv_is_not_realtime():
    assert is_realtime_command(r'C:\x\venv\Scripts\python.exe -u main.py') is True
    assert is_realtime_command(r'C:\x\venv\Scripts\python.exe download_subtitle.py https://x') is False
    assert is_realtime_command(r'C:\x\venv\Scripts\python.exe -u download_subtitle.py') is False
    assert is_realtime_command("") is False


def test_stale_pid_reused_by_other_process_is_refused(tmp_path):
    repo = tmp_path / "proj"
    ours = repo / "venv" / "Scripts" / "python.exe"
    ours.parent.mkdir(parents=True)
    ours.write_text("")
    other = tmp_path / "other" / "python.exe"
    other.parent.mkdir()
    other.write_text("")
    recorded = {
        "pid": 4242,
        "start_time": "2026-01-01T00:00:00",
        "exe": str(ours),
        "command_line": f"{ours} -u main.py",
        "repo_root": str(repo),
    }
    live_reused = {
        "pid": 4242,
        "start_time": "2026-09-06T12:00:00",
        "exe": str(other),
        "command_line": f"{other} notepad.py",
    }
    assert should_force_stop(recorded, live_reused) is False


def test_pid_reused_same_pid_different_start_time(tmp_path):
    repo = tmp_path / "proj"
    ours = repo / "venv" / "Scripts" / "python.exe"
    ours.parent.mkdir(parents=True)
    ours.write_text("")
    recorded = {
        "pid": 7,
        "start_time": "2026-01-01T00:00:00",
        "exe": str(ours),
        "command_line": f"{ours} -u main.py",
        "repo_root": str(repo),
    }
    live = {
        "pid": 7,
        "start_time": "2026-09-06T08:00:00",
        "exe": str(ours),
        "command_line": f"{ours} -u main.py",
    }
    assert should_force_stop(recorded, live) is False


def test_true_launcher_and_child_are_stop_targets(tmp_path):
    repo = tmp_path / "proj"
    ours = repo / "venv" / "Scripts" / "python.exe"
    ours.parent.mkdir(parents=True)
    ours.write_text("")
    recorded = {
        "pid": 11,
        "start_time": "2026-09-06T08:00:00",
        "exe": str(ours),
        "command_line": f"{ours} -u main.py",
        "repo_root": str(repo),
    }
    live = dict(recorded)
    live.pop("repo_root")
    assert should_force_stop(recorded, live) is True
    # 子进程同样跑 main.py、解释器仍是本 venv
    child = dict(live)
    child["pid"] = 12
    recorded_child = dict(recorded)
    recorded_child["pid"] = 12
    assert should_force_stop(recorded_child, child) is True


def test_same_title_in_another_repo_is_not_ours(tmp_path):
    repo = tmp_path / "a"
    other = tmp_path / "b"
    ours = repo / "venv" / "Scripts" / "python.exe"
    theirs = other / "venv" / "Scripts" / "python.exe"
    ours.parent.mkdir(parents=True)
    theirs.parent.mkdir(parents=True)
    ours.write_text("")
    theirs.write_text("")
    assert confirm_realtime_process(
        exe=str(theirs),
        command_line=f"{theirs} -u main.py",
        repo_root=repo,
        window_title="德语直播实时字幕",
    ) is False
    assert confirm_realtime_process(
        exe=str(ours),
        command_line=f"{ours} -u main.py",
        repo_root=repo,
        window_title="德语直播实时字幕",
    ) is True


def test_window_title_alone_is_not_enough(tmp_path):
    repo = tmp_path / "proj"
    (repo / "venv" / "Scripts").mkdir(parents=True)
    (repo / "venv" / "Scripts" / "python.exe").write_text("")
    assert confirm_realtime_process(
        exe=r"C:\Python313\python.exe",
        command_line=r"C:\Python313\python.exe -u something.py",
        repo_root=repo,
        window_title="实时字幕",
    ) is False


def test_old_pid_file_without_identity_cannot_force_kill():
    parsed = parse_pid_file("4242\n")
    assert parsed["pid"] == 4242
    assert parsed.get("start_time") is None
    # 只有 PID、无法确认创建时间/入口时不可强杀
    assert should_force_stop(parsed, {
        "pid": 4242,
        "exe": r"C:\Windows\System32\notepad.exe",
        "command_line": "notepad.exe",
        "start_time": "now",
    }) is False


def test_missing_live_start_time_cannot_force_kill(tmp_path):
    repo = tmp_path / "proj"
    ours = repo / "venv" / "Scripts" / "python.exe"
    ours.parent.mkdir(parents=True)
    ours.write_text("")
    recorded = {
        "pid": 4242,
        "start_time": "2026-01-01T00:00:00",
        "exe": str(ours),
        "command_line": f"{ours} -u main.py",
        "repo_root": str(repo),
    }
    live = {
        "pid": 4242,
        "exe": str(ours),
        "command_line": f"{ours} -u main.py",
    }
    assert should_force_stop(recorded, live) is False


def test_stop_script_does_not_use_bare_venv_prefix():
    from pathlib import Path

    text = (Path(__file__).resolve().parents[1]
            / "scripts" / "windows" / "stop_subtitles.ps1").read_text(encoding="utf-8-sig")
    helper = (Path(__file__).resolve().parents[1]
              / "scripts" / "windows" / "_identity.ps1").read_text(encoding="utf-8-sig")
    # 裸 StartsWith(venv) 会匹配 venv_backup；必须精确 python.exe + main.py
    assert "venv_backup" in text
    assert "Test-RealtimeInstance" in text
    assert "main.py" in helper
    assert "GetFullPath" in helper
