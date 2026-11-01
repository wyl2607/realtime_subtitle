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
# 6. D1 放行时的 lookahead：remainder 为空也要让否决看到下文（PR #91 补修）
# ======================================================================

def test_held_boundary_lookahead_does_not_release_ellipsis(monkeypatch):
    """当前块停在 'Wir brauchen …'，unstable 看到 'Geduld' 时不能提前切成一句。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Wir brauchen …"), "Geduld für")])

    _round(t)

    assert t._tx_queue == []
    assert t.pending_text == "Wir brauchen …"


def test_held_boundary_lookahead_does_not_release_spaced_abbreviation(monkeypatch):
    """当前块停在 'Das gilt z.'，unstable 看到 'B. für alle' 时不能提前切成一句。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Das gilt z."), "B. für alle")])

    _round(t)

    assert t._tx_queue == []
    assert t.pending_text == "Das gilt z."


def test_held_boundary_lookahead_still_releases_normal_sentence(monkeypatch):
    """反向守卫：真句尾 + 大写下文，D1 必须照常放行（原语义不变）。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Das ist gut."), "Morgen kommt er")])

    _round(t)

    assert [str(s) for s in t._tx_queue] == ["Das ist gut."]
    assert t.pending_text == ""


def test_held_boundary_lookahead_still_releases_single_capital_letter(monkeypatch):
    """反向守卫：'Vitamin D.' 后接大写词，不能被单字母规则误扣留。"""
    monkeypatch.setattr(config, "SOURCE_LANGUAGE", "de", raising=False)
    t = _translator([(TimedText("Er nimmt Vitamin D."), "Das hilft")])

    _round(t)

    assert [str(s) for s in t._tx_queue] == ["Er nimmt Vitamin D."]
    assert t.pending_text == ""


def test_split_after_name_phil():
    """'Phil.' 是人名句尾，不能被当成学位缩写（phil.）而并句。"""
    sents, rest = _split_sentences("Das sagt auch Phil. Er hat recht.", final=True, lang="de")
    assert sents == ["Das sagt auch Phil.", "Er hat recht."], sents
    assert rest == ""


def test_split_on_ellipsis_after_modal_verb():
    """情态动词后的省略号是句中停顿：整句成句，且第一句含省略号。"""
    text = "Wir müssen … Geduld haben. Dann geht es."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Wir müssen … Geduld haben.", "Dann geht es."], sents
    assert "…" in sents[0]
    assert rest == ""


def test_split_on_ellipsis_after_pronoun_still_splits():
    """人称代词后的省略号是省略号停顿，不能当不完整词否决。"""
    text = "Das war es … Morgen geht es weiter."
    sents, rest = _split_sentences(text, final=True, lang="de")
    assert sents == ["Das war es …", "Morgen geht es weiter."], sents
    assert rest == ""


def test_chinese_lookahead_is_ignored():
    """无空格语言不吃 lookahead，行为必须与不传时完全相同。"""
    text = "我们需要……耐心。"
    without = _split_sentences(text, final=True, lang="zh")
    with_lookahead = _split_sentences(text, final=True, lang="zh", lookahead="然后")
    assert with_lookahead == without, (with_lookahead, without)
