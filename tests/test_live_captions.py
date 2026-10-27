"""纯快照分段和伪 UIA 生命周期；不创建窗口、不加载模型、不发网络请求。"""
from contextlib import contextmanager
from types import SimpleNamespace
from threading import Event, get_ident

import pytest

from realtime_subtitle.capture import live_captions as lc


def test_sentence_waits_for_stability_and_duplicate_snapshots_do_not_reemit():
    differ = lc.CaptionDiffer(stable_seconds=1)
    assert differ.update("Guten Morgen.", now=0) == lc.CaptionUpdate([], "Guten Morgen.")
    assert differ.update("Guten Morgen.", now=.99).finalized == []
    assert differ.update("Guten Morgen.", now=1) == lc.CaptionUpdate(["Guten Morgen."], "")
    assert differ.update("Guten Morgen.", now=20) == lc.CaptionUpdate([], "")


def test_prime_skips_text_left_in_window_from_previous_run():
    differ = lc.CaptionDiffer(stable_seconds=1)
    differ.prime("Alter Satz von gestern. Noch ein alter")
    assert differ.update("Alter Satz von gestern. Noch ein alter", now=5) == lc.CaptionUpdate([], "")
    result = differ.update("Alter Satz von gestern. Noch ein alter Neuer Satz.", now=6)
    assert result.finalized == [] and result.draft == "Neuer Satz."
    assert differ.update("Alter Satz von gestern. Noch ein alter Neuer Satz.", now=7).finalized == ["Neuer Satz."]


def test_late_punctuation_alone_is_not_a_sentence():
    differ = lc.CaptionDiffer(stable_seconds=1)
    differ.prime("Morgen wird es regnen")
    assert differ.update("Morgen wird es regnen.", now=0).finalized == []
    assert differ.update("Morgen wird es regnen.", now=2).finalized == []
    assert differ.update("Morgen wird es regnen. Danke.", now=3).finalized == []
    assert differ.update("Morgen wird es regnen. Danke.", now=5).finalized == ["Danke."]


def test_new_text_finalizes_preceding_sentence_immediately():
    differ = lc.CaptionDiffer()
    result = differ.update("Der Zug kommt. Bitte warten", now=0)
    assert result.finalized == ["Der Zug kommt."]
    assert result.draft == "Bitte warten"
    assert differ.update("Der Zug kommt. Bitte warten!", now=.1).finalized == []
    assert differ.update("Der Zug kommt. Bitte warten!", now=1).finalized == ["Bitte warten!"]


def test_german_revision_resets_stability_timer():
    differ = lc.CaptionDiffer(stable_seconds=1)
    assert differ.update("Das ist eine Katze.", now=0).finalized == []
    assert differ.update("Das ist eine Karte.", now=.7).finalized == []
    assert differ.update("Das ist eine Karte.", now=1.1).finalized == []
    assert differ.update("Das ist eine Karte.", now=1.8).finalized == ["Das ist eine Karte."]


def test_scroll_removes_old_lines_without_replaying_final_sentences():
    differ = lc.CaptionDiffer()
    assert differ.update("Erster Satz. Zweiter Satz. Noch ein", now=0).finalized == [
        "Erster Satz.", "Zweiter Satz."]
    result = differ.update("Zweiter Satz. Noch ein letzter Satz!", now=.1)
    assert result.finalized == []
    assert result.draft == "Noch ein letzter Satz!"
    assert differ.update("Noch ein letzter Satz!", now=1).finalized == ["Noch ein letzter Satz!"]
    assert differ.update("Noch ein letzter Satz!", now=2).finalized == []


def test_scroll_and_last_sentence_revision_in_same_snapshot():
    differ = lc.CaptionDiffer()
    differ.update("Der erste Satz. Die blaue Katze", now=0)
    result = differ.update("Die blaue Karte liegt hier. Weiter", now=.2)
    assert result == lc.CaptionUpdate(["Die blaue Karte liegt hier."], "Weiter")


def test_identical_spoken_sentences_are_distinct_occurrences():
    differ = lc.CaptionDiffer()
    assert differ.update("Ja. Ja. Noch", now=0).finalized == ["Ja.", "Ja."]
    assert differ.update("Ja. Ja. Noch", now=1).finalized == []
    assert differ.update("Ja. Ja. Noch. Ja. Ende", now=2).finalized == ["Noch.", "Ja."]


def test_empty_snapshot_clears_draft_but_does_not_replay_old_final():
    differ = lc.CaptionDiffer()
    differ.update("Fertig. Entwurf", now=0)
    assert differ.update("", now=1) == lc.CaptionUpdate([], "")
    assert differ.update(None, now=2) == lc.CaptionUpdate([], "")
    assert differ.update("Fertig. Entwurf", now=3) == lc.CaptionUpdate([], "Entwurf")


def test_new_unrelated_caption_after_scroll_is_not_discarded():
    differ = lc.CaptionDiffer(stable_seconds=0)
    assert differ.update("ABC.", now=0).finalized == ["ABC."]
    assert differ.update("XYZ!", now=1).finalized == ["XYZ!"]


def test_finalized_tail_revision_is_not_emitted_as_fragment():
    differ = lc.CaptionDiffer(stable_seconds=0)
    assert differ.update("Hallo Welt.", now=0).finalized == ["Hallo Welt."]
    assert differ.update("Hallo schöne Welt.", now=1).finalized == []
    assert differ.update("Hallo schöne Welt. Weiter.", now=2).finalized == ["Weiter."]


def test_chinese_punctuation_and_closing_quotes():
    differ = lc.CaptionDiffer(stable_seconds=1)
    result = differ.update("今天晴天。你听见了吗？他说：“听见了！”后面", now=0)
    assert result == lc.CaptionUpdate(["今天晴天。", "你听见了吗？", "他说：“听见了！”"], "后面")
    result = differ.update("你听见了吗？他说：“听见了！”后面还有。", now=.2)
    assert result == lc.CaptionUpdate([], "后面还有。")
    assert differ.update("你听见了吗？他说：“听见了！”后面还有。", now=1.3).finalized == ["后面还有。"]


def test_whitespace_normalization_does_not_restart_stability():
    differ = lc.CaptionDiffer(stable_seconds=1)
    differ.update("Guten\n Morgen.  ", now=0)
    assert differ.update("Guten Morgen.", now=1).finalized == ["Guten Morgen."]


def test_decimal_point_is_not_a_sentence_boundary():
    differ = lc.CaptionDiffer()
    result = differ.update("Das sind 3.14 Meter. Weiter", now=0)
    assert result == lc.CaptionUpdate(["Das sind 3.14 Meter."], "Weiter")


def test_runon_forced_by_character_limit_at_word_boundary():
    differ = lc.CaptionDiffer(max_chars=12, max_seconds=100)
    assert differ.update("eins zwei drei vier", now=0) == lc.CaptionUpdate(["eins zwei"], "drei vier")
    assert differ.update("eins zwei drei vier", now=10) == lc.CaptionUpdate([], "drei vier")
    assert differ.update("eins zwei drei vier fünf sechs", now=11).finalized == ["drei vier"]


def test_chinese_runon_forced_in_bounded_chunks():
    differ = lc.CaptionDiffer(max_chars=4)
    assert differ.update("甲乙丙丁戊己庚辛壬", now=0) == lc.CaptionUpdate(["甲乙丙丁", "戊己庚辛"], "壬")
    assert differ.update("戊己庚辛壬", now=1) == lc.CaptionUpdate([], "壬")


def test_runon_timeout_is_not_reset_by_continuous_growth():
    differ = lc.CaptionDiffer(max_seconds=2, max_chars=100)
    assert differ.update("ein", now=0).finalized == []
    assert differ.update("ein langer", now=1).finalized == []
    assert differ.update("ein langer Satz", now=2) == lc.CaptionUpdate(["ein langer Satz"], "")
    assert differ.update("ein langer Satz wächst", now=2.5) == lc.CaptionUpdate([], "wächst")
    assert differ.update("ein langer Satz wächst", now=4.5) == lc.CaptionUpdate(["wächst"], "")


def test_revised_runon_does_not_restart_timeout():
    differ = lc.CaptionDiffer(max_seconds=2)
    differ.update("die blaue Katze", now=0)
    assert differ.update("die blaue Karte", now=1).finalized == []
    assert differ.update("die blaue Karte", now=2).finalized == ["die blaue Karte"]


@pytest.mark.parametrize("kwargs", [{"stable_seconds": -1}, {"max_chars": 0}, {"max_seconds": 0}])
def test_invalid_differ_limits(kwargs):
    with pytest.raises(ValueError):
        lc.CaptionDiffer(**kwargs)


def test_source_selection_on_non_windows_falls_back_without_optional_import(monkeypatch, capsys):
    monkeypatch.setattr(lc.sys, "platform", "linux")
    monkeypatch.setattr(lc.importlib.util, "find_spec", lambda name: pytest.fail("must not probe Windows deps"))
    assert lc.prepare_live_captions_reader("livecaptions") is None
    assert "Windows 11 22H2" in capsys.readouterr().out
    assert lc.prepare_live_captions_reader("whisper") is None
    assert capsys.readouterr().out == ""


@pytest.mark.parametrize("build, supported", [(22000, False), (22621, True), (26100, True)])
def test_windows_build_requirement(monkeypatch, build, supported):
    monkeypatch.setattr(lc.sys, "platform", "win32")
    monkeypatch.setattr(lc.sys, "getwindowsversion", lambda: SimpleNamespace(major=10, build=build), raising=False)
    assert lc._windows_supported() is supported


def test_missing_optional_dependency_falls_back(monkeypatch, capsys):
    monkeypatch.setattr(lc, "_windows_supported", lambda: True)
    monkeypatch.setattr(lc.importlib.util, "find_spec", lambda name: None)
    assert lc.prepare_live_captions_reader("livecaptions") is None
    assert "pip install uiautomation" in capsys.readouterr().out


def test_missing_executable_falls_back(monkeypatch, capsys):
    monkeypatch.setattr(lc, "_windows_supported", lambda: True)
    monkeypatch.setattr(lc, "_check_uia_available", lambda: None)
    monkeypatch.setattr(lc, "_find_executable", lambda: None)
    assert lc.prepare_live_captions_reader("livecaptions") is None
    assert "LiveCaptions.exe" in capsys.readouterr().out


def test_prepare_preflights_import_and_com_on_a_background_thread(monkeypatch):
    monkeypatch.setattr(lc, "_windows_supported", lambda: True)
    monkeypatch.setattr(lc.importlib.util, "find_spec", lambda name: object())
    monkeypatch.setattr(lc, "_find_executable", lambda: "C:/Windows/System32/LiveCaptions.exe")
    threads = []
    @contextmanager
    def initialize():
        threads.append(get_ident())
        yield
    def import_module(name):
        threads.append(get_ident())
        return SimpleNamespace(UIAutomationInitializerInThread=initialize)
    monkeypatch.setattr(lc.importlib, "import_module", import_module)
    reader = lc.prepare_live_captions_reader("livecaptions", hide_window=False)
    assert isinstance(reader, lc.LiveCaptionsReader)
    assert reader.hide_window is False
    assert len(threads) == 2 and threads[0] == threads[1] != get_ident()


def test_reader_launches_without_shell_only_when_process_is_absent(monkeypatch):
    pids = set()
    launches = []
    monkeypatch.setattr(lc, "_running_caption_pids", lambda: pids)
    monkeypatch.setattr(lc.subprocess, "Popen", lambda args, **kwargs: launches.append((args, kwargs)) or SimpleNamespace(pid=123))
    element = SimpleNamespace(Exists=lambda *args: True)
    window = SimpleNamespace(ProcessId=123, Control=lambda **kwargs: element)
    auto = SimpleNamespace(GetRootControl=lambda: SimpleNamespace(GetChildren=lambda: [window]))
    reader = lc.LiveCaptionsReader("LiveCaptions.exe", hide_window=False)
    assert reader._find_controls(auto) == (window, element)
    assert launches == [(["LiveCaptions.exe"], {"shell": False})]
    pids.add(123)
    assert reader._find_controls(auto) == (window, element)
    assert len(launches) == 1


def test_tasklist_pid_parsing_ignores_localized_information(monkeypatch):
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(stdout='信息: 没有别的任务\n"LiveCaptions.exe","123","Console","1","42 K"\n')
    monkeypatch.setattr(lc.subprocess, "run", run)
    assert lc._running_caption_pids() == {123}
    assert calls[0][0][0] == "tasklist"
    assert not calls[0][1].get("shell", False)


def _fake_reader_environment(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "repo_path", lambda flag: str(tmp_path / flag))
    monkeypatch.setattr(lc, "POLL_INTERVAL_SECONDS", .001)
    monkeypatch.setattr(lc, "RECONNECT_INITIAL_SECONDS", .002)
    monkeypatch.setattr(lc, "RECONNECT_MAX_SECONDS", .008)
    events = []
    @contextmanager
    def initialize():
        events.append(("init", get_ident()))
        try:
            yield
        finally:
            events.append(("uninit", get_ident()))
    auto = SimpleNamespace(UIAutomationInitializerInThread=initialize)
    monkeypatch.setattr(lc.importlib, "import_module", lambda name: auto)
    return events


class _FreshElement:
    """连上那一刻窗口是空的（prime 读到 ""），之后才出现 text。"""

    def __init__(self, text):
        self._reads = 0
        self._text = text

    def Exists(self, *args):
        return True

    @property
    def Name(self):
        self._reads += 1
        return "" if self._reads == 1 else self._text


def test_reader_callbacks_and_restore_are_on_com_thread(monkeypatch, tmp_path):
    events = _fake_reader_environment(monkeypatch, tmp_path)
    reader = lc.LiveCaptionsReader("LiveCaptions.exe")
    monkeypatch.setattr(reader, "_find_controls", lambda auto: (object(), _FreshElement("Hallo.")))
    monkeypatch.setattr(reader, "_hide_window", lambda window: events.append(("hide", get_ident())))
    monkeypatch.setattr(reader, "_restore_windows", lambda: events.append(("restore", get_ident())))
    updates = []
    completed = Event()
    statuses = []
    def update(finalized, draft):
        updates.append((finalized, draft))
        events.append(("update", get_ident()))
        if len(updates) == 2:
            reader.stop()  # 从回调停止也不能 join 自己。
            completed.set()
    reader.start(update, statuses.append)
    assert completed.wait(2)
    reader.stop()
    assert updates == [([], "Hallo."), ([], "Hallo.")]
    assert statuses == [lc.STATUS_LOCAL]
    assert [name for name, _ in events] == ["init", "hide", "update", "update", "restore", "uninit"]
    assert len({ident for _, ident in events}) == 1
    assert events[0][1] != get_ident()
    assert not reader._thread.is_alive()


def test_reader_reconnect_backoff_then_recovers(monkeypatch, tmp_path):
    events = _fake_reader_environment(monkeypatch, tmp_path)
    reader = lc.LiveCaptionsReader("LiveCaptions.exe", hide_window=False)
    attempts = []
    waits = []
    def find(auto):
        attempts.append(True)
        if len(attempts) < 3:
            raise LookupError("not ready")
        return object(), SimpleNamespace(Exists=lambda *args: True, Name="")
    monkeypatch.setattr(reader, "_find_controls", find)
    monkeypatch.setattr(reader, "_wait_retry", lambda delay, on_update: waits.append(delay))
    statuses = []
    completed = Event()
    def update(finalized, draft):
        reader.stop()
        completed.set()
    reader.start(update, statuses.append)
    assert completed.wait(2)
    reader.stop()
    assert waits == [.002, .004]
    assert statuses[-1] == lc.STATUS_LOCAL
    assert all("正在重连" in status for status in statuses[:-1])
    assert [name for name, _ in events] == ["init", "uninit"] * 3


def test_disappearing_element_reconnects(monkeypatch, tmp_path):
    _fake_reader_environment(monkeypatch, tmp_path)
    reader = lc.LiveCaptionsReader("LiveCaptions.exe", hide_window=False)
    attempts = []
    def find(auto):
        attempts.append(True)
        return object(), SimpleNamespace(Exists=lambda *args: len(attempts) > 1, Name="")
    monkeypatch.setattr(reader, "_find_controls", find)
    statuses = []
    completed = Event()
    def update(finalized, draft):
        if len(attempts) > 1:
            reader.stop()
            completed.set()
    reader.start(update, statuses.append)
    assert completed.wait(2)
    reader.stop()
    assert len(attempts) == 2
    assert any("正在重连" in status for status in statuses)
    assert statuses[-1] == lc.STATUS_LOCAL


def test_paused_reader_consumes_snapshots_without_replay(monkeypatch, tmp_path):
    _fake_reader_environment(monkeypatch, tmp_path)
    pause = tmp_path / ".paused"
    pause.touch()
    reader = lc.LiveCaptionsReader("LiveCaptions.exe", hide_window=False)
    monkeypatch.setattr(reader, "_find_controls", lambda auto: (
        object(), _FreshElement("已暂停。新的草稿")))
    updates = []
    completed = Event()
    def update(finalized, draft):
        updates.append((finalized, draft))
        if len(updates) == 1:
            pause.unlink()
        else:
            reader.stop()
            completed.set()
    reader.start(update)
    assert completed.wait(2)
    reader.stop()
    assert updates == [([], ""), ([], "新的草稿")]


def test_stop_flag_prevents_uia_import(monkeypatch, tmp_path):
    monkeypatch.setattr(lc, "repo_path", lambda flag: str(tmp_path / flag))
    (tmp_path / ".stop").touch()
    monkeypatch.setattr(lc.importlib, "import_module", lambda name: pytest.fail("already stopped"))
    reader = lc.LiveCaptionsReader("LiveCaptions.exe")
    reader.start(lambda *args: pytest.fail("already stopped"))
    reader.stop()
    assert not reader._thread.is_alive()


def test_window_visibility_is_restored_without_terminating_process(monkeypatch):
    import ctypes
    actions = []
    user32 = SimpleNamespace(
        IsWindowVisible=lambda handle: True,
        IsIconic=lambda handle: False,
        IsZoomed=lambda handle: True,
        IsWindow=lambda handle: True,
        ShowWindow=lambda handle, state: actions.append((handle, state)),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    reader = lc.LiveCaptionsReader("LiveCaptions.exe")
    reader._hide_window(SimpleNamespace(NativeWindowHandle=42))
    reader._restore_windows()
    # 6 = SW_MINIMIZE；绝不能是 0（SW_HIDE 会让字幕元素从 UIA 树里消失）
    assert actions == [(42, 6), (42, 3)]
    assert reader._hidden_windows == {}


def test_already_minimized_window_is_left_alone(monkeypatch):
    import ctypes
    actions = []
    user32 = SimpleNamespace(
        IsWindowVisible=lambda handle: True,
        IsIconic=lambda handle: True,
        IsZoomed=lambda handle: False,
        IsWindow=lambda handle: True,
        ShowWindow=lambda handle, state: actions.append((handle, state)),
    )
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=user32), raising=False)
    reader = lc.LiveCaptionsReader("LiveCaptions.exe")
    reader._hide_window(SimpleNamespace(NativeWindowHandle=42))
    reader._restore_windows()
    assert actions == []  # 用户自己最小化的，停止时也别替他还原


@pytest.fixture
def caption_translator(monkeypatch):
    """真实翻译队列 + 假翻译器：不加载模型、不发请求、不写用户缓存。"""
    from types import SimpleNamespace
    from realtime_subtitle import config
    from realtime_subtitle.translate import translator_queue as queue
    from realtime_subtitle.asr import backends

    def forbidden_model():
        raise AssertionError("Live Captions 不应加载 Whisper")

    monkeypatch.setattr(backends, "create_whisper_model", forbidden_model)
    monkeypatch.setattr(queue, "_select_translate_backend", lambda: (
        "apple", SimpleNamespace(close=lambda: None)))
    monkeypatch.setattr(queue.WhisperQueueTranslator, "_load_lookup_cache", lambda self: None)
    monkeypatch.setattr(queue.WhisperQueueTranslator, "_save_lookup_cache", lambda self: None)
    monkeypatch.setattr(queue.WhisperQueueTranslator, "_unload_our_models", lambda *a, **kw: None)
    monkeypatch.setattr(queue, "_warm_model", None)
    monkeypatch.setattr(config, "SAVE_TRANSCRIPT", False)
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", False)
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de")
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh")
    translator = queue.WhisperQueueTranslator(asr_source="livecaptions")
    yield translator
    translator.shutdown()


def test_caption_pipeline_uses_translation_archive_and_draft(caption_translator, monkeypatch):
    t = caption_translator
    assert not hasattr(t, "model")
    assert not hasattr(t, "processor")
    pairs, archive, displays, drafts = [], [], [], []
    t.on_pair = lambda *args: pairs.append(args)
    t.on_display = lambda *args: displays.append(args)
    t._save_transcript = lambda *args: archive.append(args)
    t._translate_single_sentence = lambda *args, **kw: "这是译文。"
    monkeypatch.setattr(t, "_maybe_draft", lambda: drafts.append(t.pending_text))
    t.enqueue_captions(["Das ist ein vollständiger Satz."], "Noch nicht fertig")
    t._asr_executor.submit(lambda: None).result(timeout=2)
    t._tx_executor.submit(lambda: None).result(timeout=2)
    assert archive == [("Das ist ein vollständiger Satz.", "这是译文。")]
    assert pairs[0][:2] == archive[0]
    assert pairs[0][4].startswith("livecaptions:")
    assert pairs[0][5] == 0
    assert displays and "Noch nicht fertig" in displays[-1][0]
    assert drafts == ["Noch nicht fertig"]
    t.request_flush()  # 没有音频 processor，定时器也应安全


def test_caption_pipeline_preserves_language_switch_and_rejects_stale_update(caption_translator, monkeypatch):
    from realtime_subtitle import config

    t = caption_translator
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de")
    monkeypatch.setattr(config, "TARGET_LANGUAGE", "zh")
    applied = []
    t.on_language_applied = lambda *args: applied.append(args)
    t.request_switch_language("en", target="zh")
    t._asr_executor.submit(lambda: None).result(timeout=2)
    assert config.SOURCE_LANGUAGE == "en"
    assert applied == [("en", "zh", 1)]
    t._process_captions(["Old language."], "old draft", 0)
    assert not t._tx_queue
    assert t.pending_text == ""
    t._process_captions([], "new draft", 1)
    assert t.pending_text == "new draft"


@pytest.mark.parametrize("available", [True, False])
def test_app_selects_caption_or_audio_source_without_models(monkeypatch, available):
    import sys
    from types import SimpleNamespace

    pytest.importorskip("PyQt6")
    monkeypatch.setenv("REALTIME_SUBTITLE_NO_SINGLETON", "1")
    from realtime_subtitle import app as app_module, config
    from realtime_subtitle.capture import live_captions
    from realtime_subtitle.translate import translator_queue

    events = []
    reader = SimpleNamespace(
        start=lambda *args: events.append("captions"), stop=lambda: None)
    monkeypatch.setattr(config, "ASR_SOURCE", "livecaptions")
    monkeypatch.setattr(live_captions, "prepare_live_captions_reader",
                        lambda *args, **kw: reader if available else None)

    def make_translator(asr_source):
        events.append(asr_source)
        return SimpleNamespace(asr_source=asr_source, enqueue_captions=lambda *a: None,
                               shutdown=lambda: None)

    monkeypatch.setattr(translator_queue, "WhisperQueueTranslator", make_translator)

    def audio_capture(**kwargs):
        assert not available, "系统字幕模式不应初始化音频采集"
        return SimpleNamespace(start=lambda: events.append("audio"), stop=lambda: None)

    monkeypatch.setitem(sys.modules, "realtime_subtitle.capture.audio_capture",
                        SimpleNamespace(AudioCapture=audio_capture))
    window = SimpleNamespace(**{name: (lambda *a: None) for name in (
        "show_status", "update_live", "add_pair", "update_draft", "on_translate_failed")})
    app = app_module.SubtitleApp.__new__(app_module.SubtitleApp)
    app.subtitle_window = window
    app._closing = False
    app.translator = None
    app.remote = None
    app.audio_capture = None
    app._attach_remote = lambda: None
    app._start_memory_tier_loop = lambda: None
    app._print_usage = lambda: None
    app._setup_hotkey = lambda: None
    app._load_models()
    assert events == (["livecaptions", "captions"] if available else ["whisper", "audio"])
    if available:
        assert app.audio_capture is None
        assert app._route_status_text() == "本机·Windows实时字幕"


def test_live_captions_does_not_open_remote_audio_bridge(monkeypatch):
    from types import SimpleNamespace

    pytest.importorskip("PyQt6")
    monkeypatch.setenv("REALTIME_SUBTITLE_NO_SINGLETON", "1")
    from realtime_subtitle import app as app_module
    from realtime_subtitle.ui import settings_window

    monkeypatch.setattr(settings_window, "cloud_active", lambda: True)
    monkeypatch.setattr(settings_window, "node_active", lambda: True)

    def forbidden_bridge(**kwargs):
        raise AssertionError("系统字幕不应启动远端识别")

    monkeypatch.setattr(app_module, "open_remote_bridge", forbidden_bridge)
    app = app_module.SubtitleApp.__new__(app_module.SubtitleApp)
    app.translator = SimpleNamespace(asr_source="livecaptions")
    app.subtitle_window = SimpleNamespace(add_pair=lambda *a: None)
    app.remote = None
    app._refresh_panel = lambda: None
    app._attach_remote()
    assert app.remote is None
    assert app.translator.on_pair is app.subtitle_window.add_pair


def test_scrolling_preserves_unfinished_sentence_prefix():
    differ = lc.CaptionDiffer(stable_seconds=0)
    assert differ.update("Ein langer Satz ohne", now=0).finalized == []
    assert differ.update("Satz ohne Punkt kommt jetzt.", now=1).finalized == [
        "Ein langer Satz ohne Punkt kommt jetzt."]
    assert differ.update("Satz ohne Punkt kommt jetzt.", now=2).finalized == []


def test_latin_abbreviations_and_email_do_not_split_sentences():
    differ = lc.CaptionDiffer()
    assert differ.update("Das ist z.B. eine E-Mail an a@b.de. Weiter", now=0) == lc.CaptionUpdate(
        ["Das ist z.B. eine E-Mail an a@b.de."], "Weiter")


@pytest.mark.parametrize("error", [ImportError("broken"), ValueError("spec absent")])
def test_broken_dependency_discovery_falls_back(monkeypatch, capsys, error):
    monkeypatch.setattr(lc, "_windows_supported", lambda: True)
    def broken(name):
        raise error
    monkeypatch.setattr(lc.importlib.util, "find_spec", broken)
    assert lc.prepare_live_captions_reader("livecaptions") is None
    assert "回退 Whisper" in capsys.readouterr().out


def test_broken_dependency_import_falls_back(monkeypatch, capsys):
    monkeypatch.setattr(lc, "_windows_supported", lambda: True)
    monkeypatch.setattr(lc.importlib.util, "find_spec", lambda name: object())
    def broken(name):
        raise ImportError("comtypes unavailable")
    monkeypatch.setattr(lc.importlib, "import_module", broken)
    assert lc.prepare_live_captions_reader("livecaptions") is None
    assert "回退 Whisper" in capsys.readouterr().out


def test_module_import_does_not_load_optional_audio_uia_or_gui_dependencies():
    result = lc.subprocess.run([
        lc.sys.executable, "-c",
        "import sys; import realtime_subtitle.capture.live_captions; "
        "assert not any(name in sys.modules for name in "
        "('soxr', 'sounddevice', 'pyaudiowpatch', 'uiautomation', 'comtypes', "
        "'realtime_subtitle.capture.audio_capture', 'PyQt6'))"
    ], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr


def test_revised_caption_clears_old_translation_and_rejects_partial(caption_translator):
    t = caption_translator
    t.pending_text = "Die alte Fassung"
    t._draft_last_text = t.pending_text
    emitted = []
    t.on_draft = lambda text, revision: emitted.append(text)
    t._maybe_draft = lambda: None

    def translate(snapshot, context, on_partial):
        t._process_captions([], "Neu", 0)
        on_partial("旧版本的草稿")
        return "旧版本的草稿"

    t._translate_single_sentence = translate
    t._draft_worker("Die alte Fassung")
    assert emitted == [""]
    assert t._draft_last_text == ""
    assert t.pending_text == "Neu"


def test_caption_translation_does_not_accumulate_undrained_asr_stats(caption_translator):
    t = caption_translator
    t._translate_single_sentence = lambda *args, **kwargs: "这是译文。"
    t.enqueue_captions(["Ein vollständiger Satz."], "")
    t._asr_executor.submit(lambda: None).result(timeout=2)
    t._tx_executor.submit(lambda: None).result(timeout=2)
    assert not t._stat_tx



def test_german_closing_quotes_and_ellipsis():
    differ = lc.CaptionDiffer(stable_seconds=1)
    assert differ.update('„Das stimmt!“ Danach… Weiter', now=0) == lc.CaptionUpdate(
        ['„Das stimmt!“', 'Danach…'], 'Weiter')
    assert differ.update('„Das stimmt!“ Danach… Weiter…', now=.1).finalized == []
    assert differ.update('„Das stimmt!“ Danach… Weiter…', now=1.2).finalized == ['Weiter…']
