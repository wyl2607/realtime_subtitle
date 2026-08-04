"""AI 分析按钮：时间窗过滤 / prompt / URL / 弹窗显示与按钮不误关。

运行: venv\\Scripts\\python.exe -m pytest test_ai_analysis.py -q

⚠️ 不 import main.py（单实例 Mutex 会 sys.exit）。
⚠️ torch 必须先于 PyQt5 加载，否则 WinError 1114。
"""
import torch  # noqa: F401  先于 PyQt5
import sys
from urllib.parse import quote, unquote, urlparse, parse_qs

sys.stdout.reconfigure(encoding="utf-8")

from PyQt5.QtWidgets import QApplication
from PyQt5.QtCore import QPoint, Qt
from PyQt5.QtTest import QTest

import config
from translator_queue import (
    filter_recent_german_context,
    build_background_summary_prompt,
    build_deep_explain_prompt,
    build_web_query_for_background,
    build_web_query_for_sentence,
    build_ai_web_url,
    AI_WEB_FRAGMENT_MAX_CHARS,
)
from popups import WordPopup, AIAnalysisPopup


_APP = None  # 必须持有引用：QApplication 没引用会被立即GC，后续建QWidget触发qFatal秒退


def _app():
    global _APP
    app = QApplication.instance()
    if app is None:
        app = QApplication(sys.argv)
    _APP = app
    return app


# ---------------------------------------------------------------------------
# 纯函数：时间窗过滤
# ---------------------------------------------------------------------------

def test_filter_recent_keeps_only_window_and_joins_german():
    now = 1_000_000.0
    pairs = [
        ("alt eins", "旧一", now - 600),   # 10 分钟前，应丢
        ("neu zwei", "新二", now - 60),    # 1 分钟前
        ("neu drei", "新三", now - 10),
    ]
    text = filter_recent_german_context(
        pairs, now=now, window_minutes=5, max_chars=2000)
    assert text == "neu zwei neu drei"
    assert "alt" not in text


def test_filter_recent_empty_when_no_pairs_in_window():
    now = 1_000_000.0
    pairs = [("nur alt", "旧", now - 9999)]
    assert filter_recent_german_context(
        pairs, now=now, window_minutes=5, max_chars=2000) == ""
    assert filter_recent_german_context([], now=now) == ""


def test_filter_recent_truncates_oldest_when_over_max_chars():
    now = 1_000_000.0
    # 三句各约 40 字符；max_chars 只够装最近两句
    a = "A" * 40
    b = "B" * 40
    c = "C" * 40
    pairs = [
        (a, "甲", now - 30),
        (b, "乙", now - 20),
        (c, "丙", now - 10),
    ]
    text = filter_recent_german_context(
        pairs, now=now, window_minutes=5, max_chars=85)
    # 应优先保留最近的；最旧的 A 被丢掉
    assert "A" not in text
    assert "B" in text and "C" in text


def test_filter_skips_legacy_two_tuples_without_timestamp():
    """防御：万一还有旧二元组，不应炸；也进不了时间窗。"""
    now = 1_000_000.0
    pairs = [
        ("legacy", "旧格式"),
        ("ok", "新", now - 5),
    ]
    text = filter_recent_german_context(
        pairs, now=now, window_minutes=5, max_chars=2000)
    assert text == "ok"


# ---------------------------------------------------------------------------
# prompt / URL
# ---------------------------------------------------------------------------

def test_background_summary_prompt_contains_source_and_no_think():
    p = build_background_summary_prompt("Das ist ein Test.")
    assert "/no_think" in p
    assert "Das ist ein Test." in p
    assert "3-5" in p or "3–5" in p


def test_deep_explain_prompt_is_richer_than_word_lookup():
    p = build_deep_explain_prompt("Das ist der absolute Wahnsinn!")
    assert "/no_think" in p
    assert "Das ist der absolute Wahnsinn!" in p
    # 比查词格式（原形/词性/释义）更展开：背景 + 俚语
    assert "俚语" in p or "双关" in p
    assert "背景" in p


def test_web_query_truncates_long_german_fragment():
    long_de = "Wort " * 200  # 远超 300
    q = build_web_query_for_sentence(long_de)
    # 问句外壳 + 截断片段；原文不应整段塞进
    assert len(long_de) > AI_WEB_FRAGMENT_MAX_CHARS
    # 截断后的片段长度上限
    assert long_de[:AI_WEB_FRAGMENT_MAX_CHARS] in q
    assert long_de[: AI_WEB_FRAGMENT_MAX_CHARS + 20] not in q


def test_build_ai_web_url_encodes_and_fills_template(monkeypatch):
    monkeypatch.setattr(
        config, "AI_ANALYSIS_WEB_URL_TEMPLATE",
        "https://example.test/ai?q={query}")
    prompt = "请帮我解释这句德语的背景：「Hallo Welt」"
    url = build_ai_web_url(prompt)
    assert url.startswith("https://example.test/ai?q=")
    parsed = urlparse(url)
    qs = parse_qs(parsed.query)
    assert unquote(qs["q"][0]) == prompt
    # quote 后不应残留未编码空格
    assert " " not in url.split("q=", 1)[1]
    assert quote(prompt, safe="") in url


def test_build_web_query_for_background_mentions_live():
    q = build_web_query_for_background("Guten Tag zusammen")
    assert "Guten Tag zusammen" in q
    assert "背景" in q


# ---------------------------------------------------------------------------
# 弹窗 UI
# ---------------------------------------------------------------------------

def test_ai_analysis_popup_show_and_hide():
    app = _app()
    popup = AIAnalysisPopup()
    screen = QApplication.primaryScreen()
    assert screen is not None
    area = screen.availableGeometry()
    pos = QPoint(area.center().x(), area.center().y())

    popup.show_at(pos, "🤖 测试总结内容", timeout_ms=500,
                  show_web=True, web_query="test query")
    app.processEvents()
    assert popup.isVisible()
    assert popup.web_btn.isVisible()
    assert not popup.deep_btn.isVisible()

    popup.hide()
    app.processEvents()
    assert not popup.isVisible()


def test_word_popup_shows_deep_button_after_lookup_mode():
    app = _app()
    popup = WordPopup()
    screen = QApplication.primaryScreen().availableGeometry()
    pos = QPoint(screen.center().x(), screen.center().y())
    popup.show_at(
        pos, "📖 <b>Haus</b><br>释义: 房子",
        show_deep=True, show_web=False, context="Das ist ein Haus.")
    app.processEvents()
    assert popup.isVisible()
    assert popup.deep_btn.isVisible()
    assert not popup.web_btn.isVisible()
    popup.hide()


def test_word_popup_button_click_does_not_hide_blank_does():
    """最容易埋坑：点操作按钮不能把整个弹窗 hide；点空白/标签才关。"""
    app = _app()
    popup = WordPopup()
    screen = QApplication.primaryScreen().availableGeometry()
    pos = QPoint(screen.center().x(), screen.center().y())
    deep_clicks = []
    web_clicks = []
    popup.on_deep_explain = lambda ctx: deep_clicks.append(ctx)
    popup.on_open_web = lambda q: web_clicks.append(q)

    popup.show_at(
        pos, "📖 查词结果<br>一行释义",
        timeout_ms=60000,
        show_deep=True,
        show_web=True,
        context="Kontextsatz.",
        web_query="web-q",
    )
    app.processEvents()
    assert popup.isVisible()

    # 点深度解释：弹窗仍在，回调收到 context
    QTest.mouseClick(popup.deep_btn, Qt.LeftButton)
    app.processEvents()
    assert popup.isVisible(), "点深度解释不应关闭弹窗"
    assert deep_clicks == ["Kontextsatz."]

    # 点问更强的AI：同样不关
    QTest.mouseClick(popup.web_btn, Qt.LeftButton)
    app.processEvents()
    assert popup.isVisible(), "点网页按钮不应关闭弹窗"
    assert web_clicks == ["web-q"]

    # 点空白（父级 mousePressEvent）：应隐藏
    # QLabel 默认不处理按钮，事件会到父级；直接调 mousePressEvent 更稳
    popup.mousePressEvent(None)
    app.processEvents()
    assert not popup.isVisible()


def test_update_content_restarts_hide_timer():
    """内容从「分析中」变成结果时必须重新 start 计时器（间接：update 后仍可见）。

    「分析中」态已 show_web=True（用户可提前跳网页），update 后仍保持可见。
    """
    app = _app()
    popup = AIAnalysisPopup()
    screen = QApplication.primaryScreen().availableGeometry()
    pos = QPoint(screen.center().x(), screen.center().y())
    popup.show_at(
        pos, "分析中…", timeout_ms=60000, show_web=True, web_query="q")
    app.processEvents()
    assert popup.isVisible()
    assert popup.web_btn.isVisible(), "分析中就该能点「问更强的AI」"
    popup.update_content(
        "最终结果", show_web=True, web_query="q", timeout_ms=60000)
    app.processEvents()
    assert popup.isVisible()
    assert popup.web_btn.isVisible()
    assert "最终结果" in popup.label.text()
    popup.hide()


def test_analyze_background_and_deep_explain_use_lookup_executor():
    """两个新方法必须 submit 到 _lookup_executor，不新建池、不碰 _tx_executor。"""
    from translator_queue import WhisperQueueTranslator
    # 不真正构造完整 translator（会加载模型）；只测方法绑定 + submit 目标
    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    submitted = []

    class _Exec:
        def submit(self, fn, *a):
            submitted.append((fn, a))

    t._lookup_executor = _Exec()
    t.analyze_background = WhisperQueueTranslator.analyze_background.__get__(t)
    t.deep_explain = WhisperQueueTranslator.deep_explain.__get__(t)
    t._analyze_background_worker = (
        WhisperQueueTranslator._analyze_background_worker.__get__(t))
    t._deep_explain_worker = (
        WhisperQueueTranslator._deep_explain_worker.__get__(t))

    cb = lambda *a: None
    t.analyze_background("de text", cb)
    t.deep_explain("ein satz", cb)
    assert len(submitted) == 2
    assert submitted[0][0] == t._analyze_background_worker
    assert submitted[1][0] == t._deep_explain_worker
    assert submitted[0][1][0] == "de text"
    assert submitted[1][1][0] == "ein satz"


# ---------------------------------------------------------------------------
# GPU 让路：查词/AI分析在飞时草稿翻译应跳过
#
# 根因（2026-08-04 实测）：直播翻译流从不真正闲着，查词/AI分析这类一次性
# 人工请求跟它抢同一张卡的 Ollama 推理算力，会从 <1 秒拖到 8-15 秒。
# _lookup_inflight 标志让 _maybe_draft 在查词/分析请求飞行期间跳过草稿，
# 把 GPU 让给用户主动发起的请求。
# ---------------------------------------------------------------------------

def _draft_ready_translator():
    """构造一个满足 _maybe_draft 除 _lookup_inflight 外全部"应该出草稿"条件的假 translator。"""
    from translator_queue import WhisperQueueTranslator
    from threading import Lock
    t = object.__new__(WhisperQueueTranslator)
    t.on_draft = lambda *a: None
    t.pending_text = "Das ist ein laengerer Satz mit genug Woertern"
    t._draft_last_text = ""  # 和 pending_text 不同，不会因"没变"提前 return
    t._draft_last_time = 0.0  # 足够久之前，不会被 DRAFT_MIN_INTERVAL 拦
    t._asr_busy = False
    t._lookup_inflight = False
    t._ollama_hot = True
    t._asr_lock = Lock()
    t._audio_inbox = []
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._stats_lock = Lock()
    t._stat_draft = 0

    submitted = []

    class _Exec:
        def submit(self, fn, *a):
            submitted.append((fn, a))

    t._tx_executor = _Exec()
    t._submitted = submitted
    return t


def test_maybe_draft_skips_when_lookup_inflight(monkeypatch):
    import config
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True)
    from translator_queue import WhisperQueueTranslator

    t = _draft_ready_translator()
    t._lookup_inflight = True
    WhisperQueueTranslator._maybe_draft(t)
    assert t._submitted == [], "查词/分析在飞时草稿不该提交，会跟它抢GPU"


def test_maybe_draft_runs_when_lookup_not_inflight(monkeypatch):
    """对照组：其它条件不变，只把 _lookup_inflight 改成 False，草稿应该正常提交。

    证明上一个测试真的是被 _lookup_inflight 挡住的，不是被其它前置条件挡住的。
    """
    import config
    monkeypatch.setattr(config, "DRAFT_TRANSLATION", True)
    from translator_queue import WhisperQueueTranslator

    t = _draft_ready_translator()
    assert t._lookup_inflight is False
    WhisperQueueTranslator._maybe_draft(t)
    assert len(t._submitted) == 1


def test_lookup_worker_sets_and_clears_inflight_flag():
    """_lookup_worker 请求期间 _lookup_inflight 应为 True，结束后必须清回 False。"""
    from translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    t._lookup_inflight = False
    t._lookup_seq = 1
    t._lookup_cache = {}
    t._lookup_cache_lock = __import__("threading").Lock()
    t._LOOKUP_CACHE_MAX = 200
    seen_inflight_during_call = []

    class _FakeResponse:
        status_code = 200
        def json(self):
            return {"response": "结果"}

    class _FakeSession:
        def post(self, *a, **k):
            seen_inflight_during_call.append(t._lookup_inflight)
            return _FakeResponse()

    t.lookup_session = _FakeSession()
    results = []
    WhisperQueueTranslator._lookup_worker(
        t, "Wort", "ein Kontext", lambda w, txt: results.append((w, txt)), seq=1)

    assert seen_inflight_during_call == [True], "请求发出时 _lookup_inflight 应为 True"
    assert t._lookup_inflight is False, "请求结束后必须清回 False，否则草稿永远让路"
    assert results == [("Wort", "结果")]


def test_lookup_worker_uses_reduced_num_predict():
    """算力收敛（2026-08-04实测）：查词 num_predict 220→170。"""
    from translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    t._lookup_inflight = False
    t._lookup_seq = 1
    t._lookup_cache = {}
    t._lookup_cache_lock = __import__("threading").Lock()
    t._LOOKUP_CACHE_MAX = 200
    posted = []

    class _FakeResponse:
        status_code = 200
        def json(self):
            return {"response": "结果"}

    class _FakeSession:
        def post(self, url, json, timeout):
            posted.append(json)
            return _FakeResponse()

    t.lookup_session = _FakeSession()
    WhisperQueueTranslator._lookup_worker(
        t, "Wort", "ein Kontext", lambda w, txt: None, seq=1)

    assert posted[0]["options"]["num_predict"] == 170


def test_run_ai_analysis_request_sets_and_clears_inflight_flag_even_on_failure():
    """failure 路径（HTTP非200/异常）也必须清 flag，否则一次失败就把草稿卡死。"""
    from translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    t._lookup_inflight = False

    class _FakeSession:
        def post(self, *a, **k):
            raise RuntimeError("网络挂了")

    t.lookup_session = _FakeSession()
    results = []
    WhisperQueueTranslator._run_ai_analysis_request(
        t, "prompt", lambda txt: results.append(txt), num_predict=400, label="测试")

    assert t._lookup_inflight is False, "异常路径也必须清 flag（finally块）"
    assert results and "失败" in results[0]


# ---------------------------------------------------------------------------
# 代数门控：背景总结 / 深度解释只认最新一次请求
#
# 与查词 _lookup_seq 同思路，但计数器在 UI 侧（subtitle_window）：
# 连点 🤖 / 深度解释时递增；_show_* 发现 seq 过时则丢弃，不更新弹窗。
# ---------------------------------------------------------------------------

def _ai_gate_stub():
    """轻量 stub：绑定真实 _show_* / show_*_result，假 popup 记更新。"""
    from subtitle_window import SubtitleWindow, SubtitleSignals

    class _Popup:
        def __init__(self):
            self.texts = []
            self._context = ""
            self.kwargs = []

        def update_content(self, html_text, **kwargs):
            self.texts.append(html_text)
            self.kwargs.append(kwargs)
            if kwargs.get("context") is not None:
                self._context = kwargs["context"]

        def show_at(self, _pos, html_text, **kwargs):
            self.texts.append(html_text)
            self.kwargs.append(kwargs)

    w = object.__new__(SubtitleWindow)
    w._ai_analysis_seq = 0
    w._deep_explain_seq = 0
    w._ai_context_text = "Hallo Welt"
    w.ai_analysis_popup = _Popup()
    w.word_popup = _Popup()
    w.signals = SubtitleSignals()
    w._show_ai_analysis = SubtitleWindow._show_ai_analysis.__get__(w)
    w._show_deep_explain = SubtitleWindow._show_deep_explain.__get__(w)
    w.show_ai_analysis_result = SubtitleWindow.show_ai_analysis_result.__get__(w)
    w.show_deep_explain_result = SubtitleWindow.show_deep_explain_result.__get__(w)
    w.signals.ai_analysis.connect(w._show_ai_analysis)
    w.signals.deep_explain.connect(w._show_deep_explain)
    return w


def test_show_ai_analysis_discards_stale_seq():
    """旧代数结果不更新弹窗；当前代数正常写入。"""
    app = _app()
    w = _ai_gate_stub()
    w._ai_analysis_seq = 2

    w._show_ai_analysis("过时总结", 1)
    assert w.ai_analysis_popup.texts == [], "旧 seq 必须丢弃"

    w._show_ai_analysis("最新总结", 2)
    assert len(w.ai_analysis_popup.texts) == 1
    assert "最新总结" in w.ai_analysis_popup.texts[0]


def test_show_deep_explain_discards_stale_seq():
    app = _app()
    w = _ai_gate_stub()
    w._deep_explain_seq = 3
    w.word_popup._context = "Das ist ein Satz."

    w._show_deep_explain("旧解释", 1)
    assert w.word_popup.texts == []

    w._show_deep_explain("新解释", 3)
    assert len(w.word_popup.texts) == 1
    assert "新解释" in w.word_popup.texts[0]


def test_result_callbacks_carry_seq_through_signal():
    """worker 经 show_*_result → 信号 → _show_* 整条链保留 seq。"""
    app = _app()
    w = _ai_gate_stub()
    w._ai_analysis_seq = 5
    w._deep_explain_seq = 7

    # 模拟过时回调（用户已连点，seq 落后）
    w.show_ai_analysis_result("旧背景", 4)
    app.processEvents()
    assert w.ai_analysis_popup.texts == []

    w.show_ai_analysis_result("新背景", 5)
    app.processEvents()
    assert len(w.ai_analysis_popup.texts) == 1
    assert "新背景" in w.ai_analysis_popup.texts[0]

    w.show_deep_explain_result("旧深度", 6)
    app.processEvents()
    assert w.word_popup.texts == []

    w.show_deep_explain_result("新深度", 7)
    app.processEvents()
    assert len(w.word_popup.texts) == 1
    assert "新深度" in w.word_popup.texts[0]


def test_on_ai_analysis_clicked_bumps_seq_and_shows_web_while_loading():
    """发起背景总结：seq+1、分析中 show_web=True、回调带捕获的 seq。"""
    import time
    from PyQt5.QtCore import QRect, QPoint
    from subtitle_window import SubtitleWindow

    app = _app()
    w = _ai_gate_stub()
    w.sentence_pairs = [
        ("Das ist ein laengerer Satz.", "这是一句较长的话。", time.time()),
    ]

    class _Container:
        def frameGeometry(self):
            return QRect(100, 100, 400, 200)

    w.container = _Container()
    w._on_ai_analysis_clicked = SubtitleWindow._on_ai_analysis_clicked.__get__(w)

    captured = []

    def _fake_analyze(text, cb):
        captured.append((text, cb))

    w.on_ai_analysis = _fake_analyze
    w._on_ai_analysis_clicked()
    app.processEvents()

    assert w._ai_analysis_seq == 1
    assert captured, "应提交分析请求"
    assert w.ai_analysis_popup.texts, "应显示分析中"
    assert "分析" in w.ai_analysis_popup.texts[-1]
    last_kw = w.ai_analysis_popup.kwargs[-1]
    assert last_kw.get("show_web") is True, "分析中就该露出网页链接"
    assert last_kw.get("web_query"), "web_query 应已算好"

    # 回调应绑定 seq=1；若期间又点一次，旧回调不生效
    w._ai_analysis_seq = 2
    captured[0][1]("不该显示的旧结果")
    app.processEvents()
    assert all("不该显示" not in t for t in w.ai_analysis_popup.texts)


def test_on_word_deep_explain_bumps_seq_and_shows_web_while_loading():
    from subtitle_window import SubtitleWindow

    app = _app()
    w = _ai_gate_stub()
    w._on_word_deep_explain = SubtitleWindow._on_word_deep_explain.__get__(w)

    captured = []
    w.on_deep_explain = lambda sentence, cb: captured.append((sentence, cb))

    w._on_word_deep_explain("Das ist der absolute Wahnsinn!")
    app.processEvents()

    assert w._deep_explain_seq == 1
    assert captured and captured[0][0].startswith("Das ist")
    last_kw = w.word_popup.kwargs[-1]
    assert last_kw.get("show_web") is True
    assert last_kw.get("web_query")

    w._deep_explain_seq = 9  # 模拟又点了别的词 / 新深度解释
    captured[0][1]("过时深度")
    app.processEvents()
    assert all("过时深度" not in t for t in w.word_popup.texts)


def test_ai_workers_use_reduced_num_predict():
    """算力收敛：背景总结 300、深度解释 400（不再用 400/600）。"""
    from translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    seen = []

    def _fake_run(prompt, callback, num_predict=400, label="分析"):
        seen.append((label, num_predict))

    t._run_ai_analysis_request = _fake_run
    WhisperQueueTranslator._analyze_background_worker(t, "de", lambda *a: None)
    WhisperQueueTranslator._deep_explain_worker(t, "satz", lambda *a: None)
    assert seen == [("背景总结", 300), ("深度解释", 400)]


def test_config_ai_context_max_chars_reduced():
    assert config.AI_CONTEXT_MAX_CHARS == 1400
