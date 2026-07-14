"""游戏模式 Ctrl+Alt+G 四旋钮切换/恢复单测（不加载模型、不开音频、不建窗口）。

运行: venv\\Scripts\\python.exe -m pytest test_game_mode.py -q

⚠️ main.py 顶部有单实例 Mutex（已在运行会 sys.exit(0)），import 前必须打桩，
否则字幕程序开着时 pytest 进程会直接退出。打桩还保证测试进程不真持有
mutex——不会挡住用户随后启动真程序。
"""
import ctypes

_orig_create = ctypes.windll.kernel32.CreateMutexW
_orig_getlast = ctypes.windll.kernel32.GetLastError
ctypes.windll.kernel32.CreateMutexW = lambda *a: 1
ctypes.windll.kernel32.GetLastError = lambda: 0
try:
    import main  # noqa: E402  重量级但只 import 模块，不实例化
finally:
    ctypes.windll.kernel32.CreateMutexW = _orig_create
    ctypes.windll.kernel32.GetLastError = _orig_getlast

import config  # noqa: E402


class _FakeWindow:
    def show_status(self, *_):
        pass

    def notify_game_mode(self, *_):
        pass  # 真窗会 emit 信号刷设置面板；单测不建 Qt


class _FakeTranslator:
    def __init__(self):
        self.warm_calls = 0
        self.unloaded = []  # 每次预热要求卸载的旧模型名
        self.warmed = []  # 每次预热要求加载的新模型名（显式传入，不现读config）

    def request_warm_model(self, old_model=None, new_model=None):
        self.warm_calls += 1
        self.unloaded.append(old_model)
        self.warmed.append(new_model)


def _make_app():
    app = main.SubtitleApp.__new__(main.SubtitleApp)  # 跳过 __init__
    app.subtitle_window = _FakeWindow()
    app.translator = _FakeTranslator()
    return app


def _snapshot():
    return (config.CHUNK_SUBMIT_SECONDS, config.WHISPER_BEAM_SIZE,
            config.DRAFT_TRANSLATION, config.OLLAMA_MODEL)


def _restore(snap):
    (config.CHUNK_SUBMIT_SECONDS, config.WHISPER_BEAM_SIZE,
     config.DRAFT_TRANSLATION, config.OLLAMA_MODEL) = snap


def test_game_mode_switches_and_restores_all_four_knobs():
    snap = _snapshot()
    try:
        app = _make_app()
        app._toggle_game_mode()  # 开
        assert config.CHUNK_SUBMIT_SECONDS == config.GAME_MODE_SUBMIT_SECONDS
        assert config.WHISPER_BEAM_SIZE == config.GAME_MODE_BEAM_SIZE
        assert config.DRAFT_TRANSLATION is False
        assert config.OLLAMA_MODEL == config.GAME_MODE_OLLAMA_MODEL
        assert app.translator.warm_calls == 1  # 切模型后预热
        assert app.translator.unloaded == [snap[3]]  # 必须要求卸载旧模型（否则keep_alive=2h赖满显存）
        # new_model 必须显式传入调用当下的目标值，不能让worker执行时现读
        # config.OLLAMA_MODEL——热键连按时那个全局可能已经被后续toggle改掉
        # （压测复现过：连按6次后ollama ps里9b/4b同时常驻）
        assert app.translator.warmed == [config.GAME_MODE_OLLAMA_MODEL]

        app._toggle_game_mode()  # 关
        assert _snapshot() == snap  # 四个值原样恢复
        assert app.translator.warm_calls == 2  # 切回也预热
        assert app.translator.unloaded[1] == config.GAME_MODE_OLLAMA_MODEL  # 切回时卸掉4b
        assert app.translator.warmed[1] == snap[3]  # 切回时目标是原模型
    finally:
        _restore(snap)


def test_game_mode_preserves_user_tuned_values():
    snap = _snapshot()
    try:
        config.CHUNK_SUBMIT_SECONDS = 0.7  # 模拟用户在⚙️面板改过
        config.OLLAMA_MODEL = "qwen3:8b"   # 模拟用户 config_local 回退
        app = _make_app()
        app._toggle_game_mode()
        app._toggle_game_mode()
        assert config.CHUNK_SUBMIT_SECONDS == 0.7
        assert config.OLLAMA_MODEL == "qwen3:8b"
    finally:
        _restore(snap)


def test_game_mode_model_none_means_no_switch():
    snap = _snapshot()
    orig_game_model = config.GAME_MODE_OLLAMA_MODEL
    try:
        config.GAME_MODE_OLLAMA_MODEL = None
        app = _make_app()
        app._toggle_game_mode()
        assert config.OLLAMA_MODEL == snap[3]  # 模型没动
        assert app.translator.warm_calls == 0  # 不预热
        app._toggle_game_mode()
        assert _snapshot() == snap
        assert app.translator.warm_calls == 0
    finally:
        config.GAME_MODE_OLLAMA_MODEL = orig_game_model
        _restore(snap)


def test_game_mode_repeat_toggle_stable():
    snap = _snapshot()
    try:
        app = _make_app()
        for _ in range(3):
            app._toggle_game_mode()
            app._toggle_game_mode()
        assert _snapshot() == snap
    finally:
        _restore(snap)


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
