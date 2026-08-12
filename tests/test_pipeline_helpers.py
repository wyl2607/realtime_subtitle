"""不加载 Whisper/Ollama 的管线辅助逻辑单测。

运行: venv\\Scripts\\python.exe -m pytest test_pipeline_helpers.py -q
  或: venv\\Scripts\\python.exe test_pipeline_helpers.py
"""
import re

import numpy as np
import pytest

import realtime_subtitle.config as config
from realtime_subtitle.asr.streaming_asr import HypothesisBuffer, OnlineASRProcessor
from realtime_subtitle.translate.translator_queue import (
    _interjection_lookup, _split_sentences, _squash_repeats,
)


def _extract_sentences(pending_text, final=True):
    """直接调真实切分函数（以前这里复制过一份逻辑，复制必然和实现漂移）。
    默认 final=True：这几条老用例测的是"切分规则本身"，不测句尾扣留。"""
    return _split_sentences(pending_text, final=final)


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


def test_sentence_merges_lowercase_continuation():
    """☠️ 最大的效果问题：38.5% 的翻译单元以小写词开头 = 上一句被切开了。
    德语句首必大写，所以句号后面跟小写词 = Whisper 打错了标点。"""
    sents, rest = _extract_sentences("Mindestens 67 sind bei dem Versuch. gestorben. Danach kam er.")
    assert sents == ["Mindestens 67 sind bei dem Versuch. gestorben.", "Danach kam er."]
    assert rest == ""
    # 实测那句：否定词在下一段，切开会翻反
    sents, _ = _extract_sentences("Demnach darf, wer schwimmend bzw. über den Seeweg eintrifft.")
    assert len(sents) == 1


def test_sentence_no_split_on_abbreviation():
    """德语缩写自带句号：bzw. / z.B. / ca. 后面即使是大写词也不是句尾。"""
    sents, rest = _extract_sentences("Er kommt bzw. Sie kommt auch. Dann gehen wir.")
    assert sents == ["Er kommt bzw. Sie kommt auch.", "Dann gehen wir."]
    assert rest == ""


def test_sentence_no_split_on_ordinal_number():
    """"am 3. Mai" —— 后面的 Mai 是大写（德语名词），只有数字规则能拦住。"""
    sents, rest = _extract_sentences("Am 3. Mai fahren wir los. Es regnet.")
    assert sents == ["Am 3. Mai fahren wir los.", "Es regnet."]
    assert rest == ""


def test_sentence_splits_after_year():
    """☠️ 四位年份结尾必须能成句。

    以前数字否决是无条件的（`token.isdigit()`），"…bis 2030." 这类句子
    **永远**不成句：那条 return 在 `if not remainder: return final` 之前，
    连收尾放行都救不回来，只能等 IDLE_FLUSH_SEC 或和下一句合并，中文晚一句。
    新闻场景（本项目主场景）年份/金额结尾极常见。
    """
    sents, rest = _extract_sentences("Der Vertrag läuft bis 2030. Alles andere ist offen.")
    assert sents == ["Der Vertrag läuft bis 2030.", "Alles andere ist offen."]
    assert rest == ""

    # 句尾正好在文本末尾时也要能被 final=True 放行（扣留兜底那条路径）
    sents, rest = _split_sentences("Das war im Jahr 1998.", final=True)
    assert sents == ["Das war im Jahr 1998."]
    assert rest == ""

    # final=False 仍照常扣留（看不到下一个词，规则③还没法判）
    sents, rest = _split_sentences("Das war im Jahr 1998.", final=False)
    assert sents == []
    assert rest == "Das war im Jahr 1998."


def test_sentence_boundary_held_until_next_word():
    """句尾正好在文本末尾时：final=False 扣留（还不知道下个词是大是小写），
    final=True（收尾/有界放行）照常成句。"""
    sents, rest = _split_sentences("Das ist ein Satz.", final=False)
    assert sents == []
    assert rest == "Das ist ein Satz."

    sents, rest = _split_sentences("Das ist ein Satz.", final=True)
    assert sents == ["Das ist ein Satz."]
    assert rest == ""

    # 已经能看见下一个词（大写）→ 不用等，立刻成句
    sents, rest = _split_sentences("Das ist ein Satz. Und", final=False)
    assert sents == ["Das ist ein Satz."]
    assert rest == "Und"


def test_normalize_clock_times():
    """德语 'HH.MM Uhr' 的点会被模型当小数点/序数点读歪，送翻译前归一化成冒号。

    2026-08-04 复核 23157 条真实句对实测到的三种错法：
      19.10 Uhr → "晚上九点"（小时算错）
      23.30 Uhr → "十一点"（丢了分钟）
      21 .43 Uhr → "0点43分"（ASR 在数字间插空格，模型彻底读歪）
    """
    from realtime_subtitle.translate.translator_queue import _normalize_clock_times as n
    assert n("Sonntag, 19.10 Uhr.") == "Sonntag, 19:10 Uhr."
    assert n("Heute 19 .25 Uhr im ZDF.") == "Heute 19:25 Uhr im ZDF."
    assert n("Und der andere war am 21 .43 Uhr.") == "Und der andere war am 21:43 Uhr."
    assert n("am 21. .31 Uhr") == "am 21:31 Uhr"      # ASR 多打一个点
    assert n("Donnerstag, 20.15 Uhr im ZDF.") == "Donnerstag, 20:15 Uhr im ZDF."
    assert n("Morgen früh, 6.30 Uhr.") == "Morgen früh, 6:30 Uhr."
    # 不是时间的点不能动
    assert n("Am 3. Mai fahren wir los.") == "Am 3. Mai fahren wir los."
    assert n("ab 1914 europa und die welt.") == "ab 1914 europa und die welt."
    assert n("Das kostet 8.50 Euro.") == "Das kostet 8.50 Euro."   # 后面不是 Uhr
    assert n("25.99 Uhr") == "25.99 Uhr"   # 25 点不存在，不当时间处理
    assert n("") == "" and n(None) is None


def test_strip_translator_note_inline_and_trailing():
    """译注不只出现在末尾：真实数据里有夹在句子中间的。

    实测例（2026-07 转录）：「多特蒙德（注：此处应为杜塞尔多夫）住过、干过活儿」
    """
    from realtime_subtitle.translate.translator_queue import _strip_translator_note as s
    # 中间的（括号闭合）
    assert s("多特蒙德（注：此处应为杜塞尔多夫）住过、干过活儿") == "多特蒙德住过、干过活儿"
    # 末尾的，右括号被 num_predict 截断
    assert s("美国总统对伊朗……（注：建议补全后半句") == "美国总统对伊朗……"
    # 末尾的，完整
    assert s("估价在90到120欧元之间。（译注：此处保留人名）") == "估价在90到120欧元之间。"
    # 一条里两种都有
    assert s("甲（注：a）乙（说明：b）") == "甲乙"
    # 整条都是译注 → 原样返回，宁可多显示不可显示空白
    assert s("（注：这整条都是注）") == "（注：这整条都是注）"
    # 正常括号不能误伤
    assert s("他（35岁）来自科隆。") == "他（35岁）来自科隆。"


def test_version_is_semver_and_string_helper_matches():
    """版本号格式固定成 主.次.修，version_string() 只在前面加个 v。
    update_subtitles.ps1 和 issue 模板都按这个格式正则匹配。"""
    from realtime_subtitle import version
    assert re.fullmatch(r"\d+\.\d+\.\d+", version.__version__), \
        f"版本号要用语义化版本 主.次.修，现在是 {version.__version__!r}"
    assert version.version_string() == "v" + version.__version__
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", version.__version_date__)


def test_version_module_stays_import_free():
    """☠️ version.py 必须是纯常量、零 import。

    update_subtitles.ps1 用 `Select-String -Pattern '^__version__\\s*=\\s*"..."'`
    直接正则读这个文件（这样 venv 坏掉/还没建的时候也能拿到版本号），
    issue 模板同理。往里加 import 不会让 Python 报错，但会让"版本号是单一
    真相源"这件事悄悄依赖上一个能跑的 venv。
    """
    import pathlib
    # 重构把 version.py 挪进了包目录，测试文件也挪进了 tests/：
    # with_name("version.py") 会指到 tests\version.py（不存在）。按包定位，
    # 别写死相对层级——update_subtitles.ps1 / issue 模板读的就是这个文件
    src = (pathlib.Path(__file__).resolve().parents[1]
           / "realtime_subtitle" / "version.py").read_text(encoding="utf-8")
    code_lines = []
    in_doc = False
    for raw in src.splitlines():
        line = raw.strip()
        if line.startswith('"""'):
            # 单行 docstring（开头结尾都在这一行）不切换状态
            if not (len(line) > 3 and line.endswith('"""')):
                in_doc = not in_doc
            continue
        if in_doc or not line or line.startswith("#"):
            continue
        code_lines.append(line)

    offenders = [ln for ln in code_lines
                 if ln.startswith("import ") or ln.startswith("from ")]
    assert not offenders, f"version.py 不能有 import：{offenders}"

    # 正则本身也要能匹配上——这是 PowerShell 那边用的同一个式子
    assert re.search(r'^__version__\s*=\s*"([^"]+)"', src, re.MULTILINE)


def test_hallucination_blacklist():
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    assert asr._is_hallucination("Untertitelung des ZDF, 2020")
    assert asr._is_hallucination("Thanks for watching this video")
    assert not asr._is_hallucination("Der Bundestag debattiert heute")
    assert not asr._is_hallucination("Meta kann das bis zu 12 Milliarden")


def test_hallucination_blacklist_does_not_eat_real_speech():
    """☠️ 命中即整段丢弃，所以只做子串匹配会误杀真人说话。

    "Copyright" 在德语媒体报道里是常见外来词，"Untertitel" 在讨论无障碍/
    流媒体的节目里也会正常出现。以前只要段里出现这些子串，整个 segment
    （可能好几秒的话）就被静默丢掉——屏幕上只是缺了一句，用户完全无感，
    日志里也只有 SHOW_PERFORMANCE 打开时才看得见。

    幻觉本身都是短固定套话，所以加长度门；短句照杀，长句放行。
    """
    asr = OnlineASRProcessor.__new__(OnlineASRProcessor)
    # 真人在讲版权/字幕话题的长句：必须放行
    assert not asr._is_hallucination(
        "Der Streit um das Copyright bei generativen Modellen beschäftigt "
        "inzwischen mehrere Gerichte in Europa.")
    assert not asr._is_hallucination(
        "Die Mediathek bietet inzwischen für fast alle Sendungen Untertitel "
        "an, auch bei Live-Übertragungen im Ersten.")
    # 经典幻觉套话（短）：照杀不误
    assert asr._is_hallucination("Untertitelung des ZDF, 2020")
    assert asr._is_hallucination("Copyright WDR 2021")
    assert asr._is_hallucination("Untertitel von Amara.org")
    assert not asr._is_hallucination("")


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
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

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
    t.pending_text = ""
    t._last_unstable = ""
    t.on_draft = None
    displays = []
    t.on_display = lambda c, u: displays.append((c, u))
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

    # 全是感叹词：零Ollama调用，且必须 emit live（否则 live 残留同一句德语）
    pairs.clear(); calls.clear(); displays.clear()
    t._tx_queue = ["Genau.", "Super!"]
    t._translation_worker()
    assert pairs == [("Genau.", "没错"), ("Super!", "太棒了")]
    assert calls == []
    assert len(displays) >= 1, "纯词典直译后应 _emit_display 清掉 live 残留"


def test_transcript_keeps_source_when_translation_failed():
    """☠️ 翻译失败（返回原文）时德语原文必须照常入档。

    以前是 `if translation != german: _save_transcript(...)`，于是 Ollama
    挂掉/熔断的那几分钟里 transcripts\\ 一条都不写——而存档的用途正是事后
    回看和学德语，用户完全无从知道那段说了什么。屏幕上只显示一遍德语
    （不重复两行）是另一回事，两者不能共用同一个判断。
    """
    from collections import deque
    from threading import Lock
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._tx_lock = Lock()
    t._stats_lock = Lock()
    t._tx_queue = ["Der Bundestag debattiert heute."]
    t._tx_epoch = 0
    t._tx_inflight = []
    t.closing = False
    t.context_history = deque(maxlen=6)
    t._stat_dict = 0
    t._stat_tx = []
    t._draft_last_text = ""
    t.pending_text = ""
    t._last_unstable = ""
    t.on_draft = None
    t.on_display = lambda c, u: None
    pairs = []
    t.on_pair = lambda g, zh: pairs.append((g, zh))
    saved = []
    t._save_transcript = lambda g, zh: saved.append((g, zh))
    # 翻译失败：_translate_single_sentence 的约定是原样返回原文
    t._translate_single_sentence = lambda s, ctx, on_partial=None: s

    class NoopExecutor:
        def submit(self, *a, **k):
            pass
    t._tx_executor = NoopExecutor()

    t._translation_worker()
    assert saved == [("Der Bundestag debattiert heute.", "")], saved
    # 屏幕上仍然只显示一遍德语（中文为空），不能变成"德语\n德语"
    assert pairs == [("Der Bundestag debattiert heute.", "")], pairs


def test_save_transcript_omits_empty_translation_line(tmp_path):
    """中文为空时只写原文行，不留一行空白译文（回看时看得出"当时没翻出来"）。"""
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._transcript_ok = True
    t._transcript_dir = str(tmp_path)
    t._save_transcript("Hallo Welt.", "你好世界")
    t._save_transcript("Nur Deutsch.", "")

    written = next(tmp_path.iterdir()).read_text(encoding="utf-8")
    assert "你好世界" in written
    assert "Nur Deutsch." in written
    # 空译文不该留下只有空格的一行
    assert not any(line.strip() == "" and line != "" for line in written.splitlines())


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


def test_lookup_only_latest_click_wins():
    """精听时连点多个词：单线程 worker 排队，过时结果回来会覆盖当前弹窗。
    只有最后一次点击的结果能上屏（排在前面的连请求都不发）。"""
    from threading import Lock

    t = _translator_for_tx()
    t._lookup_cache = __import__("collections").OrderedDict()
    t._lookup_cache_lock = Lock()
    t._LOOKUP_CACHE_MAX = 200
    t._lookup_seq = 0
    submitted = []

    class _Exec:
        def submit(self, fn, *a):
            submitted.append(a)
    t._lookup_executor = _Exec()

    shown = []
    cb = lambda w, txt: shown.append(w)
    t.lookup_word("Haus", "ctx1", cb)
    t.lookup_word("Baum", "ctx2", cb)
    t.lookup_word("Auto", "ctx3", cb)
    assert [a[0] for a in submitted] == ["Haus", "Baum", "Auto"]

    posts = []

    class _Session:
        def post(self, *a, **kw):
            posts.append(1)
            raise AssertionError("过时的查词不该发请求")
    t.lookup_session = _Session()

    # 前两次已经过时（seq 1、2 < 当前 3）：直接返回，不发请求不回调
    t._lookup_worker(*submitted[0])
    t._lookup_worker(*submitted[1])
    assert shown == [] and posts == []

    # 最后一次是当前的：正常走下去（这里让它走异常分支，只验证会回调）
    t._lookup_worker(*submitted[2])
    assert shown == ["Auto"]


def test_asr_corrections_are_deterministic(monkeypatch):
    """专有名词纠错：保留时间戳/前导空格/尾随标点，且必须确定性——
    相邻两次识别得到相同结果，local agreement 的前缀判定才不受扰动。"""
    monkeypatch.setattr(config, "ASR_CORRECTIONS",
                        {"theuta": "Ceuta", "hubschreiber": "Hubschrauber"},
                        raising=False)
    words = [(0.0, 0.3, " In"), (0.3, 0.8, " Theuta,"), (0.8, 1.2, " ein"),
             (1.2, 1.9, " Hubschreiber."), (1.9, 2.2, " ok")]
    out = OnlineASRProcessor._apply_corrections(words)

    assert [w[2] for w in out] == [" In", " Ceuta,", " ein", " Hubschrauber.", " ok"]
    assert [(w[0], w[1]) for w in out] == [(w[0], w[1]) for w in words]  # 时间戳不动
    # 确定性：再跑一遍完全一致（已修正的词不会被二次改写）
    assert OnlineASRProcessor._apply_corrections(out) == out

    # 没有词表时原样返回
    monkeypatch.setattr(config, "ASR_CORRECTIONS", {}, raising=False)
    assert OnlineASRProcessor._apply_corrections(words) == words


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


def test_lang_switch_pending_preempts_before_audio_batch():
    """语言切换标志在 inbox 每批边界抢占：即使收件箱非空也能切，不饿死。"""
    from threading import Lock
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._audio_inbox = [("fake_audio", 0.0)]
    t._asr_scheduled = True
    t._pending_lang_switch = "en"
    t._asr_error_streak = 0
    applied = []
    processed = []

    def apply(lang):
        applied.append(lang)
        # 模拟 clear 后不依赖真实 processor
    t._apply_pending_lang_switch = apply
    t._process_items = lambda items: processed.append(len(items))

    # 第一轮：有 pending lang + 一块音频 → 切语言，并【丢弃】这批切换前的音频
    # （用新语言参数识别旧语言音频会蹦乱词；clear_context 已经把缓冲丢了）
    t._process_inbox()
    assert applied == ["en"], applied
    assert processed == [], "切换前抓的旧语言音频不能用新语言识别"
    assert t._pending_lang_switch is None
    assert t._asr_scheduled is False

    # 没有切换标志时，音频照常识别（别把正常路径也一起丢了）
    applied.clear()
    processed.clear()
    t._asr_scheduled = True
    t._audio_inbox = [("fake_audio", 1.0), ("fake_audio", 2.0)]
    t._process_inbox()
    assert applied == []
    assert processed == [2], processed

    # 只有切换、无音频：也应能执行并退出
    applied.clear()
    t._asr_scheduled = True
    t._pending_lang_switch = "de"
    t._audio_inbox = []
    t._process_inbox()
    assert applied == ["de"]
    assert t._asr_scheduled is False


def test_request_switch_language_sets_flag_without_starving_submit():
    """热键只写标志；识别已在跑则不另排队（避免排在 inbox 循环后饿死）。"""
    from threading import Lock
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._asr_scheduled = True
    t._pending_lang_switch = None
    submits = []

    class FakeExec:
        def submit(self, fn, *a, **k):
            submits.append(fn)
    t._asr_executor = FakeExec()

    t.request_switch_language("en")
    assert t._pending_lang_switch == "en"
    assert submits == []  # 已 scheduled，靠边界抢占

    t._asr_scheduled = False
    t._pending_lang_switch = None
    t.request_switch_language("de")
    assert t._pending_lang_switch == "de"
    assert t._asr_scheduled is True
    assert submits == [t._process_inbox]


def test_shutdown_unloads_only_our_loaded_models(monkeypatch):
    """退出卸模型（_unload_our_models）：只卸/api/ps里确实加载着的本程序模型。
    ☠️ 对未加载的模型发 keep_alive=0 会先触发一次完整加载——所以"我们的模型
    没加载"时必须一次 post 都不发；用户自己跑的无关模型不能碰。"""
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

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


def test_unload_matches_model_name_without_tag(monkeypatch):
    """☠️ config 里写不带 tag 的名字时，/api/ps 报的是 "name:latest"。

    stop_subtitles.ps1 一直做的是 `-eq $m -or -like "$m`:*"` 前缀匹配，
    Python 侧以前只做集合精确比较 —— 点 ❌ / Alt+F4 退出（不经过停止脚本）
    时漏卸模型，5.6GB 显存按 keep_alive="2h" 白占两小时。两边必须一致。
    """
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator as W

    assert W._model_name_matches("qwen3.5:latest", "qwen3.5")
    assert W._model_name_matches("qwen3.5:9b", "qwen3.5:9b")
    # 不能反向误伤：config 写全名时，别的 tag 不是我们的
    assert not W._model_name_matches("qwen3.5:4b", "qwen3.5:9b")
    # 更不能前缀误伤到别人的模型
    assert not W._model_name_matches("qwen3.5-coder:9b", "qwen3.5")
    assert not W._model_name_matches("", "qwen3.5")
    assert not W._model_name_matches("qwen3.5:latest", None)

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
            return _Resp({"models": [{"name": n} for n in self.loaded]})

        def post(self, url, json=None, **kw):
            posts.append(json)
            return _Resp()

    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5")
    monkeypatch.setattr(config, "GAME_MODE_OLLAMA_MODEL", None, raising=False)

    t = W.__new__(W)
    t.ollama_session = _FakeSession(["qwen3.5:latest", "someone-elses:7b"])
    t._unload_our_models()
    assert [p["model"] for p in posts] == ["qwen3.5:latest"], posts
    assert posts[0]["keep_alive"] == 0


def _translator_for_tx(**overrides):
    """装配一个够跑 _translate_single_sentence / _enqueue_sentences 的翻译器
    （不加载 Whisper/不连 Ollama）。

    ☠️ 顺手把模块级 _warm_done 置位：_await_model_ready 在没置位时会真等
    OLLAMA_WARM_WAIT(60秒)，不置位的话整个测试文件会挂几分钟。"""
    from threading import Lock
    import realtime_subtitle.translate.translator_queue as tq
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    tq._warm_done.set()
    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._tx_lock = Lock()
    t._tx_queue = []
    t._tx_inflight = []
    t._tx_epoch = 0
    t.closing = False
    t._ollama_hot = False
    t._lookup_inflight = False
    t._inflight_lock = __import__('threading').Lock()
    t._stats_lock = Lock()
    t._warm_notified = False
    t._tx_fail_streak = 0
    t._tx_circuit_until = 0.0
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t._ollama_down_notified = 0.0
    # _translate_timeout 要用：ASR 积压快照 + 识别是否正占着 GPU + 降级粘性
    t._asr_backlog_n = 0
    t._asr_busy = False
    t._tx_slow_until = 0.0
    # 端口身份校验：ConnectionError 会把 _ollama_recheck_pending 置真，此后
    # _ollama_identity_ok 会真去 GET /api/version——各用例的假 session 只实现了
    # post，所以这里统一打桩成"已确认是 Ollama"。要测门禁本身的用例自己覆盖它
    t._check_ollama_identity = lambda timeout=2: ("ok", "test")
    t.on_status = None
    for k, v in overrides.items():
        setattr(t, k, v)
    return t


class _FakeStreamResponse:
    """Ollama 流式响应的最小替身（status_code / iter_lines / close）。"""

    def __init__(self, chunks, status_code=200):
        self.status_code = status_code
        self._chunks = chunks
        self.closed = False

    def iter_lines(self):
        import json as _json
        for c in self._chunks:
            yield _json.dumps(c).encode()

    def close(self):
        self.closed = True


def test_translate_retries_once_on_read_timeout(monkeypatch):
    """☠️ 2026-08-02 实测：开机时 Ollama 冷加载 33.8 秒，两句翻译被 15 秒超时
    直接丢弃且永远不会有中文。现在读超时要重试一次（第二次走冷超时）。"""
    import requests

    calls = []
    ok = _FakeStreamResponse([{"response": "你好"}, {"response": "世界", "done": True}])

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            calls.append(timeout)
            if len(calls) == 1:
                raise requests.ReadTimeout("Read timed out. (read timeout=15)")
            return ok

    t = _translator_for_tx(ollama_session=_Session())
    out = t._translate_single_sentence("Hallo Welt.", "")

    assert out == "你好世界"
    assert len(calls) == 2, "读超时应该重试恰好一次"
    assert calls[1] == config.OLLAMA_TIMEOUT_COLD, "重试必须用冷超时，否则还是会被切掉"
    assert ok.closed, "流式连接必须 close，否则不归还连接池"
    assert t._ollama_hot is True  # 成功一次就算热


def test_translate_does_not_retry_connection_error():
    """☠️ Ollama 没起时绝不能重试：会把单线程翻译 worker 堵住。
    保持既有行为——快速失败 + 上屏提示 + 降级德语原文。"""
    import requests

    calls = []

    class _Session:
        def post(self, *a, **kw):
            calls.append(1)
            raise requests.ConnectionError("connection refused")

    notes = []
    t = _translator_for_tx(ollama_session=_Session(),
                           on_status=lambda s: notes.append(s))
    out = t._translate_single_sentence("Hallo Welt.", "")

    assert out == "Hallo Welt."   # 降级德语
    assert len(calls) == 1        # 只发一次
    assert notes and "Ollama" in notes[0]


def test_translate_cold_then_warm_timeout():
    """模型没进显存用冷超时(90)，热了以后回到常规超时(15)。"""
    seen = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            seen.append(timeout)
            return _FakeStreamResponse([{"response": "好", "done": True}])

    t = _translator_for_tx(ollama_session=_Session())
    t._translate_single_sentence("Eins.", "")
    t._translate_single_sentence("Zwei.", "")

    assert seen == [config.OLLAMA_TIMEOUT_COLD, config.OLLAMA_TIMEOUT]


def test_translate_uses_long_timeout_while_asr_backlogged():
    """☠️ issue #16：模型热着不等于 15 秒够用。

    首启时 Whisper 刚加载完、正在集中消化启动积压，GPU 被 ASR 占满，
    翻译排在后面拿不到卡——旧逻辑只看 `_ollama_hot`，于是每句都先白烧
    满 15 秒才重试。ASR 收件箱有积压时必须直接用长超时。"""
    seen = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            seen.append(timeout)
            return _FakeStreamResponse([{"response": "好", "done": True}])

    t = _translator_for_tx(ollama_session=_Session())
    t._ollama_hot = True          # 模型确实在显存里（预热早就成功了）
    t._asr_backlog_n = config.TRANSLATE_SLOW_BACKLOG_BLOCKS  # 但 GPU 被识别占着
    t._translate_single_sentence("Eins.", "")

    assert seen == [config.OLLAMA_TIMEOUT_COLD], \
        "ASR 积压时翻译必须走长超时，否则每句先白烧一次短超时"


def test_translate_backlog_below_threshold_keeps_short_timeout():
    """积压没到阈值就别乱降级：短超时才能快速发现 Ollama 卡死。"""
    seen = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            seen.append(timeout)
            return _FakeStreamResponse([{"response": "好", "done": True}])

    t = _translator_for_tx(ollama_session=_Session())
    t._ollama_hot = True
    t._asr_backlog_n = max(0, config.TRANSLATE_SLOW_BACKLOG_BLOCKS - 1)
    t._translate_single_sentence("Eins.", "")

    assert seen == [config.OLLAMA_TIMEOUT]


def test_translate_timeout_degradation_is_sticky():
    """☠️ issue #16 的震荡：超时→重试(长超时)→成功→_note_tx_result 把
    _ollama_hot 翻回 True→下一句又从短超时开始→再超一次。

    降级必须有粘性：超时之后一段时间内后续句子都用长超时。"""
    import requests

    seen = []

    class _Session:
        def __init__(self):
            # ☠️ 别用 len(seen) 当"第几次请求"：下面要 seen.clear() 才能单独看
            # 第二句用了什么超时，清完这个条件会重新成立、把第二句也打成超时
            self.n = 0

        def post(self, url, json=None, stream=None, timeout=None):
            self.n += 1
            seen.append(timeout)
            if self.n == 1:
                raise requests.ReadTimeout("Read timed out. (read timeout=15)")
            return _FakeStreamResponse([{"response": "好", "done": True}])

    t = _translator_for_tx(ollama_session=_Session())
    t._ollama_hot = True          # 热的：旧逻辑第一句就会用短超时
    t._translate_single_sentence("Eins.", "")
    assert seen == [config.OLLAMA_TIMEOUT, config.OLLAMA_TIMEOUT_COLD], \
        "第一句：短超时撞上超时，重试走长超时"
    assert t._ollama_hot is True  # 重试成功了，模型确实是热的

    seen.clear()
    t._translate_single_sentence("Zwei.", "")
    assert seen == [config.OLLAMA_TIMEOUT_COLD], \
        "刚超时过就该维持长超时，不能一次成功就翻回短超时再超一次"


def test_translate_backlog_rule_can_be_disabled():
    """TRANSLATE_SLOW_BACKLOG_BLOCKS=0 关掉这条规则（给想自己调的人留口子）。"""
    seen = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            seen.append(timeout)
            return _FakeStreamResponse([{"response": "好", "done": True}])

    t = _translator_for_tx(ollama_session=_Session())
    t._ollama_hot = True
    t._asr_backlog_n = 999
    old = config.TRANSLATE_SLOW_BACKLOG_BLOCKS
    config.TRANSLATE_SLOW_BACKLOG_BLOCKS = 0
    try:
        t._translate_single_sentence("Eins.", "")
    finally:
        config.TRANSLATE_SLOW_BACKLOG_BLOCKS = old

    assert seen == [config.OLLAMA_TIMEOUT]


def test_asr_backlog_snapshot_tracks_inbox():
    """积压快照要跟着收件箱走：进队递增、被识别线程取走后清零。
    翻译线程无锁读这个 int，值不对 _translate_timeout 就会误判。"""
    from threading import Lock
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._audio_inbox = []
    t._asr_backlog_n = 0
    t._asr_scheduled = True      # 已排程：enqueue 只入队不 submit，正好隔离
    t._inbox_dropped = 0
    t._inbox_drop_warned = 0
    t._idle_flushed = False

    for i in range(3):
        t.enqueue_audio(f"audio{i}", 1000.0 + i)
    assert t._asr_backlog_n == 3

    # 模拟识别线程取走整批（_process_inbox 里那段）
    with t._asr_lock:
        t._audio_inbox = []
        t._asr_backlog_n = 0
    assert t._asr_backlog_n == 0


def test_translate_num_gpu_is_optional_and_configurable(monkeypatch):
    """不把某台机器的 num_gpu=50 硬编码进所有设备；默认自动分配，
    config_local.py 需要时仍可明确限制层数。"""
    payloads = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            payloads.append(json)
            return _FakeStreamResponse([{"response": "好", "done": True}])

    monkeypatch.setattr(config, "OLLAMA_NUM_GPU", None, raising=False)
    t = _translator_for_tx(ollama_session=_Session())
    t._translate_single_sentence("Eins.", "")
    assert "num_gpu" not in payloads[-1]["options"]

    monkeypatch.setattr(config, "OLLAMA_NUM_GPU", 12, raising=False)
    t._translate_single_sentence("Zwei.", "")
    assert payloads[-1]["options"]["num_gpu"] == 12


def test_await_model_ready_is_interruptible_by_shutdown(monkeypatch):
    """☠️ 模型还在加载时点停止，优雅退出不能被预热等待拖住。

    shutdown() 对 _tx_executor 是 wait=True，打断不了正在 _await_model_ready
    里等的 worker；以前那是一发 `_warm_done.wait(60)` 的干等，于是退出会挂到
    60 秒，而 stop_subtitles.ps1 只给 5 秒宽限就强杀——被强杀掉的正好是
    shutdown() 里排在后面的 _save_lookup_cache() 和 _unload_our_models()
    （查词缓存丢一份、显存按 keep_alive="2h" 继续占着）。
    """
    import time as _time
    import threading
    import realtime_subtitle.translate.translator_queue as tq

    monkeypatch.setattr(config, "OLLAMA_WARM_WAIT", 30, raising=False)
    tq._warm_done.clear()
    monkeypatch.setattr(tq, "_warm_ok", False, raising=False)
    try:
        t = tq.WhisperQueueTranslator.__new__(tq.WhisperQueueTranslator)
        t._ollama_hot = False
        t._warm_notified = False
        t.closing = False
        t.on_status = None

        def _stop_soon():
            _time.sleep(0.3)
            t.closing = True

        threading.Thread(target=_stop_soon, daemon=True).start()
        t0 = _time.time()
        t._await_model_ready()
        elapsed = _time.time() - t0
        assert elapsed < 5, f"退出时不该继续等预热，实际等了 {elapsed:.1f} 秒"
    finally:
        tq._warm_done.set()  # 别把状态留给后面的用例


def test_translate_circuit_breaker_stops_requests(monkeypatch):
    """连续失败到阈值 → 熔断期间一个请求都不发（队列不会因反复超时堆积）。"""
    import requests

    monkeypatch.setattr(config, "TRANSLATE_FAIL_STREAK_OPEN", 3, raising=False)
    monkeypatch.setattr(config, "TRANSLATE_CIRCUIT_SEC", 30, raising=False)

    calls = []

    class _Session:
        def post(self, *a, **kw):
            calls.append(1)
            raise requests.ConnectionError("down")

    t = _translator_for_tx(ollama_session=_Session())
    for _ in range(3):
        t._translate_single_sentence("Test.", "")
    assert len(calls) == 3
    assert t._circuit_open()

    # 熔断中：直接降级，不再发请求
    out = t._translate_single_sentence("Noch ein Test.", "")
    assert out == "Noch ein Test."
    assert len(calls) == 3, "熔断期间不应再发请求"

    # 到期后恢复（下一句正常翻译本身就是探测）
    t._tx_circuit_until = 0.0
    t._translate_single_sentence("Wieder da.", "")
    assert len(calls) == 4


def test_circuit_opens_immediately_after_cold_timeout(monkeypatch):
    """☠️ 连冷超时都没等到数据 = Ollama 半死，一次就该熔断。

    翻译 worker 是单线程的，一句最坏串着烧 OLLAMA_TIMEOUT + OLLAMA_TIMEOUT_COLD
    （默认 15+90=105 秒）。以前要攒够 TRANSLATE_FAIL_STREAK_OPEN=3 次才熔断，
    也就是 **5 分多钟**里每句都去烧满一遍；这期间队列按 TRANSLATE_QUEUE_MAX_CHARS
    一直丢最旧的句子，用户看到的是"中文时有时无"，而不是熔断本该给的那种干净
    降级（只显德语 + 一条状态提示）。

    ConnectionError（Ollama 根本没起）是另一回事：它快速失败、不烧时间，
    仍然走原来的连续 3 次计数，不受这条影响。
    """
    import requests

    monkeypatch.setattr(config, "TRANSLATE_FAIL_STREAK_OPEN", 3, raising=False)
    monkeypatch.setattr(config, "TRANSLATE_CIRCUIT_SEC", 30, raising=False)

    calls = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            calls.append(timeout)
            raise requests.ReadTimeout("Read timed out.")

    t = _translator_for_tx(ollama_session=_Session())
    t._ollama_hot = True  # 模型是热的，走短超时那条路

    out = t._translate_single_sentence("Test.", "")
    assert out == "Test."                     # 降级成德语原文
    assert len(calls) == 2, "应该短超时一次 + 冷超时重试一次"
    assert t._circuit_open(), "两次都超时之后必须立刻熔断，不能再攒 3 次"

    # 熔断中：后面的句子一个请求都不发（不再一句烧 105 秒）
    calls.clear()
    assert t._translate_single_sentence("Noch einer.", "") == "Noch einer."
    assert calls == []


def test_tx_queue_hard_cap(monkeypatch):
    """翻译队列硬顶：丢最旧的句子（它们的德语早滚过去了），保住最新的。"""
    monkeypatch.setattr(config, "TRANSLATE_QUEUE_MAX_CHARS", 40, raising=False)

    class _Exec:
        def submit(self, *a, **k):
            pass

    notes = []
    t = _translator_for_tx(_tx_executor=_Exec(), on_status=lambda s: notes.append(s))
    # 每句 8 字符 → 8 句 64 字符；上限 40 → 丢最旧 3 句，留最新 5 句(40字符)
    t._enqueue_sentences([f"Satz {i:02d}." for i in range(8)])

    assert len(t._tx_queue) == 5
    assert t._tx_queue[0] == "Satz 03."
    assert t._tx_queue[-1] == "Satz 07."
    assert t._tx_dropped == 3
    assert notes and "丢弃" in notes[0]


def test_draft_skipped_while_model_cold():
    """冷加载期间不出草稿：那唯一一次冷加载要留给正式句子。"""
    from threading import Lock

    t = _translator_for_tx()
    t._asr_lock = Lock()
    t._stats_lock = Lock()
    t._stat_draft = 0
    t._audio_inbox = []
    t._asr_busy = False
    t._draft_last_text = ""
    t._draft_last_time = 0.0
    t.pending_text = "Das ist ein ziemlich langer Satz"
    submitted = []

    class _Exec:
        def submit(self, *a, **k):
            submitted.append(a)
    t._tx_executor = _Exec()
    t.on_draft = lambda s: None

    t._ollama_hot = False
    t._maybe_draft()
    assert submitted == [], "模型还冷时不该提交草稿任务"

    t._ollama_hot = True
    t._maybe_draft()
    assert len(submitted) == 1


def test_held_boundary_released_by_flush_without_touching_audio(monkeypatch):
    """扣留的句尾到点由 flush_pending 放行。
    ☠️ 放行路径绝不能碰 processor.finish()——那会清掉正在用的音频缓冲，
    而用户很可能只是句中换了口气，后面还要接着识别。"""
    import time as _time

    monkeypatch.setattr(config, "SENTENCE_HOLD_SEC", 0.05, raising=False)
    monkeypatch.setattr(config, "IDLE_FLUSH_SEC", 999.0)  # 永远不到收尾时机

    class _Processor:
        def __init__(self):
            self.finished = 0

        def finish(self):
            self.finished += 1
            return ""

    t = _translator_for_tx()
    t.processor = _Processor()
    t.pending_text = "Das ist ein Satz."
    t.last_audio_time = _time.time()
    t._idle_flushed = False
    t._held_since = 0.0
    t._last_unstable = ""
    t.on_display = None
    t._stat_held_release = 0  # 放行计数进概况（SENTENCE_HOLD_SEC 够不够长）
    enqueued = []
    t._enqueue_sentences = lambda s: enqueued.extend(s)

    # 第一次切分：句尾在末尾 → 扣留，不出句
    assert t._extract_sentences() == []
    assert t._held_since > 0
    assert t.pending_text == "Das ist ein Satz."

    # 还没到扣留时限：不放行
    t.flush_pending()
    assert enqueued == []

    # 到点后放行，且没动音频缓冲
    _time.sleep(0.06)
    t.flush_pending()
    assert enqueued == ["Das ist ein Satz."]
    assert t.pending_text == ""
    assert t.processor.finished == 0, "有界放行不能调 processor.finish()"


def test_shutdown_waits_for_lookup_before_unloading():
    """☠️ 查词请求带 keep_alive=2h：卸载模型之后才落地的话会把模型重新拉回
    显存留驻两小时。shutdown 必须先有界等查词收尾，再卸载——但不能无限等。"""
    import time as _time
    from concurrent.futures import ThreadPoolExecutor

    order = []

    class _Immediate:
        def shutdown(self, wait=True, cancel_futures=False):
            order.append("exec")

    t = _translator_for_tx()
    t._asr_executor = _Immediate()
    t._tx_executor = _Immediate()
    t._lookup_executor = ThreadPoolExecutor(max_workers=1)
    t._lookup_executor.submit(_time.sleep, 5)  # 假装一次查词卡住了
    # AI 分析是另一个池（也带 keep_alive=2h），两个一起有界排干：
    # 总等待仍是约3秒，不是每个池各等3秒
    t._analysis_executor = ThreadPoolExecutor(max_workers=1)
    t._analysis_executor.submit(_time.sleep, 5)
    t._save_lookup_cache = lambda: None
    t._unload_our_models = lambda: order.append("unload")
    t.ollama_session = type("S", (), {"close": lambda self: None})()
    t.lookup_session = type("S", (), {"close": lambda self: None})()

    t0 = _time.time()
    t.shutdown()
    elapsed = _time.time() - t0

    assert order[-1] == "unload", "卸载必须排在等待之后"
    assert 2.5 < elapsed < 4.5, f"应有界等待约3秒，实测 {elapsed:.1f}s"


def test_translate_stream_has_size_fuse(monkeypatch):
    """☠️ 流式响应必须有累计字符硬顶。

    num_predict 只是**请求**参数，服务端听不听是另一回事：模型钻进复读循环、
    或者 11434 端口被别的本地进程占了（Ollama 没起时谁都能占），iter_lines()
    会一直往 parts 里堆，内存跟着涨、翻译 worker 也永远不返回——单线程池一堵，
    整条字幕链路的中文就停了。
    """
    import realtime_subtitle.translate.translator_queue as tq

    class _NeverEndingResponse:
        status_code = 200

        def iter_lines(self):
            import json as _json
            while True:  # 永不 done 的恶意/异常服务端
                yield _json.dumps({"response": "啊" * 500}).encode()

        def close(self):
            pass

    class _Session:
        def post(self, *a, **kw):
            return _NeverEndingResponse()

    t = _translator_for_tx(ollama_session=_Session())
    t._ollama_hot = True
    out = t._translate_single_sentence("Test.", "")
    assert len(out) <= tq._MAX_STREAM_CHARS + 1000, f"没有截断，收了 {len(out)} 字符"


def test_asr_inbox_hard_cap(monkeypatch):
    """收件箱硬顶：识别线程卡死时丢最旧块保内存，正常积压不受影响"""
    from threading import Lock
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t._asr_lock = Lock()
    t._audio_inbox = []
    t._asr_scheduled = True  # 假装识别线程在跑：enqueue 只进箱不提交 executor
    t._inbox_dropped = 0
    t._inbox_drop_warned = 0
    monkeypatch.setattr(config, "ASR_INBOX_MAX_BLOCKS", 50, raising=False)

    for i in range(120):
        t.enqueue_audio(np.zeros(16, dtype=np.float32), float(i))

    assert len(t._audio_inbox) == 50          # 顶住上限
    assert t._audio_inbox[0][1] == 70.0       # 丢的是最旧的，留最近50块
    assert t._audio_inbox[-1][1] == 119.0
    assert t._inbox_dropped == 70

    # 正常小积压完全不触发
    t2 = WhisperQueueTranslator.__new__(WhisperQueueTranslator)
    t2._asr_lock = Lock()
    t2._audio_inbox = []
    t2._asr_scheduled = True
    t2._inbox_dropped = 0
    t2._inbox_drop_warned = 0
    for i in range(10):
        t2.enqueue_audio(np.zeros(16, dtype=np.float32), float(i))
    assert len(t2._audio_inbox) == 10
    assert t2._inbox_dropped == 0


def test_startup_warm_records_model_it_actually_loaded(monkeypatch):
    """☠️ 预热装的是哪个模型必须记下来，不能事后现读 config.OLLAMA_MODEL。

    窗口是秒开的，⚙️面板在 Whisper 加载的十几秒里就能点。用户点「⚡性能」时
    translator 还是 None，main._apply_mode 只能改 config.OLLAMA_MODEL——而预热
    线程早就按旧名字把大模型往显存里装了。两个模型同时驻留正是 8GB 卡的显存
    分档刻意要避免的情况。
    """
    import realtime_subtitle.translate.translator_queue as tq

    posted = []

    class _Resp:
        def close(self):
            pass

    monkeypatch.setattr(tq.requests, "post",
                        lambda url, json=None, timeout=None: (posted.append(json), _Resp())[1])
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5:9b")

    tq._spawn_startup_warm()
    tq._warm_thread.join(timeout=5)
    assert posted and posted[0]["model"] == "qwen3.5:9b"
    assert tq._warm_model == "qwen3.5:9b"

    # 用户在加载期间切了「性能」→ config 变了，但 _warm_model 记的仍是真正
    # 被装进显存的那个，退出/对账才卸得对
    monkeypatch.setattr(config, "OLLAMA_MODEL", "qwen3.5:4b")
    assert tq._warm_model == "qwen3.5:9b"


def test_warm_model_worker_waits_for_startup_warm_before_unload(monkeypatch):
    """☠️ 卸旧模型前必须等启动预热落地。

    预热请求带 keep_alive="2h"：卸载先到、预热后到的话模型会被重新拉回显存
    留驻两小时（CLAUDE.md 第 13 条那类竞态，当时只处理了退出路径）。
    """
    import threading
    import time as _time
    import realtime_subtitle.translate.translator_queue as tq

    order = []
    warm_running = threading.Event()

    def _slow_warm():
        warm_running.set()
        _time.sleep(0.4)
        order.append("startup-warm-done")

    monkeypatch.setattr(tq, "_warm_thread",
                        threading.Thread(target=_slow_warm, daemon=True),
                        raising=False)
    tq._warm_thread.start()
    warm_running.wait(2)

    class _Resp:
        def close(self):
            pass

    class _Session:
        def post(self, url, json=None, timeout=None):
            order.append("unload" if json.get("keep_alive") == 0 else "warm-new")
            return _Resp()

    t = tq.WhisperQueueTranslator.__new__(tq.WhisperQueueTranslator)
    t.ollama_session = _Session()
    t.closing = False
    t._ollama_hot = True
    t._warm_model_worker(old_model="qwen3.5:9b", new_model="qwen3.5:4b")

    assert order[0] == "startup-warm-done", f"卸载抢在预热前面了：{order}"
    assert order[1:] == ["unload", "warm-new"], order


def test_translator_note_is_stripped():
    """☠️ 2026-08-02 ZDF 实测：句子被截断时模型会追加一整段"（注：建议补全
    后半句…）"到字幕条上。prompt 里已经禁止，这里是兜底剥离。"""
    from realtime_subtitle.translate.translator_queue import _strip_translator_note

    assert _strip_translator_note(
        "美国总统特朗普就伊朗……（注：根据上下文，此处为新闻播报风格，建议补全后半句）"
    ) == "美国总统特朗普就伊朗……"
    # num_predict 截断导致右括号丢失，一样要剥
    assert _strip_translator_note("他来了。（译注：此处原文不完整") == "他来了。"
    assert _strip_translator_note("他来了。(注: 原文如此)") == "他来了。"
    # 正常译文一个字都不能动
    assert _strip_translator_note("该市缺乏足够的运尸车。") == "该市缺乏足够的运尸车。"
    assert _strip_translator_note("他说（笑），这不可能。") == "他说（笑），这不可能。"
    # 整条都是译注：宁可原样显示，也不要给个空白字幕
    only_note = "（注：无法翻译）"
    assert _strip_translator_note(only_note) == only_note


def test_prompt_forbids_translating_the_context():
    """☠️ 2026-08-02 ZDF 实测：半句片段会让模型把整段上下文重翻一遍上屏
    （3/3 复现，74字 vs 应有的15字）——用户看到刚读过的几句又滚一遍。
    三条硬约束必须在 prompt 里，别在"精简 prompt"时被顺手删掉。"""
    payloads = []

    class _Session:
        def post(self, url, json=None, stream=None, timeout=None):
            payloads.append(json)
            return _FakeStreamResponse([{"response": "好", "done": True}])

    t = _translator_for_tx(ollama_session=_Session())
    t._translate_single_sentence("und der Corona-Pandemie.", "Hinter der Französischen Revolution.")

    prompt = payloads[-1]["prompt"]
    assert "不要翻译上下文里的句子" in prompt
    assert "只翻这半句" in prompt
    assert "24小时制" in prompt
    # 规则编号必须连续，不能出现两个"4."（语域各3条 + 通用4条 = 1..7）
    nums = re.findall(r'^(\d+)\. ', prompt, flags=re.M)
    assert nums == [str(i) for i in range(1, len(nums) + 1)], f"规则编号断了: {nums}"


def test_held_release_misfire_counter(monkeypatch):
    """扣留放行后接小写词 = 切在了说话人换气上（德语句首必大写）。

    光数放行次数区分不出"说完了停顿"和"句中换气"——实测 43% 的成句都走放行
    路径，但那里面哪些是切早了，只有这个计数器能回答。它决定要不要调大
    SENTENCE_HOLD_SEC（transcripts 的时间戳是存盘时刻，推不出停顿时长）。
    """
    t = _translator_for_tx()
    t.pending_text = ""
    t._stat_held_release = 0
    t._stat_held_misfire = 0
    t._release_pending = True

    # 放行后接小写德语词 → 记一次切早
    t._append_committed("über den Seeweg eintrifft.")
    assert t._stat_held_misfire == 1
    assert t._release_pending is False, "标志用完必须清，否则后面每次提交都误记"

    # 放行后接大写词（正常新句）→ 不记
    t.pending_text = ""
    t._release_pending = True
    t._append_committed("Danach kam er.")
    assert t._stat_held_misfire == 1

    # 没有放行过的普通提交 → 不记
    t.pending_text = ""
    t._append_committed("und weiter geht es.")
    assert t._stat_held_misfire == 1

    # 中文/非拉丁开头不参与判定
    t.pending_text = ""
    t._release_pending = True
    t._append_committed("中文开头")
    assert t._stat_held_misfire == 1


def test_soxr_resamplestream_keeps_filter_state():
    """soxr major bumps must not break stateful ResampleStream (issue #23 / pitfall #12)."""
    import numpy as np
    try:
        import soxr
    except ImportError:
        import pytest
        pytest.skip("soxr not installed")

    def _stream():
        # soxr 0.5+ uses num_channels=; older used positional channels
        try:
            return soxr.ResampleStream(48000, 16000, num_channels=1, dtype="float32")
        except TypeError:
            return soxr.ResampleStream(48000, 16000, 1, dtype="float32")

    rng = np.random.default_rng(0)
    x = rng.standard_normal(48000).astype(np.float32)

    y1 = np.asarray(_stream().resample_chunk(x, last=True)).reshape(-1)
    y2 = np.asarray(_stream().resample_chunk(x, last=True)).reshape(-1)
    assert y1.shape == y2.shape
    assert np.allclose(y1, y2, atol=1e-5)

    full = _stream()
    y_full = np.asarray(full.resample_chunk(x, last=True)).reshape(-1)
    st = _stream()
    mid = len(x) // 2
    y_a = np.asarray(st.resample_chunk(x[:mid], last=False)).reshape(-1)
    y_b = np.asarray(st.resample_chunk(x[mid:], last=True)).reshape(-1)
    y_stream = np.concatenate([y_a, y_b])
    n = min(len(y_full), len(y_stream))
    assert n > 1000
    corr = float(np.corrcoef(y_full[:n], y_stream[:n])[0, 1])
    assert corr > 0.99, f"soxr stream correlation too low: {corr}"


# ======================================================================
# 2026-08-11 修的一批"防护写了但没生效"（详见各函数注释里的 ☠️）
# ======================================================================

def test_translate_timeout_is_long_while_asr_holds_the_gpu():
    """识别正占着 GPU 时必须用长超时。

    ☠️ 这条不能只靠 _asr_backlog_n：_process_inbox 是先把收件箱整个取走、
    把快照清零，**然后**才跑 process_iter。也就是说 ASR 真正霸着 GPU 的那
    0.26~2.5 秒里 _asr_backlog_n 恰好是 0，规则整个失效——而那正是最需要
    长超时的时刻（issue #16 的 15 秒震荡）。_asr_busy 补的就是这一段。
    """
    from realtime_subtitle import config

    t = _translator_for_tx()
    t._ollama_hot = True          # 模型在显存里 → 默认该走短超时
    t._asr_backlog_n = 0          # 收件箱已被取空，快照读不出积压
    t._asr_busy = False
    assert t._translate_timeout() == config.OLLAMA_TIMEOUT

    t._asr_busy = True            # 但 process_iter 正在 GPU 上跑
    assert t._translate_timeout() == config.OLLAMA_TIMEOUT_COLD, \
        "ASR 占着 GPU 时还用短超时，每句都会先白烧满 OLLAMA_TIMEOUT"


def test_draft_worker_yields_to_asr_at_request_time():
    """草稿的"给 ASR 让路"必须发生在真要发请求的时刻。

    ☠️ 这个检查以前在 _maybe_draft 里，是死代码：那边跑在 ASR 线程上、且
    刚好在 process_iter 的 finally 之后，_asr_busy 恒为 False。而且即使不恒
    为 False 也拦不住——草稿是 submit 到 _tx_executor 异步跑的。
    """
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = _translator_for_tx()
    t.context_history = []
    t.pending_text = "Das ist ein Satz"
    t.on_draft = lambda s: None
    calls = []
    t._translate_single_sentence = lambda *a, **k: calls.append(a) or "译文"

    t._asr_busy = True
    WhisperQueueTranslator._draft_worker(t, "Das ist ein Satz")
    assert calls == [], "ASR 正在 GPU 上跑时不该发草稿请求"

    t._asr_busy = False
    WhisperQueueTranslator._draft_worker(t, "Das ist ein Satz")
    assert len(calls) == 1, "ASR 空闲时草稿应该正常发出"


def test_tx_stats_do_not_accumulate_when_summary_disabled(monkeypatch):
    """关掉概况后翻译耗时样本一个都不该留在内存里。

    ☠️ 排空点只有 _stat_note_asr 里那个到点打印的分支，而它在
    STATS_SUMMARY_INTERVAL<=0 时直接 return。以前 _stat_tx 是在
    _translation_worker 里无条件 append 的，于是用户按 config 注释把概况
    关掉之后，这个 list 就再也没有出口了（一句一条，长跑只涨不消）。
    """
    from realtime_subtitle import config

    t = _translator_for_tx()
    t._stat_tx = []

    monkeypatch.setattr(config, "STATS_SUMMARY_INTERVAL", 0, raising=False)
    for _ in range(500):
        t._stat_note_tx(1.23)
    assert t._stat_tx == [], "概况关掉后仍在累积翻译耗时样本"

    monkeypatch.setattr(config, "STATS_SUMMARY_INTERVAL", 60, raising=False)
    t._stat_note_tx(1.23)
    assert t._stat_tx == [1.23], "概况开着时应该照常采样"


@pytest.mark.parametrize("bad_template", [
    "https://example.com/?q={query}&x={oops}",   # 多了一个占位符
    "https://example.com/?q={query}&brace={",    # 不成对的左括号
    "https://example.com/?q={0}",                # 位置占位符
])
def test_build_ai_web_url_survives_a_broken_template(monkeypatch, bad_template):
    """模板写坏了只能降级成"功能关闭"，不能抛异常。

    ☠️ 调用点在 Qt 槽函数里（SubtitleWindow._open_ai_web），异常会直接冒到
    事件循环，按钮表现为"点了没反应"，用户根本不会去看 stderr。而 config
    的注释本来就鼓励用户把模板换成 ChatGPT 等。
    """
    from realtime_subtitle import config
    from realtime_subtitle.translate.translator_queue import build_ai_web_url

    monkeypatch.setattr(config, "AI_ANALYSIS_WEB_URL_TEMPLATE", bad_template,
                        raising=False)
    assert build_ai_web_url("测试问题") == ""


def test_build_ai_web_url_still_works_for_valid_templates(monkeypatch):
    from realtime_subtitle import config
    from realtime_subtitle.translate.translator_queue import build_ai_web_url

    monkeypatch.setattr(config, "AI_ANALYSIS_WEB_URL_TEMPLATE",
                        "https://chatgpt.com/?q={query}&hints=search", raising=False)
    url = build_ai_web_url("Hallo Welt")
    assert url.startswith("https://chatgpt.com/?q=Hallo%20Welt")
    assert url.endswith("&hints=search")


def test_transcripts_are_kept_forever_by_default():
    """默认必须是"永久保留"——自动删用户攒的语料不该是默认行为。

    transcripts 是拿来回看和学德语的（README 就是这么推的），保留期是给
    介意隐私的人**主动**在 config_local.py 里开的。这条用例盯着这个决定，
    免得以后有人顺手把默认值改成某个天数、把别人的语料悄悄删了。
    """
    from realtime_subtitle import config

    assert getattr(config, "TRANSCRIPT_KEEP_DAYS", 0) == 0


def test_prune_old_transcripts_respects_keep_days(tmp_path, monkeypatch):
    """字幕存档保留期：按文件名日期删，认不出的文件一律不碰。

    ⚠️ 这是隐私措施：本程序抓的是系统全部声音（可能含语音通话），存档是
    明文、按天一个文件、SAVE_TRANSCRIPT 默认开着，以前没有任何保留期。
    """
    import time as _time
    from realtime_subtitle import config
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    day = 86400
    now = _time.time()
    old = _time.strftime("%Y-%m-%d", _time.localtime(now - 40 * day))
    recent = _time.strftime("%Y-%m-%d", _time.localtime(now - 3 * day))
    for name in (f"{old}.txt", f"{recent}.txt", "我的笔记.txt", "readme.md"):
        (tmp_path / name).write_text("x", encoding="utf-8")

    t = object.__new__(WhisperQueueTranslator)
    t._transcript_dir = str(tmp_path)

    monkeypatch.setattr(config, "TRANSCRIPT_KEEP_DAYS", 30, raising=False)
    t._prune_old_transcripts()
    names = {p.name for p in tmp_path.iterdir()}
    assert f"{old}.txt" not in names, "超过保留期的存档没被清掉"
    assert f"{recent}.txt" in names
    assert "我的笔记.txt" in names, "非日期命名的文件不该被动"
    assert "readme.md" in names

    # 0 = 永久保留（老行为），一个都不能删
    monkeypatch.setattr(config, "TRANSCRIPT_KEEP_DAYS", 0, raising=False)
    t._prune_old_transcripts()
    assert {p.name for p in tmp_path.iterdir()} == names


# ======================================================================
# 2026-08-12 审计修复的回归用例
# ======================================================================


def _fake_getaddrinfo(*addrs):
    """造一份 socket.getaddrinfo 的返回：只有 sockaddr[0] 会被读到。"""
    return lambda *a, **kw: [(0, 0, 0, "", (addr, 0)) for addr in addrs]


def test_local_ollama_accepts_loopback(monkeypatch):
    import socket
    from realtime_subtitle.translate import translator_queue as tq

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1"))
    assert tq._assert_local_ollama("http://127.0.0.1:11434") is True
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("::1", "127.0.0.1"))
    assert tq._assert_local_ollama("http://localhost:11434") is True


def test_local_ollama_refuses_remote_host(monkeypatch):
    """☠️ 这条盯的是全项目唯一一条能静默违反隐私承诺的路径。

    README 第一句是「不向任何云端发送音频或文本」，而 OLLAMA_BASE_URL 收的是
    **系统全部声音的转录**（可能含语音通话）。config_local.py 是被 exec 的、
    CLAUDE.md 第 2 节还鼓励 AI 助手去写它——所以这里必须是硬失败，不能是警告：
    改错一个字的后果是转录静默出网，而屏幕上和日志里看不出任何异常。
    """
    import socket
    from realtime_subtitle.translate import translator_queue as tq

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("203.0.113.7"))
    with pytest.raises(tq.RemoteOllamaRefused):
        tq._assert_local_ollama("http://ollama.example.com:11434")

    # 环回 + 外网混在一起也要拦（DNS 返回多条时不能只看第一条）
    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("127.0.0.1", "10.0.0.5"))
    with pytest.raises(tq.RemoteOllamaRefused):
        tq._assert_local_ollama("http://ollama.lan:11434")


def test_local_ollama_opt_in_allows_remote(monkeypatch):
    """确实要用另一台机器的 Ollama：显式声明即放行（知情同意）。"""
    import socket
    from realtime_subtitle.translate import translator_queue as tq

    monkeypatch.setattr(socket, "getaddrinfo", _fake_getaddrinfo("203.0.113.7"))
    monkeypatch.setattr(config, "ALLOW_REMOTE_OLLAMA", True, raising=False)
    assert tq._assert_local_ollama("http://ollama.example.com:11434") is True


def test_local_ollama_unresolvable_does_not_block_startup(monkeypatch):
    """解析不了就放行：请求本来也发不出去，交给连通性检查报。
    在这里拦只会把"Ollama 没起来"升级成"程序起不来"。"""
    import socket
    from realtime_subtitle.translate import translator_queue as tq

    def _boom(*a, **kw):
        raise socket.gaierror("no such host")

    monkeypatch.setattr(socket, "getaddrinfo", _boom)
    assert tq._assert_local_ollama("http://nope.invalid:11434") is True


def test_ai_web_url_requires_query_placeholder(monkeypatch):
    """模板里没有 {query} 时 .format() 不报错、原样返回——于是浏览器打开一个
    空首页，用户刚才那句问题凭空消失，而且没有任何提示。必须显式挡住。"""
    from realtime_subtitle.translate.translator_queue import build_ai_web_url

    monkeypatch.setattr(config, "AI_ANALYSIS_WEB_URL_TEMPLATE",
                        "https://grok.com/", raising=False)
    assert build_ai_web_url("问题") == ""


def test_ai_web_url_requires_http_scheme(monkeypatch):
    """webbrowser.open 在 Windows 上对"不像 URL"的字符串会退化成 os.startfile
    （= ShellExecute）。把配置写错的后果限制成"按钮不可用"。"""
    from realtime_subtitle.translate.translator_queue import build_ai_web_url

    for bad in ("file:///C:/Windows/system32/calc.exe?{query}",
                r"C:\Windows\system32\calc.exe {query}",
                "grok.com/?q={query}"):
        monkeypatch.setattr(config, "AI_ANALYSIS_WEB_URL_TEMPLATE", bad, raising=False)
        assert build_ai_web_url("问题") == "", bad


def test_ai_web_url_still_builds_normal_template(monkeypatch):
    from realtime_subtitle.translate.translator_queue import build_ai_web_url

    monkeypatch.setattr(config, "AI_ANALYSIS_WEB_URL_TEMPLATE",
                        "https://grok.com/?q={query}", raising=False)
    url = build_ai_web_url("a b&c")
    assert url == "https://grok.com/?q=a%20b%26c"


def test_ollama_identity_blocks_impostor_but_not_downtime(monkeypatch):
    """端口身份校验：只拦"有回应但不是 Ollama"，不拦"连不上"。

    Ollama 会自动更新并重启（CLAUDE.md 第 4 节第 6 条），重启窗口期里 11434
    是空的、本机任何进程都能补位——启动时查一次不够。但连不上时必须放行，
    否则"Ollama 没起来"会变成"字幕永远没有中文"（现有的 ConnectionError
    路径才是报这件事的地方，还带 60 秒节流的屏幕提示）。
    """
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    def _make(state):
        t = object.__new__(WhisperQueueTranslator)
        t.on_status = None
        t._ollama_recheck_pending = True   # 断过连，需要重验
        t._check_ollama_identity = lambda timeout=2: (state, "")
        return t

    impostor = _make("impostor")
    assert impostor._ollama_identity_ok() is False
    assert impostor._ollama_identity_ok() is False, "节流期内也要保持拦住"

    down = _make("unreachable")
    assert down._ollama_identity_ok() is True, "连不上不该拦，交给 ConnectionError 路径"
    assert down._ollama_recheck_pending is True, "还没有明确结论，下次还要再验"

    good = _make("ok")
    assert good._ollama_identity_ok() is True
    assert good._ollama_recheck_pending is False, "验过了就不该再多花一次请求"


def test_ollama_identity_costs_nothing_on_the_normal_path():
    """启动时验过、此后没断过连 = 一次属性判断，零请求。

    这条盯的是"别为了安全检查给每句字幕都加一次 2 秒 GET"——假 session 上
    连 .get 都没有，一旦有人把门禁改成无条件重验，这里就会 AttributeError。
    """
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t.on_status = None
    assert t._ollama_identity_ok() is True


def test_save_transcript_prunes_on_day_rollover(tmp_path, monkeypatch):
    """保留期以前只在 __init__ 里跑一次，而字幕程序的典型用法是开机挂着不关
    ——也就是说设了 TRANSCRIPT_KEEP_DAYS 的用户只要不重启就永远不会真的清理。
    """
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t._transcript_ok = True
    t._transcript_dir = str(tmp_path)
    t._transcript_day = "2026-08-11"  # 假装上一条是昨天写的

    calls = []
    t._prune_old_transcripts = lambda: calls.append(1)

    t._save_transcript("Hallo.", "你好")
    assert calls == [1], "跨天时必须重跑一次保留期清理"
    t._save_transcript("Noch was.", "还有")
    assert calls == [1], "同一天内不该反复清理"


def test_finish_prunes_committed_words_out_of_buffer():
    """☠️ commited_in_buffer 的语义是"已提交、且音频还在缓冲里"的词，而
    finish() 刚把整个音频缓冲丢掉——那批词按定义就全过期了。

    以前 finish() 不 pop，于是唯一的排空点只剩 _chunk_at()，而它只在缓冲涨过
    BUFFER_TRIM_SEC(12秒) 时才触发。看剧/聊天/游戏语音正好是"短促片段 + 中间
    有静音"，缓冲根本涨不到 12 秒，每段说完都走 finish()——这个 list 于是整场
    只涨不消（修复前本用例跑出 1200 条，而同类的 self.commited 一直被截在 200）。
    """
    buf = HypothesisBuffer()
    buf.commited_in_buffer = [(0.0, 1.0, "a"), (1.0, 2.0, "b")]
    buf.buffer = [(2.0, 3.0, "c")]

    class _P(OnlineASRProcessor):
        def __init__(self):
            self.model = None
            self.init()

    p = _P()
    p.transcript_buffer = buf
    p.audio_buffer = np.zeros(16000 * 3, dtype=np.float32)

    assert p.finish() == "c"
    assert buf.commited_in_buffer == [], "音频缓冲已清空，已提交词必须一起过期"


def test_tx_drop_warning_is_throttled(capsys):
    """队列丢弃告警的 print 必须和 on_status 一起被节流。

    以前 print 在 if 外面，于是被节流的只有屏幕提示——而这个函数触发的场景
    恰恰是"Ollama 半死、每句都超时"，也就是它会以每句一行的频率刷
    subtitle.log。对照组是 enqueue_audio 里收件箱那条告警（print 在节流内）。
    """
    from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator

    t = object.__new__(WhisperQueueTranslator)
    t._tx_dropped = 0
    t._tx_drop_warned = 0
    t.on_status = None

    for _ in range(25):
        t._tx_dropped += 1
        t._warn_tx_dropped(1)

    lines = [ln for ln in capsys.readouterr().out.splitlines() if "翻译积压" in ln]
    # 25 次丢弃：首次 + 每累计 10 条一次 = 3 行，绝不该是 25 行
    assert len(lines) <= 4, f"告警没被节流，打了 {len(lines)} 行"
    assert lines, "节流过头了，一条都没报"
