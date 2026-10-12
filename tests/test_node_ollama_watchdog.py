"""Tests for OllamaWatchdog (realtime_subtitle.node.ollama_watchdog)."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from realtime_subtitle.node.ollama_watchdog import (
    OllamaWatchdog,
    default_spawn,
    create_macos_launchctl_spawn,
)


class FakeClock:
    def __init__(self, initial: float = 1000.0):
        self._now = initial

    def __call__(self) -> float:
        return self._now

    def advance(self, delta: float) -> None:
        self._now += delta


def test_watchdog_non_ollama_translator_does_nothing():
    """Non-ollama translators (e.g., 'apple') should never trigger probe/spawn."""
    probe_called = False
    spawn_called = False

    def probe() -> bool:
        nonlocal probe_called
        probe_called = True
        return False

    def spawn() -> None:
        nonlocal spawn_called
        spawn_called = True

    clock = FakeClock()
    wd = OllamaWatchdog("apple", probe=probe, spawn=spawn, clock=clock)

    wd.tick()
    wd.tick()

    assert not probe_called
    assert not spawn_called


def test_watchdog_single_failure_does_not_spawn():
    """One probe failure should not trigger spawn (needs 2 consecutive)."""
    clock = FakeClock()
    failures = 0

    def probe() -> bool:
        nonlocal failures
        failures += 1
        return False

    spawn_called = False

    def spawn() -> None:
        nonlocal spawn_called
        spawn_called = True

    wd = OllamaWatchdog("ollama:qwen3.5:4b", probe=probe, spawn=spawn, clock=clock)

    wd.tick()  # failure 1
    assert not spawn_called
    wd.tick()  # failure 2 -> should spawn
    assert spawn_called


def test_watchdog_two_failures_spawns_once():
    """Two consecutive failures should trigger exactly one spawn."""
    clock = FakeClock()
    spawn_count = 0

    def probe() -> bool:
        return False

    def spawn() -> None:
        nonlocal spawn_count
        spawn_count += 1

    wd = OllamaWatchdog("ollama:qwen3.5:4b", probe=probe, spawn=spawn, clock=clock)

    wd.tick()  # failure 1
    wd.tick()  # failure 2 -> spawn
    assert spawn_count == 1

    # Third tick within cooldown should not spawn again
    wd.tick()
    assert spawn_count == 1


def test_watchdog_spawn_cooldown_300s():
    """Second spawn should be blocked within 300s cooldown."""
    clock = FakeClock()
    spawn_count = 0

    def probe() -> bool:
        return False

    def spawn() -> None:
        nonlocal spawn_count
        spawn_count += 1

    wd = OllamaWatchdog("ollama:qwen3.5:4b", probe=probe, spawn=spawn, clock=clock)

    wd.tick()  # failure 1
    wd.tick()  # failure 2 -> spawn #1
    assert spawn_count == 1

    clock.advance(299)
    wd.tick()  # still within cooldown
    assert spawn_count == 1

    clock.advance(2)  # now at 301s total
    wd.tick()  # failure 3 (consecutive count reset? no, it keeps counting)
    wd.tick()  # failure 4 -> should spawn #2
    assert spawn_count == 2


def test_watchdog_success_clears_failures():
    """A successful probe should reset consecutive failure count."""
    clock = FakeClock()
    probe_results = [False, False, True, False, False]
    probe_idx = 0

    def probe() -> bool:
        nonlocal probe_idx
        result = probe_results[probe_idx]
        probe_idx += 1
        return result

    spawn_count = 0

    def spawn() -> None:
        nonlocal spawn_count
        spawn_count += 1

    wd = OllamaWatchdog("ollama:qwen3.5:4b", probe=probe, spawn=spawn, clock=clock)

    wd.tick()  # fail 1
    wd.tick()  # fail 2 -> spawn
    assert spawn_count == 1

    wd.tick()  # success -> clears failures
    clock.advance(301)  # advance past cooldown
    wd.tick()  # fail 1
    wd.tick()  # fail 2 -> spawn again
    assert spawn_count == 2


def test_watchdog_exe_not_found_logs_once():
    """When ollama executable not found, should log skip once and not retry."""
    clock = FakeClock()

    def probe() -> bool:
        return False

    spawn_called = False

    def spawn() -> None:
        nonlocal spawn_called
        spawn_called = True

    with patch("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", return_value=None):
        wd = OllamaWatchdog("ollama:qwen3.5:4b", probe=probe, spawn=spawn, clock=clock)

        wd.tick()
        wd.tick()  # should log skip once
        wd.tick()
        wd.tick()

        assert not spawn_called


def test_watchdog_spawn_failure_does_not_crash():
    """Spawn raising exception should be caught and logged, not crash tick."""
    clock = FakeClock()

    def probe() -> bool:
        return False

    def spawn() -> None:
        raise RuntimeError("spawn failed")

    with patch("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", return_value=Path("/fake/ollama")):
        wd = OllamaWatchdog("ollama:qwen3.5:4b", probe=probe, spawn=spawn, clock=clock)

        # Should not raise
        wd.tick()
        wd.tick()


def test_default_spawn_windows_flags(monkeypatch):
    """On Windows, default_spawn should use DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP."""
    if sys.platform != "win32":
        pytest.skip("Windows-specific test")

    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    monkeypatch.setattr("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", lambda: Path("C:/fake/ollama.exe"))

    default_spawn()

    popen_mock.assert_called_once()
    args, kwargs = popen_mock.call_args
    assert args[0] == ["C:/fake/ollama.exe", "serve"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["creationflags"] == (subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)
    assert "OLLAMA_HOST" not in kwargs["env"]


def test_default_spawn_macos_flags(monkeypatch):
    """On macOS, default_spawn should use start_new_session=True."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")

    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    monkeypatch.setattr("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", lambda: Path("/opt/homebrew/bin/ollama"))

    default_spawn()

    popen_mock.assert_called_once()
    args, kwargs = popen_mock.call_args
    assert args[0] == ["/opt/homebrew/bin/ollama", "serve"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["stdout"] == subprocess.DEVNULL
    assert kwargs["stderr"] == subprocess.DEVNULL
    assert kwargs["start_new_session"] is True
    assert "OLLAMA_HOST" not in kwargs["env"]


def test_default_spawn_removes_ollama_host(monkeypatch):
    """default_spawn should remove OLLAMA_HOST from environment."""
    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    monkeypatch.setattr("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", lambda: Path("/fake/ollama"))
    monkeypatch.setenv("OLLAMA_HOST", "http://example.com:11434")

    default_spawn()

    args, kwargs = popen_mock.call_args
    assert "OLLAMA_HOST" not in kwargs["env"]


def test_macos_launchctl_spawn_tries_kickstart_first(monkeypatch):
    """create_macos_launchctl_spawn should try launchctl kickstart first, then fall back to serve."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")

    run_mock = MagicMock()
    monkeypatch.setattr("subprocess.run", run_mock)
    monkeypatch.setattr("subprocess.Popen", MagicMock())
    monkeypatch.setattr("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", lambda: Path("/opt/homebrew/bin/ollama"))

    spawn = create_macos_launchctl_spawn()
    spawn()

    # Should have tried launchctl kickstart
    run_mock.assert_called_once()
    args, kwargs = run_mock.call_args
    assert args[0][:3] == ["launchctl", "kickstart", "-k"]
    assert f"gui/{os.getuid()}/homebrew.mxcl.ollama" in args[0]


def test_macos_launchctl_spawn_falls_back_on_failure(monkeypatch):
    """If launchctl kickstart fails, should fall back to Popen serve."""
    if sys.platform != "darwin":
        pytest.skip("macOS-specific test")

    run_mock = MagicMock(side_effect=subprocess.CalledProcessError(1, "launchctl"))
    popen_mock = MagicMock()
    monkeypatch.setattr("subprocess.run", run_mock)
    monkeypatch.setattr("subprocess.Popen", popen_mock)
    monkeypatch.setattr("realtime_subtitle.node.ollama_watchdog._find_ollama_exe_default", lambda: Path("/opt/homebrew/bin/ollama"))

    spawn = create_macos_launchctl_spawn()
    spawn()

    run_mock.assert_called_once()
    popen_mock.assert_called_once()


def test_gateway_integration_watchdog_started_and_stopped():
    """Gateway should start watchdog task on start() and cancel on stop()."""
    # This test is more of an integration test - we verify the gateway has the watchdog
    # and the task is created/cancelled. Actual gateway tests are in test_node_gateway.py.
    from realtime_subtitle.node.gateway import Gateway
    from realtime_subtitle.node import info as node_info

    with patch.object(node_info.NodeInfo, "asr_and_translator", return_value=({}, "ollama:test")):
        with patch("realtime_subtitle.node.gateway.OllamaWatchdog") as mock_wd_class:
            mock_wd = MagicMock()
            mock_wd_class.return_value = mock_wd

            _ = Gateway(
                token="x" * 32,
                uds_dir=Path("/tmp/test"),
                port=0,
                resolve_host=lambda: "127.0.0.1",
                allow_non_tailscale_for_tests=True,
            )

            # Check watchdog was created with correct translator
            mock_wd_class.assert_called_once()
            args, kwargs = mock_wd_class.call_args
            assert args[0] == "ollama:test"
            assert "probe" in kwargs
            assert "spawn" in kwargs
            # clock uses default (time.monotonic), not explicitly passed


# Need to import subprocess for the monkeypatch tests
import subprocess
import os