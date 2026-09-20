"""语言切换请求链路：用户最后一次选择必须生效（审核 B01）。

⚠️ import app 前必须关单实例 Mutex，理由见 test_game_mode.py 文件头。

这里盯的不是"切换能不能工作"（那由 test_pipeline_helpers 覆盖），而是
**请求是在哪一层被丢掉的**：面板以前在 UI 层用 `src == config.SOURCE_LANGUAGE`
提前 return，而 config 只在后台真正应用之后才变——于是"德→点英→再点德"里
第二次点击被当成"没变化"扔掉，最终停在英语，用户最后的选择失效。
现在所有点击一律进同一个切换入口，由后台在消费时决定要不要真切。
"""
import os
from threading import Lock

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
import realtime_subtitle.app as app_mod  # noqa: E402  只 import 模块，不实例化
import realtime_subtitle.config as config  # noqa: E402
from realtime_subtitle.translate.translator_queue import (  # noqa: E402
    DecodeHealth, LanguageVote, WhisperQueueTranslator,
)


class _FakeWindow:
    """悬浮窗 + 设置面板的最小替身（自己当自己的 settings_window）。"""

    def __init__(self):
        self.settings_window = self
        self.refreshes = 0
        self.statuses = []

    def show_status(self, text):
        self.statuses.append(text)

    def refresh_from_config(self):
        self.refreshes += 1

    def notify_language_applied(self, *_):
        pass


class _RecordingTranslator:
    def __init__(self):
        self.requests = []

    def request_switch_language(self, new_lang, source="manual", target=None):
        self.requests.append((new_lang, source, target))


def _make_app(translator):
    app = app_mod.SubtitleApp.__new__(app_mod.SubtitleApp)  # 跳过 __init__
    app.subtitle_window = _FakeWindow()
    app.translator = translator
    return app


def _bare_translator():
    """只装 _apply_pending_lang_switch / _process_inbox 用到的字段。"""
    t = object.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._asr_scheduled = True  # 已"醒着"，request 不会去 submit 真线程
    t._asr_backlog_n = 0
    t._asr_error_streak = 0
    t._audio_inbox = []
    t._pending_lang_switch = None
    t._pending_lang_source = None
    t._lang_vote = LanguageVote()
    t._decode_health = DecodeHealth()
    t._lang_rescue = False
    t._lang_detect_next = 0.0
    t._lang_in_cooldown = False
    t.on_status = None
    t.on_display = None
    t.on_language_applied = None
    t.context_history = []
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._tx_epoch = 0
    t.pending_text = ""
    t._held_since = 0.0
    t._last_unstable = ""
    t._draft_last_text = ""
    t.processor = type("_P", (), {"init": lambda self: None})()
    return t


def _pin_pair(monkeypatch, source="de", target="zh"):
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", source, raising=False)
    monkeypatch.setattr(config, "TARGET_LANGUAGE", target, raising=False)


def test_last_language_choice_reaches_the_worker(monkeypatch):
    """德语 → 点英语（还没应用）→ 再点德语：后台必须收到最后那个 de。"""
    _pin_pair(monkeypatch)
    translator = _RecordingTranslator()
    app = _make_app(translator)

    app._request_language_pair("en")
    app._request_language_pair("de")  # config 仍是 de，但这是用户最后的选择

    assert [r[0] for r in translator.requests] == ["en", "de"]
    assert all(r[1] == "manual" for r in translator.requests)


def test_three_clicks_all_go_through_one_entry(monkeypatch):
    _pin_pair(monkeypatch)
    translator = _RecordingTranslator()
    app = _make_app(translator)

    for src in ("en", "de", "zh"):
        app._request_language_pair(src)

    assert [r[0] for r in translator.requests] == ["en", "de", "zh"]


def test_pending_request_carries_its_origin(monkeypatch):
    """语言和来源必须作为一个请求整体存取，否则会错配成 en+auto。"""
    _pin_pair(monkeypatch)
    t = _bare_translator()
    got = []
    t._apply_pending_lang_switch = (
        lambda lang, source=None, target=None: got.append((lang, source)) or True)

    t.request_switch_language("zh", source="auto")
    t.request_switch_language("en", source="manual")  # 覆盖尚未消费的旧请求
    t._process_inbox()

    assert got == [("en", "manual")]


def test_request_injected_during_apply_keeps_its_own_origin(monkeypatch):
    """apply 跑在锁外且很慢，这期间入队的新请求来源不许被它清掉。

    确定性交错：真实 apply 的 clear_context 执行到一半时注入一条 zh/auto。
    以前 apply 收尾会 `self._pending_lang_source = None`，清掉的正是这条新
    请求的来源，下一轮消费就把 auto 读成了 manual。不用 sleep，也不能只在
    同一把锁内连着 enqueue 两次——那覆盖不到这个窗口。
    """
    _pin_pair(monkeypatch)
    t = _bare_translator()
    seen = []
    real_apply = t._apply_pending_lang_switch

    def recording_apply(lang, source=None, target=None):
        seen.append((lang, source))
        return real_apply(lang, source=source, target=target)

    t._apply_pending_lang_switch = recording_apply

    injected = []

    def clear_context_then_inject():
        if not injected:
            injected.append(True)
            # 用户/自动检测在 apply 执行期间提交了下一条请求
            t.request_switch_language("zh", source="auto")

    t.clear_context = clear_context_then_inject
    t.request_switch_language("en", source="manual")

    t._process_inbox()

    assert seen == [("en", "manual"), ("zh", "auto")]


def test_same_pair_request_refreshes_ui_without_clearing_context(monkeypatch):
    """无变化的请求：刷 UI、走冷却，但不清上下文、不作废在飞的翻译。"""
    _pin_pair(monkeypatch)
    t = _bare_translator()
    applied = []
    t.on_language_applied = lambda src, tgt, rev=0: applied.append((src, tgt))
    t.context_history = ["旧上下文"]
    epoch = t._tx_epoch

    changed = t._apply_pending_lang_switch("de", source="manual")

    assert changed is False
    assert applied == [("de", "zh")]
    assert t.context_history == ["旧上下文"]
    assert t._tx_epoch == epoch
    assert t._lang_in_cooldown is True  # 手动选择照样进冷却，掐掉自动回切


def test_no_change_switch_keeps_buffered_audio(monkeypatch):
    """没真切语言就没有"旧语言音频"，那一批不能丢。"""
    _pin_pair(monkeypatch)
    t = _bare_translator()
    seen = []
    t._process_items = lambda items: seen.append(list(items))
    t._audio_inbox = [("chunk", 1.0)]
    t._pending_lang_switch = "de"
    t._pending_lang_source = "manual"

    t._process_inbox()

    assert seen == [[("chunk", 1.0)]]


def test_real_switch_still_discards_pre_switch_audio(monkeypatch):
    """真切语言时那一批是切换前抓的旧语言声音，必须照旧丢掉。"""
    _pin_pair(monkeypatch)
    t = _bare_translator()
    seen = []
    t._process_items = lambda items: seen.append(list(items))
    t._audio_inbox = [("chunk", 1.0)]
    t._pending_lang_switch = "zh"
    t._pending_lang_source = "manual"

    t._process_inbox()

    assert seen == []
    assert config.SOURCE_LANGUAGE == "zh"
