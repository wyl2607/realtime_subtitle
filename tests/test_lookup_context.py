"""F05/F06/F07：查词缓存按句境隔离、request_id 贯穿、只有完整流进缓存。"""
import json
from threading import Lock
from types import SimpleNamespace

from realtime_subtitle.translate.lookup import (
    LOOKUP_CACHE_VERSION,
    lookup_cache_key,
    normalize_lookup_context,
)
from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator


class _FakeStreamResponse:
    status_code = 200

    def __init__(self, events):
        self._events = list(events)
        self.closed = False

    def iter_lines(self):
        for event in self._events:
            if isinstance(event, bytes):
                yield event
            else:
                yield json.dumps(event).encode("utf-8")

    def close(self):
        self.closed = True


def _translator():
    t = object.__new__(WhisperQueueTranslator)
    t.closing = False
    t._lookup_inflight = False
    t._inflight_lock = Lock()
    t._ollama_hot = False
    t._lookup_seq = 0
    t._lookup_cache = __import__("collections").OrderedDict()
    t._lookup_cache_lock = Lock()
    t._LOOKUP_CACHE_MAX = 200
    return t


def test_essen_and_essen_lowercase_are_different_keys():
    assert lookup_cache_key("Essen", "de", "Ich mag Essen.") != lookup_cache_key(
        "essen", "de", "Ich mag essen.")
    assert normalize_lookup_context("  Ich   mag  Essen. ") == "Ich mag Essen."


def test_same_word_same_sentence_hits_cache():
    t = _translator()
    ctx = "Ich mag Essen."
    key = lookup_cache_key("Essen", "de", ctx)
    t._lookup_cache_put(key, "原形: das Essen\n词性: 名词\n释义: 食物")
    seen = []
    t.lookup_word("Essen", ctx, lambda w, txt, rid=None: seen.append(txt), request_id="r1")
    assert seen and "食物" in seen[0]


def test_band_different_contexts_do_not_reuse_sense():
    t = _translator()
    submitted = []

    class _Exec:
        def submit(self, fn, *a):
            submitted.append(a)

    t._lookup_executor = _Exec()
    t._lookup_cache_put(
        lookup_cache_key("Band", "de", "Die Band spielt."),
        "原形: die Band\n词性: 名词\n释义: 乐队\n本句中: 乐队在演出",
    )
    t.lookup_word("Band", "Das Band ist gerissen.", lambda *a: None, request_id="r2")
    assert submitted, "不同句境必须重新查，不能复用乐队义项"
    assert submitted[0][0] == "Band"


def test_old_cache_format_is_ignored(tmp_path, monkeypatch):
    from realtime_subtitle import config

    path = tmp_path / "lookup_cache.json"
    path.write_text(json.dumps([["haus", "de", "释义", "ctx"]], ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(config, "LOOKUP_CACHE_FILE", "lookup_cache.json", raising=False)
    monkeypatch.setattr(WhisperQueueTranslator, "_lookup_cache_path", lambda self: str(path))
    t = _translator()
    t._load_lookup_cache()
    assert len(t._lookup_cache) == 0


def test_versioned_cache_roundtrip(tmp_path, monkeypatch):
    from realtime_subtitle import config

    monkeypatch.setattr(config, "LOOKUP_CACHE_FILE", "lookup_cache.json", raising=False)
    monkeypatch.setattr(
        WhisperQueueTranslator, "_lookup_cache_path",
        lambda self: str(tmp_path / "lookup_cache.json"))
    t = _translator()
    key = lookup_cache_key("Essen", "de", "Ich mag Essen.")
    t._lookup_cache_put(key, "原形: das Essen")
    t._save_lookup_cache()
    raw = json.loads((tmp_path / "lookup_cache.json").read_text(encoding="utf-8"))
    assert raw["version"] == LOOKUP_CACHE_VERSION
    t2 = _translator()
    t2._load_lookup_cache()
    assert t2._lookup_cache == t._lookup_cache
    assert list(t2._lookup_cache)[0][0] == "Essen"


def test_corrupt_cache_does_not_block_startup(tmp_path, monkeypatch):
    path = tmp_path / "lookup_cache.json"
    path.write_text("{ 坏", encoding="utf-8")
    monkeypatch.setattr(
        WhisperQueueTranslator, "_lookup_cache_path", lambda self: str(path))
    t = _translator()
    t._load_lookup_cache()
    assert len(t._lookup_cache) == 0


def test_cache_capacity_still_capped():
    t = _translator()
    t._LOOKUP_CACHE_MAX = 3
    for i in range(5):
        t._lookup_cache_put(lookup_cache_key(f"W{i}", "de", f"c{i}"), f"释义{i}")
    assert len(t._lookup_cache) == 3


def test_ui_discards_stale_request_id_same_word():
    """旧 emit → 同词新句点击 → 执行旧信号：必须丢弃旧结果。"""
    from realtime_subtitle.ui.subtitle_render import LiveTextRenderMixin

    shown = []

    class _Popup:
        def show_at(self, *a, **k):
            shown.append(k.get("html") if "html" in k else a)

    d = SimpleNamespace()
    d._lookup_pending_id = "new-id"
    d._lookup_pending_word = "Band"
    d._lookup_context = "Das Band ist gerissen."
    d._lookup_anchor = None
    d.container = SimpleNamespace(pos=lambda: None)
    d.word_popup = _Popup()
    LiveTextRenderMixin._show_lookup(d, "Band", "乐队", False, "old-id")
    assert shown == []
    LiveTextRenderMixin._show_lookup(d, "Band", "带子", False, "new-id")
    assert shown, "当前 request_id 的结果必须显示"


def test_cache_hit_forwards_request_id():
    t = _translator()
    ctx = "Ich mag Essen."
    t._lookup_cache_put(lookup_cache_key("Essen", "de", ctx), "原形: das Essen")
    seen = []
    t.lookup_word("Essen", ctx, lambda w, txt, rid=None: seen.append(rid), request_id="rid-9")
    assert seen == ["rid-9"]


def test_complete_done_is_cached():
    t = _translator()
    text_out = []
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        {"response": "原形: essen\n", "done": False},
        {"response": "词性: 动词\n释义: 吃", "done": True},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "essen", "Wir essen Brot.", lambda w, txt, rid=None: text_out.append(txt),
        seq=0, on_partial=None, request_id="r")
    assert t._lookup_cache
    assert "吃" in text_out[0]


def test_eof_without_done_is_not_cached():
    t = _translator()
    shown = []
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        {"response": "原形: essen\n词性:", "done": False},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "essen", "Wir essen Brot.", lambda w, txt, rid=None: shown.append(txt),
        seq=0, request_id="r")
    assert len(t._lookup_cache) == 0
    assert shown and "失败" in shown[0]


def test_error_frame_is_not_cached():
    t = _translator()
    shown = []
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        {"response": "原形: x", "done": False},
        {"error": "model crashed", "done": True},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "essen", "ctx", lambda w, txt, rid=None: shown.append(txt),
        seq=0, request_id="r")
    assert len(t._lookup_cache) == 0
    assert shown and "失败" in shown[0]


def test_broken_json_then_done_still_completes():
    t = _translator()
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        b"{not json",
        {"response": "原形: das Haus\n词性: 名词\n释义: 房子", "done": True},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "Haus", "Das Haus.", lambda *a: None, seq=0, request_id="r")
    assert t._lookup_cache


def test_overlong_stream_is_not_cached(monkeypatch):
    import realtime_subtitle.translate.lookup as lookup

    monkeypatch.setattr(lookup, "_MAX_STREAM_CHARS", 8)
    t = _translator()
    shown = []
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        {"response": "abcdefghij", "done": False},
        {"response": "more", "done": True},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "Haus", "ctx", lambda w, txt, rid=None: shown.append(txt),
        seq=0, request_id="r")
    assert len(t._lookup_cache) == 0


def test_overlong_done_frame_is_not_cached(monkeypatch):
    import realtime_subtitle.translate.lookup as lookup

    monkeypatch.setattr(lookup, "_MAX_STREAM_CHARS", 8)
    t = _translator()
    shown = []
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        {"response": "123456789", "done": True},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "Haus", "ctx", lambda w, txt, rid=None: shown.append(txt),
        seq=0, request_id="r")
    assert len(t._lookup_cache) == 0
    assert shown and "失败" in shown[0]


def test_cancelled_lookup_does_not_popup_failure():
    t = _translator()
    t._lookup_seq = 2
    shown = []
    t.lookup_session = SimpleNamespace(post=lambda *a, **k: _FakeStreamResponse([
        {"response": "原形:", "done": False},
        {"response": " essen", "done": True},
    ]))
    WhisperQueueTranslator._lookup_worker(
        t, "essen", "ctx", lambda w, txt, rid=None: shown.append(txt),
        seq=1, request_id="old")
    assert shown == []
    assert len(t._lookup_cache) == 0
