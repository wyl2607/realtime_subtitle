"""不加载 Whisper/Ollama 的管线辅助逻辑单测。

运行: venv\\Scripts\\python.exe -m pytest test_pipeline_helpers.py -q
  或: venv\\Scripts\\python.exe test_pipeline_helpers.py
"""
import numpy as np

import config
from streaming_asr import HypothesisBuffer, OnlineASRProcessor
from translator_queue import _SENTENCE_END, _interjection_lookup, _squash_repeats


def _extract_sentences(pending_text):
    """与 WhisperQueueTranslator._extract_sentences 相同逻辑（不实例化翻译器）"""
    sentences = []
    rest = pending_text
    while True:
        m = _SENTENCE_END.match(rest)
        if not m:
            break
        sentences.append(m.group(1).strip())
        rest = rest[m.end():].lstrip()
    return sentences, rest


def test_sentence_split_on_period():
    sents, rest = _extract_sentences("Hallo Welt. Wie geht's?")
    assert sents == ["Hallo Welt.", "Wie geht's?"]
    assert rest == ""


def test_sentence_keeps_incomplete():
    sents, rest = _extract_sentences("Das ist ein Satz. Und noch")
    assert sents == ["Das ist ein Satz."]
    assert rest == "Und noch"


def test_sentence_no_split_on_comma():
    sents, rest = _extract_sentences("Hallo, Welt und mehr")
    assert sents == []
    assert rest == "Hallo, Welt und mehr"


def test_sentence_ellipsis():
    sents, rest = _extract_sentences("Warte mal… Okay.")
    assert sents == ["Warte mal…", "Okay."]
    assert rest == ""


def test_hallucination_blacklist():
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    assert asr._is_hallucination("Untertitelung des ZDF, 2020")
    assert asr._is_hallucination("Thanks for watching this video")
    assert not asr._is_hallucination("Der Bundestag debattiert heute")
    assert not asr._is_hallucination("Meta kann das bis zu 12 Milliarden")


def test_hypothesis_local_agreement_commits_common_prefix():
    buf = HypothesisBuffer()
    # 第一次识别
    buf.insert([(0.0, 0.2, " Hallo"), (0.2, 0.5, " Welt")], 0.0)
    assert buf.flush() == []  # 首次无 buffer，不提交
    # 第二次一致前缀
    buf.insert([(0.0, 0.2, " Hallo"), (0.2, 0.5, " Welt"), (0.5, 0.8, " !")], 0.0)
    committed = buf.flush()
    words = [t for _, _, t in committed]
    assert words == [" Hallo", " Welt"]


def test_audio_chunk_flush_no_append_growth():
    """insert 多次后 process 前合并，缓冲长度正确且不依赖 np.append"""
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    asr.init()
    chunk = np.ones(1600, dtype=np.float32)  # 0.1s @ 16k
    asr.insert_audio_chunk(chunk)
    asr.insert_audio_chunk(chunk)
    assert abs(asr.buffer_seconds() - 0.2) < 1e-6
    asr._flush_audio_chunks()
    assert len(asr.audio_buffer) == 3200
    assert asr._audio_chunks == []


def _committed_words(words, step=0.5):
    return [(i * step, (i + 1) * step, " " + w) for i, w in enumerate(words)]


def test_prompt_seed_on_cold_start():
    """冷启动无已提交上下文 → prompt 用德语语言锚（防开头被英文锁死）"""
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    asr.init()
    assert config.SOURCE_LANGUAGE == "de"
    assert asr._prompt() == config.LANGUAGE_SEED_PROMPTS["de"]


def test_prompt_keeps_german_context():
    """正常德语上下文原样喂回（带补句号），不会被误判丢弃"""
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    asr.init()
    words = "ich habe das nicht gewusst und wir sind dann einfach gegangen".split()
    asr.commited = _committed_words(words)
    asr.buffer_time_offset = 100.0  # 全部已滚出缓冲
    p = asr._prompt()
    assert "gegangen" in p
    assert p != config.LANGUAGE_SEED_PROMPTS["de"]


def test_prompt_discards_english_contamination():
    """de模式下已提交文本明显是英文 → 不喂回 prompt，换语言锚打断自我强化"""
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    asr.init()
    words = "get back to the water i'm not going to have it and you are just out".split()
    asr.commited = _committed_words(words)
    asr.buffer_time_offset = 100.0
    assert asr._prompt() == config.LANGUAGE_SEED_PROMPTS["de"]


def test_prompt_neutral_exclamations_not_flagged():
    """中性感叹词（游戏音常见）不触发误锁判定"""
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    asr.init()
    words = "Whoa! Ja! Okay! Los jetzt!".split()
    asr.commited = _committed_words(words)
    asr.buffer_time_offset = 100.0
    assert asr._prompt() != config.LANGUAGE_SEED_PROMPTS["de"]


def test_interjection_dictionary_hits():
    """高频感叹词命中词典（跳过Ollama）；正常句/未收录词不命中"""
    assert config.SOURCE_LANGUAGE == "de"
    assert _interjection_lookup("Ja.") == "是"
    assert _interjection_lookup("Was?") == "什么？"
    assert _interjection_lookup("Oh mein Gott!") == "我的天哪"
    assert _interjection_lookup("Alles klar.") == "明白了"
    assert _interjection_lookup("Das ist gut.") is None  # 未收录的3词句
    assert _interjection_lookup("Ich werde sterben.") is None
    assert _interjection_lookup("") is None


def test_interjection_disabled_for_non_german():
    old = config.SOURCE_LANGUAGE
    try:
        config.SOURCE_LANGUAGE = "en"
        assert _interjection_lookup("Ja.") is None
    finally:
        config.SOURCE_LANGUAGE = old


def test_squash_repeats_word_runs():
    """句内同词连续>3次收敛到3次（"Get. Get."×6是Whisper噪音伪影）"""
    out = _squash_repeats(["ja ja ja ja ja ja genau"])
    assert out == ["ja ja ja genau"]
    # 3次以内不动（真人口语常见）
    assert _squash_repeats(["ja ja ja"]) == ["ja ja ja"]


def test_squash_repeats_duplicate_sentences():
    out = _squash_repeats(["Get.", "Get.", "Get.", "Get.", "Get.", "Get."])
    assert out == ["Get.", "Get."]
    # 不同句子完全不受影响
    keep = ["Hallo.", "Wie geht's?", "Hallo."]
    assert _squash_repeats(keep) == keep


def test_translation_worker_dict_shortcircuit_and_order():
    """worker级：队首感叹词词典直发、其余合并一次Ollama、上屏顺序不颠倒"""
    from collections import deque
    from threading import Lock
    from translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._tx_lock = Lock()
    t._stats_lock = Lock()
    t._tx_queue = ["Ja.", "Whoa!", "Das ist ein Test.", "Was?"]
    t._tx_epoch = 0
    t._tx_inflight = []
    t.closing = False
    t.context_history = deque(maxlen=6)
    t._stat_dict = 0
    t._stat_tx = []
    t._stat_draft = 0
    t._draft_last_text = ""
    t.on_draft = None
    t.on_display = None
    pairs = []
    t.on_pair = lambda g, zh: pairs.append((g, zh))
    t._save_transcript = lambda g, zh: None
    calls = []

    def fake_translate(sentence, context, on_partial=None):
        calls.append(sentence)
        return "中文翻译"
    t._translate_single_sentence = fake_translate

    class NoopExecutor:
        def submit(self, *a, **k):
            raise AssertionError("队列应该被一轮吃完，不该有leftover再调度")
    t._tx_executor = NoopExecutor()

    t._translation_worker()
    # 队首两个感叹词直发；"Was?"排在正常句之后 → 跟着batch合并翻，顺序不颠倒
    assert pairs == [("Ja.", "是"), ("Whoa!", "哇哦"),
                     ("Das ist ein Test. Was?", "中文翻译")], pairs
    assert calls == ["Das ist ein Test. Was?"]  # 只打了一次Ollama
    assert t._stat_dict == 2
    assert t._tx_queue == []

    # 全是感叹词：零Ollama调用
    pairs.clear(); calls.clear()
    t._tx_queue = ["Genau.", "Super!"]
    t._translation_worker()
    assert pairs == [("Genau.", "没错"), ("Super!", "太棒了")]
    assert calls == []


def test_collapse_word_runs_at_ingestion():
    """词流入口掐复读循环：电影场景实测"Geh!"×50刷满live行和草稿。
    保留前3个（原始时间戳），下游提交/显示/翻译全部干净"""
    words = [(i * 0.3, i * 0.3 + 0.2, " Geh!") for i in range(20)]
    out = OnlineASRProcessor._collapse_word_runs(words)
    assert len(out) == 3
    assert out == words[:3]  # 时间戳取前3个原始值（两次识别间稳定）
    # 大小写/标点变体算同一个词
    varied = [(0, 1, " Geh!"), (1, 2, " geh"), (2, 3, " GEH!"), (3, 4, " Geh,"), (4, 5, " weiter")]
    out = OnlineASRProcessor._collapse_word_runs(varied)
    assert [w[2] for w in out] == [" Geh!", " geh", " GEH!", " weiter"]
    # 3个以内的真实口语重复不动；混合句完全不受影响
    ok = [(0, 1, " ja"), (1, 2, " ja"), (2, 3, " ja"), (3, 4, " genau")]
    assert OnlineASRProcessor._collapse_word_runs(ok) == ok


def test_glossary_substring_still_config_driven():
    # 冒烟：术语表里至少有政治词条，防误删
    assert "AfD" in config.GLOSSARY
    assert "Bundestag" in config.GLOSSARY


if __name__ == "__main__":
    tests = [v for k, v in list(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in tests:
        try:
            fn()
            print(f"  OK  {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL  {fn.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    raise SystemExit(failed)


def test_shutdown_unloads_only_our_loaded_models(monkeypatch):
    """退出卸模型（_unload_our_models）：只卸/api/ps里确实加载着的本程序模型。
    ☠️ 对未加载的模型发 keep_alive=0 会先触发一次完整加载——所以"我们的模型
    没加载"时必须一次 post 都不发；用户自己跑的无关模型不能碰。"""
    from translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    posts = []

    class _Resp:
        def __init__(self, payload=None):
            self._payload = payload or {}

        def json(self):
            return self._payload

        def close(self):
            pass

    class _FakeSession:
        def __init__(self, loaded):
            self.loaded = loaded

        def get(self, url, **kw):
            assert url.endswith("/api/ps")
            return _Resp({"models": [{"name": n} for n in self.loaded]})

        def post(self, url, json=None, **kw):
            posts.append(json)
            return _Resp()

    monkeypatch.setattr(config, "OLLAMA_MODEL", "test-main")
    monkeypatch.setattr(config, "GAME_MODE_OLLAMA_MODEL", "test-game", raising=False)

    # 主模型+游戏模型+无关模型都加载着 → 只卸我们的两个，keep_alive=0
    t.ollama_session = _FakeSession(["test-main", "test-game", "someone-elses-model"])
    t._unload_our_models()
    assert sorted(p["model"] for p in posts) == ["test-game", "test-main"], posts
    assert all(p["keep_alive"] == 0 for p in posts)

    # 我们的模型都没加载 → 零 post（发了反而触发加载）
    posts.clear()
    t.ollama_session = _FakeSession(["someone-elses-model"])
    t._unload_our_models()
    assert posts == []

    # Ollama 不在 → 静默跳过不抛（不阻塞退出流程）
    class _DeadSession:
        def get(self, *a, **kw):
            raise OSError("connection refused")

    t.ollama_session = _DeadSession()
    t._unload_our_models()
