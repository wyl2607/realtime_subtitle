"""回归测试：德语字幕在句中被提前切开（一句拆成两条显示），以及误否决导致成句失败。

包含两类用例：
1. 正向用例（不能切开 / 不能在句中提前截断）：
   - 带空格的缩写（"z. B.", "d. h.", "u. a.", "z. T.", "v. a.", "i. d. R."）；
   - 无歧义德语缩写（"Hr.", "Fr.", "Fa.", "Schiller-Str.", "Kap.", "19. Jh.", "Dr. med."）；
   - 句中停顿/犹豫引入的省略号（"..." / "…"）紧跟介词/冠词/连词等不完整词；
   - ASR 识别线程句尾扣留 lookahead 遇到带点缩写时不被误放行。

2. 负向用例（必须正常断句 / final=True 时必须闭合放行）：
   - 常见句末普通词（"so", "Art" 等）绝不能被误判为缩写而阻止断句；
   - 句末孤立大写单字母（"Vitamin D.", "Plan B." 等）绝不能被单字母规则误扣留；
   - 三位数字（"Das kostet 100."）在句末接新句大写词时必须正常断句；
   - 真实新闻广播中以短词结尾的句子必须正常闭合成句。
"""
import realtime_subtitle.config as config
from realtime_subtitle.asr.streaming_asr import TimedText
from realtime_subtitle.translate.text_rules import _boundary_is_real, _split_sentences
from tests.test_held_boundary_lookahead import _round, _translator


# ======================================================================
# 1. 带空格的多词缩写（"z. B.", "d. h.", "u. a.", "z. T.", "v. a.", "i. d. R."）
# ======================================================================

def test_no_split_on_spaced_abbreviation_zb():
    """'z. B.' 后面即使是大写名词或普通词，也不能在 'z.' 或 'B.' 处切开。"""
    text = "Das ist z. B. der Fall. Und das ist gut."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das ist z. B. der Fall.", "Und das ist gut."], sents
    assert rest == ""


def test_no_split_on_spaced_abbreviation_ua():
    """'u. a.' 后面紧跟大写德语名词时，不能在 'u. a.' 处切开。"""
    text = "Wir besprechen u. a. Themen wie Wirtschaft. Danach machen wir Pause."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Wir besprechen u. a. Themen wie Wirtschaft.", "Danach machen wir Pause."], sents
    assert rest == ""


def test_no_split_on_spaced_abbreviation_dh():
    """'d. h.' 后面紧跟专有名词时，不能在 'd. h.' 处切开。"""
    text = "Das bedeutet d. h. Deutschland muss handeln. Alles ist bereit."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das bedeutet d. h. Deutschland muss handeln.", "Alles ist bereit."], sents
    assert rest == ""


def test_no_split_on_spaced_abbreviation_zt():
    """'z. T.' 后面跟大写词时不能被切开。"""
    text = "Das ist z. T. Unsinn. Wir wissen es besser."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das ist z. T. Unsinn.", "Wir wissen es besser."], sents
    assert rest == ""


def test_no_split_on_spaced_abbreviation_va():
    """'v. a.' (vor allem) 后面跟大写名词不能被切开。"""
    text = "Das betrifft v. a. Jugendliche in Städten. Sie brauchen Hilfe."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das betrifft v. a. Jugendliche in Städten.", "Sie brauchen Hilfe."], sents
    assert rest == ""


def test_no_split_on_spaced_abbreviation_idr():
    """'i. d. R.' 三词缩写不能被切开。"""
    text = "Das gilt i. d. R. immer. Wir haben es geprüft."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das gilt i. d. R. immer.", "Wir haben es geprüft."], sents
    assert rest == ""


# ======================================================================
# 2. 无歧义德语缩写（"Hr.", "Fr.", "Fa.", "Str.", "Kap.", "Jh.", "Dr. med."）
# ======================================================================

def test_no_split_on_titles_hr_fr():
    """'Hr. Schmidt' / 'Fr. Weber' 称谓后跟大写人名不能切开。"""
    text1 = "Wir danken Hr. Schmidt für die Unterstützung. Er hat uns sehr geholfen."
    sents1, _ = _split_sentences(text1, final=True, lang="de")
    assert sents1 == ["Wir danken Hr. Schmidt für die Unterstützung.", "Er hat uns sehr geholfen."], sents1

    text2 = "Ich habe Fr. Weber gestern getroffen. Sie war sehr freundlich."
    sents2, _ = _split_sentences(text2, final=True, lang="de")
    assert sents2 == ["Ich habe Fr. Weber gestern getroffen.", "Sie war sehr freundlich."], sents2


def test_no_split_on_company_and_street():
    """'Fa. Siemens' / 'Goethe-Str. 10' 不能切开。"""
    text1 = "Wir besuchen die Fa. Siemens in Berlin. Das Treffen beginnt um zehn."
    sents1, _ = _split_sentences(text1, final=True, lang="de")
    assert sents1 == ["Wir besuchen die Fa. Siemens in Berlin.", "Das Treffen beginnt um zehn."], sents1

    text2 = "In der Schiller-Str. 10 befindet sich das Büro. Es ist geöffnet."
    sents2, _ = _split_sentences(text2, final=True, lang="de")
    assert sents2 == ["In der Schiller-Str. 10 befindet sich das Büro.", "Es ist geöffnet."], sents2


def test_no_split_on_references_and_century():
    """'Kap. 3' / '19. Jh.' 不能在句中切开。"""
    text1 = "Siehe Kap. 3 für weitere Details. Dort steht alles."
    sents1, _ = _split_sentences(text1, final=True, lang="de")
    assert sents1 == ["Siehe Kap. 3 für weitere Details.", "Dort steht alles."], sents1

    text2 = "Im 19. Jh. lebten viele Dichter. Ihre Werke sind berühmt."
    sents2, _ = _split_sentences(text2, final=True, lang="de")
    assert sents2 == ["Im 19. Jh. lebten viele Dichter.", "Ihre Werke sind berühmt."], sents2


def test_no_split_on_doctor_degree_abbreviation():
    """'Dr. med. Weber' 不能在 'med.' 处切开。"""
    text = "Dr. med. Weber leitet die Klinik. Er operiert heute."
    sents, _ = _split_sentences(text, final=True, lang="de")
    assert sents == ["Dr. med. Weber leitet die Klinik.", "Er operiert heute."], sents


# ======================================================================
# 3. 句中停顿/犹豫引入的省略号（"..." / "…"）
# ======================================================================

def test_no_split_on_mid_sentence_ellipsis_before_noun():
    """句中停顿的省略号在动词/介词/连词后紧接大写名词时，不能提前切句。"""
    text1 = "Wir brauchen … Geduld für diese Aufgabe. Dann schaffen wir es."
    sents1, _ = _split_sentences(text1, final=True, lang="de")
    assert sents1 == ["Wir brauchen … Geduld für diese Aufgabe.", "Dann schaffen wir es."], sents1

    text2 = "Ich glaube, dass ... Deutschland gewinnen wird. Sie spielen gut."
    sents2, _ = _split_sentences(text2, final=True, lang="de")
    assert sents2 == ["Ich glaube, dass ... Deutschland gewinnen wird.", "Sie spielen gut."], sents2

    text3 = "Er wartet auf … Hilfe von der Polizei. Niemand kommt."
    sents3, _ = _split_sentences(text3, final=True, lang="de")
    assert sents3 == ["Er wartet auf … Hilfe von der Polizei.", "Niemand kommt."], sents3


# ======================================================================
# 4. ASR 句尾扣留 Lookahead
# ======================================================================

def test_held_boundary_lookahead_does_not_release_title(monkeypatch):
    """当前块以 'Wir danken Hr.' 结尾，unstable 看到 'Schmidt' 时，不能提前放行切断。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Wir danken Hr."), "Schmidt für alles")])

    _round(t)

    assert t._tx_queue == []
    assert t.pending_text == "Wir danken Hr."


# ======================================================================
# 5. 负向测试：必须正常成句 / final=True 时必须闭合（防回归）
# ======================================================================

def test_negative_must_close_on_so():
    """'so' 是德语普通词，句尾 '... so.' 绝不能被当成缩写（s.o.）而阻止闭合。"""
    for text in ["Das ist so.", "Genau so.", "Ich sehe es so."]:
        assert _boundary_is_real(text, "", final=True, lang="de") is True, f"Failed on {text}"
        sents, rest = _split_sentences(text, final=True, lang="de")
        assert sents == [text], f"Failed splitting: {sents}"
        assert rest == ""


def test_negative_must_close_on_art():
    """'Art' 是德语普通名词，'auf diese Art.' 必须正常成句，不可被误当缩写。"""
    candidate = "Das geht auf diese Art."
    assert _boundary_is_real(candidate, "", final=True, lang="de") is True
    sents, rest = _split_sentences("Das geht auf diese Art. Danach sehen wir weiter.", final=True, lang="de")
    assert sents == ["Das geht auf diese Art.", "Danach sehen wir weiter."], sents
    assert rest == ""


def test_negative_must_close_on_single_capital_letter():
    """句尾孤立大写单字母（如 'Vitamin D.', 'Plan B.'）必须正常闭合，不能被单字母规则误扣留。"""
    for cand in ["Er nimmt Vitamin D.", "Wir wählen Plan B."]:
        assert _boundary_is_real(cand, "", final=True, lang="de") is True, f"Failed on {cand}"

    text1 = "Er nimmt Vitamin D. Das hilft ihm sehr."
    sents1, _ = _split_sentences(text1, final=True, lang="de")
    assert sents1 == ["Er nimmt Vitamin D.", "Das hilft ihm sehr."], sents1

    text2 = "Wir wählen Plan B. Alles ist vorbereitet."
    sents2, _ = _split_sentences(text2, final=True, lang="de")
    assert sents2 == ["Wir wählen Plan B.", "Alles ist vorbereitet."], sents2


def test_negative_must_split_on_three_digit_number():
    """三位数在句末紧接新句大写词时，必须正常断句，不能被序数规则误否决。"""
    cand = "Das kostet 100."
    rem = "Danach gehen wir."
    assert _boundary_is_real(cand, rem, final=False, lang="de") is True
    assert _boundary_is_real(cand, rem, final=True, lang="de") is True

    text = "Das kostet 100. Danach gehen wir."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das kostet 100.", "Danach gehen wir."], sents
    assert rest == ""


def test_negative_must_close_on_realistic_news_sentences_with_short_words():
    """现实德语新闻广播中以短词（名词/地名/介词副词/肯定词等）结尾的句子，必须正常成句。"""
    news_pairs = [
        ("Die Lage bleibt unklar vor Ort.", "Die Polizei sichert das Gebäude."),
        ("Der Minister reiste nach Rom.", "Dort führt er politische Gespräche."),
        ("Der Druck auf die Regierung nimmt zu.", "Viele fordern sofortige Reformen."),
        ("Der Bericht liegt nun vor.", "Wir prüfen die neuen Zahlen."),
        ("Die Abgeordneten stimmten mit Ja.", "Das Gesetz ist damit beschlossen."),
    ]
    for s1, s2 in news_pairs:
        # 单独以 final=True 闭合
        assert _boundary_is_real(s1, "", final=True, lang="de") is True, f"Failed closing {s1}"
        # 两句连接时正确切分成两句
        full_text = f"{s1} {s2}"
        sents, rest = _split_sentences(full_text, final=True, lang="de")
        assert sents == [s1, s2], f"Failed splitting {full_text}: got {sents}"
        assert rest == ""


# ======================================================================
# 6. D1 Lookahead 回归测试（PR #91 补修）
# ======================================================================

def test_d1_lookahead_holds_ellipsis_then_capital_noun(monkeypatch):
    """committed "Wir brauchen …" + unstable "Geduld für" → 必须扣住，不切成 "Wir brauchen …"。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    # 第一轮：committed 以省略号结尾，unstable 以大写名词开头
    t = _translator([(TimedText("Wir brauchen …"), "Geduld für diese Aufgabe")])

    _round(t)

    # D1 lookahead 应该看到 "Geduld" (大写)，但因为前词是不完整词 "brauchen"，
    # 省略号否决规则生效 → 扣住
    assert t._tx_queue == [], f"Expected empty queue, got {[str(s) for s in t._tx_queue]}"
    assert t.pending_text == "Wir brauchen …", f"Expected held text, got {t.pending_text!r}"


def test_d1_lookahead_holds_single_letter_abbreviation(monkeypatch):
    """committed "Das gilt z." + unstable "B. für alle" → 必须扣住，不切成 "Das gilt z."。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Das gilt z."), "B. für alle")])

    _round(t)

    # ①c 单字母缩写否决：lookahead 以 "B." 开头，配对 "z.b" 在 _SPACED_ABBREVIATIONS 中
    assert t._tx_queue == [], f"Expected empty queue, got {[str(s) for s in t._tx_queue]}"
    assert t.pending_text == "Das gilt z.", f"Expected held text, got {t.pending_text!r}"


def test_d1_lookahead_still_holds_title(monkeypatch):
    """committed "Wir danken Hr." + unstable "Schmidt für" → 正确扣住（①a 不依赖 remainder，保持原行为）。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Wir danken Hr."), "Schmidt für alles")])

    _round(t)

    assert t._tx_queue == [], f"Expected empty queue, got {[str(s) for s in t._tx_queue]}"
    assert t.pending_text == "Wir danken Hr.", f"Expected held text, got {t.pending_text!r}"


# ======================================================================
# 7. D1 Lookahead 反向守卫：正常句尾必须照常放行
# ======================================================================

def test_d1_lookahead_releases_normal_sentence_end(monkeypatch):
    """committed "Das ist gut." + unstable "Morgen kommt er" → 照常放行成句（D1 原语义不变）。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Das ist gut."), "Morgen kommt er")])

    _round(t)

    # "gut." 是真句尾，lookahead 以大写 "Morgen" 开头 → 放行
    assert len(t._tx_queue) == 1
    assert str(t._tx_queue[0]) == "Das ist gut."
    assert t._held_since == 0.0


def test_d1_lookahead_releases_single_capital_letter(monkeypatch):
    """committed "Er nimmt Vitamin D." + unstable "Das hilft" → 照常放行（孤立大写单字母不被误扣）。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Er nimmt Vitamin D."), "Das hilft")])

    _round(t)

    assert len(t._tx_queue) == 1
    assert str(t._tx_queue[0]) == "Er nimmt Vitamin D."
    assert t._held_since == 0.0


# ======================================================================
# 8. _split_sentences 直接测试（Phil 人名、情态动词省略号、中文无影响）
# ======================================================================

def test_split_sentences_phil_name():
    """_split_sentences("Das sagt auch Phil. Er hat recht.", final=True, lang="de") → 两句。"""
    sents, rest = _split_sentences("Das sagt auch Phil. Er hat recht.", final=True, lang="de")
    assert sents == ["Das sagt auch Phil.", "Er hat recht."], sents
    assert rest == ""


def test_split_sentences_ellipsis_with_modal_verb():
    """Wir müssen … Geduld haben. Dann geht es. → 两句且第一句含省略号。"""
    sents, rest = _split_sentences("Wir müssen … Geduld haben. Dann geht es.", final=True, lang="de")
    assert sents == ["Wir müssen … Geduld haben.", "Dann geht es."], sents
    assert rest == ""
    assert "…" in sents[0]


def test_split_sentences_ellipsis_with_es_not_modal():
    """Das war es … Morgen geht es weiter. → 两句（es 是代词不是情态动词，不触发否决）。"""
    sents, rest = _split_sentences("Das war es … Morgen geht es weiter.", final=True, lang="de")
    assert sents == ["Das war es …", "Morgen geht es weiter."], sents
    assert rest == ""


def test_split_sentences_chinese_lookahead_no_effect():
    """中文：lookahead 不改变行为，结果与不传 lookahead 完全相同。"""
    # 用单句无中间省略号的文本，避免触发原有的 "……" 双省略号切分行为
    sents_with, rest_with = _split_sentences("我们需要耐心。", final=True, lang="zh", lookahead="然后")
    sents_without, rest_without = _split_sentences("我们需要耐心。", final=True, lang="zh")
    assert sents_with == sents_without, f"With lookahead: {sents_with}, without: {sents_without}"
    assert rest_with == rest_without
    assert sents_with == ["我们需要耐心。"]
