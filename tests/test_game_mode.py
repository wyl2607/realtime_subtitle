"""模式系统单测：_apply_mode 套值 + Ctrl+Alt+G 跳「性能」的记忆语义
（不加载模型、不开音频、不建窗口）。

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
    import realtime_subtitle.app as main  # noqa: E402  重量级但只 import 模块，不实例化
finally:
    ctypes.windll.kernel32.CreateMutexW = _orig_create
    ctypes.windll.kernel32.GetLastError = _orig_getlast

import realtime_subtitle.config as config  # noqa: E402


class _FakeWindow:
    def __init__(self):
        self.modes = []  # 每次 notify_mode_applied 收到的模式名

    def show_status(self, *_):
        pass

    def notify_mode_applied(self, name):
        self.modes.append(name)  # 真窗会 emit 信号刷面板；单测不建 Qt


class _FakeTranslator:
    def __init__(self):
        self.warm_calls = 0
        self.unloaded = []  # 每次预热要求卸载的旧模型名
        self.warmed = []  # 每次预热要求加载的新模型名（显式传入，不现读config）

    def request_warm_model(self, old_model=None, new_model=None):
        self.warm_calls += 1
        self.unloaded.append(old_model)
        self.warmed.append(new_model)


def _make_app(baseline="test-main-model", translator=True):
    app = main.SubtitleApp.__new__(main.SubtitleApp)  # 跳过 __init__
    app.subtitle_window = _FakeWindow()
    app.translator = _FakeTranslator() if translator else None
    app._baseline_ollama_model = baseline
    app._current_mode = None
    app._mode_before_perf = None
    return app


def _snapshot():
    return (config.CHUNK_SUBMIT_SECONDS, config.WHISPER_BEAM_SIZE,
            config.DRAFT_TRANSLATION, config.OLLAMA_MODEL,
            config.MAX_SENTENCE_PAIRS, config.SHOW_BILINGUAL,
            config.IDLE_FLUSH_SEC, config.TRANSLATION_STYLE)


def _restore(snap):
    (config.CHUNK_SUBMIT_SECONDS, config.WHISPER_BEAM_SIZE,
     config.DRAFT_TRANSLATION, config.OLLAMA_MODEL,
     config.MAX_SENTENCE_PAIRS, config.SHOW_BILINGUAL,
     config.IDLE_FLUSH_SEC, config.TRANSLATION_STYLE) = snap


def test_apply_perf_mode_sets_every_key_and_switches_model():
    snap = _snapshot()
    orig_game_model = config.GAME_MODE_OLLAMA_MODEL
    try:
        # 模型名固定为假值，与本机 config_local 解耦：小显存档的 config_local 会把
        # GAME_MODE_OLLAMA_MODEL 设 None（或主模型本来就等于性能模型），那种机器上
        # 直接读全局会让"切换+预热"的断言全部错位（2026-07-17 全新安装演练实测）
        config.OLLAMA_MODEL = "test-main-model"
        config.GAME_MODE_OLLAMA_MODEL = "test-perf-model"
        app = _make_app()

        assert app._apply_mode("性能") is True
        for key, val in config.PRESETS["性能"].items():
            assert getattr(config, key) == val, key
        assert config.OLLAMA_MODEL == "test-perf-model"
        assert app.translator.warm_calls == 1
        assert app.translator.unloaded == ["test-main-model"]  # 必须卸旧模型（keep_alive=2h会赖满显存）
        # new_model 必须显式传入调用当下的目标值，不能让 worker 执行时现读
        # config.OLLAMA_MODEL——热键连按时那个全局可能已被后续切换改掉
        # （压测复现过：连按6次后 ollama ps 里两个模型同时常驻）
        assert app.translator.warmed == ["test-perf-model"]
        assert app._current_mode == "性能"
        assert app.subtitle_window.modes == ["性能"]
    finally:
        config.GAME_MODE_OLLAMA_MODEL = orig_game_model
        _restore(snap)


def test_non_perf_mode_returns_to_baseline_model():
    """非性能模式统一切回启动时拍下的基线模型，而不是 PRESETS 里写死的值
    （不同机器 config_local 的 OLLAMA_MODEL 不一样，写死会覆盖显存分档）。"""
    snap = _snapshot()
    try:
        config.OLLAMA_MODEL = "test-perf-model"
        app = _make_app(baseline="machine-specific-model")
        app._apply_mode("直播")
        assert config.OLLAMA_MODEL == "machine-specific-model"
        assert app.translator.warmed == ["machine-specific-model"]
        assert config.TRANSLATION_STYLE == config.PRESETS["直播"]["TRANSLATION_STYLE"]
    finally:
        _restore(snap)


def test_apply_mode_is_idempotent_on_model():
    """模型已经等于目标值时不重复预热（省一次卸载+加载往返）。"""
    snap = _snapshot()
    try:
        config.OLLAMA_MODEL = "same-model"
        app = _make_app(baseline="same-model")
        app._apply_mode("看剧")
        assert app.translator.warm_calls == 0
    finally:
        _restore(snap)


def test_apply_mode_none_marks_custom_without_touching_config():
    """用户手动拨滑块 → 只更新记账和指示器，一个 config 值都不动。"""
    snap = _snapshot()
    try:
        app = _make_app()
        app._apply_mode("精听")
        before = _snapshot()
        app.translator.warm_calls = 0

        assert app._apply_mode(None) is True
        assert _snapshot() == before
        assert app._current_mode is None
        assert app.translator.warm_calls == 0
        assert app.subtitle_window.modes[-1] is None
    finally:
        _restore(snap)


def test_apply_mode_unknown_name_changes_nothing():
    snap = _snapshot()
    try:
        app = _make_app()
        assert app._apply_mode("不存在的模式") is False
        assert _snapshot() == snap
        assert app._current_mode is None
    finally:
        _restore(snap)


def test_apply_mode_survives_translator_still_loading():
    """☠️ 窗口先显示、模型后台加载的那十几秒里 translator 还是 None，
    这期间点模式按钮不能崩（规格没覆盖这个窗口期）。"""
    snap = _snapshot()
    orig_game_model = config.GAME_MODE_OLLAMA_MODEL
    try:
        config.OLLAMA_MODEL = "test-main-model"
        config.GAME_MODE_OLLAMA_MODEL = "test-perf-model"
        app = _make_app(translator=False)
        assert app._apply_mode("性能") is True
        assert config.OLLAMA_MODEL == "test-perf-model"  # config 照改，退出时会一并卸载
    finally:
        config.GAME_MODE_OLLAMA_MODEL = orig_game_model
        _restore(snap)


def test_perf_hotkey_jumps_and_returns_to_previous_mode():
    """Ctrl+Alt+G：跳「性能」，再按跳回进入前那个模式（不是固定回默认）。"""
    snap = _snapshot()
    try:
        app = _make_app()
        app._apply_mode("看剧")
        app._toggle_perf_hotkey()
        assert app._current_mode == "性能"
        app._toggle_perf_hotkey()
        assert app._current_mode == "看剧"

        # 从「直播」出发同理
        app._apply_mode("直播")
        app._toggle_perf_hotkey()
        assert app._current_mode == "性能"
        app._toggle_perf_hotkey()
        assert app._current_mode == "直播"
    finally:
        _restore(snap)


def test_perf_hotkey_from_custom_returns_to_default():
    """从未选过模式（自定义）时按热键 → 性能；再按回默认「直播」。"""
    snap = _snapshot()
    try:
        app = _make_app()
        assert app._current_mode is None
        app._toggle_perf_hotkey()
        assert app._current_mode == "性能"
        app._toggle_perf_hotkey()
        assert app._current_mode == "直播"
    finally:
        _restore(snap)


def test_repeat_perf_toggle_stable():
    snap = _snapshot()
    try:
        app = _make_app()
        app._apply_mode("直播")
        pinned = _snapshot()
        for _ in range(3):
            app._toggle_perf_hotkey()
            app._toggle_perf_hotkey()
        assert app._current_mode == "直播"
        assert _snapshot() == pinned
    finally:
        _restore(snap)


if __name__ == "__main__":
    import pytest
    import sys
    sys.exit(pytest.main([__file__, "-q"]))
