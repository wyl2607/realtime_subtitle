"""切句与"无空格语言"规则：纯函数，不碰模型、线程、网络。

从 translator_queue.py 拆出来的（2026-09-27，继 lookup / transcript /
runtime_stats 之后的第四刀）。拆的理由和前三刀一样不是行数，而是**这一块
有自己的规则体系**：CLAUDE.md 第 4 节第 32 条那一整串"中文/日文是另一套
规则"的坑全落在这里（全角终止符、按字符而非按词的阈值、`" ".join` 对
无空格语言要换空串……），和识别/翻译线程、锁、Ollama 没有任何交集。
放在一起之后，"换成中文还成立吗"这个问题只需要对着一个文件问。

☠️ 统一入口仍然是 `_no_space_language()`，语言集合在
`config.NO_SPACE_LANGUAGES`。加韩语/泰语往 config 里补，别在调用点散着判。

☠️ translator_queue 把这里的名字**全部 re-export**，测试和 offline 里
`translator_queue._split_sentences` 这类写法照旧能用。新代码直接从本模块
import 即可；但**别删那段 re-export**（理由写在那边）。

`_glossary_applies` / `_interjection_lookup` 没有跟过来：它们判的是
"当前语言对"（翻译策略），不是文本规则，留在 translator_queue。
"""
import logging
import re

import realtime_subtitle.config as config

_log = logging.getLogger(__name__)


# 句子结束符：只认 .!?（旧管线按逗号切句是碎句/上下文错乱的来源之一）。
# 切分时要按位置往后扫，不能每次都从头 match，所以只留匹配终止符本身的这一条
# （成对的 _SENTENCE_END 在改成 finditer 之后就没有调用点了，已删）
_SENTENCE_TERMINATOR = re.compile(r'[.!?…]["»«\']?(?=\s|$)')

# ☠️ 中文/日文必须用另一套规则，否则**一句都切不出来**（2026-08-12 实测）：
#   "这是第一句。然后还有一句？对的。"  →  上面那条正则切出 0 句
# 两个原因，缺一不可：
#   1. 全角 。！？ 不在 [.!?…] 里；
#   2. 就算加进去，`(?=\s|$)` 也永远不成立——中文 Whisper 输出没有空格。
# 而当时的兜底 MAX_PENDING_WORDS 用的是 `.split()`，中文整段恒等于 1 个"词"，
# 于是第二道闸也是死的：中文语音只能靠 IDLE_FLUSH_SEC（说话人停满 2 秒）
# 才冲得出来一次字幕，完全谈不上实时。
#
# 全角终止符**不要求后面跟空白**：它们本身就无歧义，不像半角 . 可能是小数点
# 或缩写点。半角那半条仍保留 (?=\s|$)，这样中文里夹的 "3.5" 不会被切开。
_SENTENCE_TERMINATOR_CJK = re.compile(r'[。！？…]["»«\'』」）)]?|[.!?](?=\s|$)')

_PUNCT_STRIP = " \t.!?…,;:–—\"'«»„“”"
_QUOTE_CHARS = "\"'«»„“”"
_TERMINATOR_CHARS = (".", "!", "?", "…")
_TERMINATOR_CHARS_CJK = ("。", "！", "？", "…", ".", "!", "?")


def _no_space_language(lang=None):
    """这个源语言是不是"不用空格分词"的书写系统（中文/日文）。

    切句、残句长度兜底、复读压缩三处的规则都要跟着变——它们原本全是按
    "空格分词的拉丁语系"写的。语言集合放 config 里，方便加韩语/泰语等。
    """
    lang = lang if lang is not None else config.SOURCE_LANGUAGE
    return lang in getattr(config, "NO_SPACE_LANGUAGES", ("zh", "ja"))


def _terminator_re(lang=None):
    return _SENTENCE_TERMINATOR_CJK if _no_space_language(lang) else _SENTENCE_TERMINATOR


def _ends_with_terminator(text, lang=None):
    """文本是不是正好停在一个句子终止符上（判断"要不要扣留"用）。"""
    chars = _TERMINATOR_CHARS_CJK if _no_space_language(lang) else _TERMINATOR_CHARS
    return bool(text) and text.rstrip().rstrip(_QUOTE_CHARS).endswith(chars)


# 模型偶尔在译文后面追加"（注：……）"——句子被截断时最容易触发（实测
# "US-Präsident Trump hat dem Iran..." 收到一整段"建议补全后半句"的说明）。
# prompt 里已经明说不要加，这里是兜底：字幕条上多这么一坨没人想看。
# num_predict 截断会让右括号丢掉，所以右括号是可选的。
_TRANSLATOR_NOTE = re.compile(r'[（(]\s*(?:译注|注|说明|备注)\s*[：:][^）)]*[）)]?\s*$')
# 译注不只出现在末尾：2026-08-04 复核 23157 条真实句对，实测有"多特蒙德（注：
# 此处应为杜塞尔多夫）住过、干过活儿"这种夹在句子中间的。中间的那种右括号一定
# 在（没被 num_predict 截断），所以这条要求右括号闭合，不会误吃掉正文
_TRANSLATOR_NOTE_INLINE = re.compile(r'[（(]\s*(?:译注|注|说明|备注)\s*[：:][^）)]*[）)]')


def _strip_translator_note(text):
    """去掉译文里的译注；如果整条都是译注就原样返回（宁可多显示不可显示空白）。

    先去中间的（要求括号闭合），再去末尾的（右括号可选——生成被
    num_predict 截断时右括号会丢）。
    """
    cleaned = _TRANSLATOR_NOTE_INLINE.sub("", text)
    cleaned = _TRANSLATOR_NOTE.sub("", cleaned).strip()
    return cleaned or text.strip()


# 译文目标语言是简体中文时的繁→简兜底。小模型（qwen3.5:4b 等）即使 prompt
# 里已写"简体中文"（config.TRANSLATION_TARGET_NAMES），仍会偶发繁体，实测例
# 「Mews的這種概念」。节点（node/engines.py）和桌面本机 Ollama
# （translator_queue.py）共用本函数，在译文出口做最后一道转换。
# ☠️ 只在目标语言是简体中文时转；de/en 等原样返回，一个字都不动。
# 目标语言码可能是 "zh"（config）或 "zh-Hans"（Apple Translation 的写法），
# 两个都收；大小写不敏感。opencc 是纯 Python 库，模块级懒加载一次。
_SIMPLIFIED_TARGETS = {"zh", "zh-hans", "zh-hans-cn", "zh-cn"}
_opencc_converter = None
_opencc_failed = False


def to_simplified_if_target(text, target_language):
    """目标语言是简体中文时把译文繁转简；其余语言/空文本原样返回。

    缺 opencc 时**只记一次 warning 并原样返回**，绝不因为这层兜底崩掉整条
    翻译链路（节点上 opencc 是单独装的，落下了也只是不转，不是不能用）。
    """
    global _opencc_converter, _opencc_failed
    if not text or not target_language:
        return text
    if str(target_language).lower() not in _SIMPLIFIED_TARGETS:
        return text
    if _opencc_failed:
        return text
    if _opencc_converter is None:
        try:
            from opencc import OpenCC
            _opencc_converter = OpenCC("t2s")
        except Exception as exc:  # noqa: BLE001 - 可选依赖，缺了也只是不转
            _opencc_failed = True
            _log.warning("opencc 不可用，译文不做繁转简：%s", exc)
            return text
    return _opencc_converter.convert(text)


# 德语时间是"点"分隔（19.10 Uhr = 19:10），模型经常把这个点当小数点或序数点，
# 实测错法有三种：19.10→"晚上九点"（小时算错）、23.30→"十一点"（丢了分钟）、
# 21 .43→"0点43分"（ASR 在数字间插了空格，模型彻底读歪）。
# prompt 里那条"时间是24小时制不要改"的硬约束挡不住，因为歧义在输入端。
# 送去翻译前先归一化成 19:10 Uhr——冒号在任何语言里都只可能是时间，
# 模型没有可误读的余地。只动喂给模型的文本，屏幕上和存档里的德语原文不变。
_DE_CLOCK = re.compile(r'\b([01]?\d|2[0-3])\s*\.\s*\.?\s*([0-5]\d)(?=\s*Uhr\b)')
# 常见德语补充缩写表（仅收录无歧义的非普通词缩写，避免句尾正常词被误判否决）
_GERMAN_EXTRA_ABBREVIATIONS = {
    # 称谓与人名前缀
    "hr", "fr", "fa", "str",
    # 法律、书籍、文献与时间编号
    "kap", "bd", "jh",
    # 常见短语缩写
    "z.bsp", "u.ä", "o.ä", "z.zt", "e.v",
    # 学位与头衔修饰
    "med", "jur", "rer", "theol", "nat",
}

# 常见德语带空格多词缩写（如 "z. B.", "u. a.", "i. d. R."）
_SPACED_ABBREVIATIONS = {
    "z.b", "d.h", "u.a", "z.t", "v.a", "i.d.r", "u.u", "s.o", "s.u", "e.v", "a.d",
}

# 常见德语省略号前不应断句的不完整词（介词、冠词、连词、不完整助动词/情态词）
_INCOMPLETE_BEFORE_ELLIPSIS = {
    "der", "die", "das", "den", "dem", "des", "ein", "eine", "einer", "eines", "einem", "einen",
    "mein", "meine", "dein", "deine", "sein", "seine", "ihr", "ihre", "unser", "unsere", "euer", "eure",
    "in", "an", "auf", "für", "mit", "von", "zu", "bei", "nach", "über", "unter", "vor", "durch", "ohne", "um", "aus",
    "und", "oder", "aber", "dass", "daß", "weil", "wenn", "ob", "als", "wie", "denn", "sowie",
    "haben", "hat", "hatte", "hatten", "habe", "bin", "ist", "sind", "war", "waren", "wird", "werden", "wurde", "wurden",
    "brauchen", "braucht", "brauchte",
    "kann", "können", "muss", "müssen", "will", "wollen", "soll", "sollen", "darf", "dürfen", "mag", "mögen", "werde",
}



def _normalize_clock_times(text):
    """德语 'HH.MM Uhr' / 'HH .MM Uhr' → 'HH:MM Uhr'。非德语时间格式不动。"""
    if not text:
        return text
    return _DE_CLOCK.sub(lambda m: f"{m.group(1)}:{m.group(2)}", text)


def _boundary_is_real(candidate, remainder, final, lang=None):
    """这个句号/问号是真句尾，还是 Whisper 打错的？

    2026-08-02 统计 19 个转录文件共 23526 句：**38.5% 的翻译单元以小写德语词
    开头**——德语句首必大写，所以那都是上一句在非句尾处被切开的碎片。碎片单独
    送翻译会翻错（实测 "Demnach darf, wer schwimmend bzw." 被译成"均不得……"，
    否定词其实在下一段）。三条否决规则：
    """
    # ☠️ 下面三条否决规则全都是**拉丁语系专属**的，对中文一条都不成立：
    #   ① 缩写表是德语的（bzw./z.B.）；
    #   ② 序数否决针对 "am 3. Mai" 这种写法；
    #   ③ 续行否决靠"下一个词小写 = 没说完"，而中文没有大小写——`islower()`
    #      对汉字恒为 False，等于这条规则恒判"是真句尾"，纯属瞎蒙对。
    # 全角 。！？ 本身无歧义，直接判真即可；连"句尾扣留等下一个词"都不用做，
    # 中文字幕因此比德语还快一拍（德语要等 SENTENCE_HOLD_SEC 才能确认）。
    if _no_space_language(lang):
        return True
    tail = candidate.rstrip(_QUOTE_CHARS)
    if tail.endswith("."):
        words = tail[:-1].split()
        token = words[-1] if words else ""
        clean_token = token.lower().strip(_QUOTE_CHARS)
        abbrevs = config.SENTENCE_ABBREVIATIONS | _GERMAN_EXTRA_ABBREVIATIONS

        # ①a 单词缩写否决（德语 bzw./z.B./ca./hr./fr. 等自带句号，含 Schiller-Str. 这类连字符复合）
        token_base = clean_token.rsplit("-", 1)[-1]
        if clean_token in abbrevs or token_base in abbrevs:
            return False

        # ①b 带空格的多词缩写末尾否决（如 "z. B." -> "z.b", "i. d. R." -> "i.d.r"）
        if len(words) >= 2:
            w2 = words[-2].strip(_QUOTE_CHARS).rstrip(".")
            part2 = f"{w2}.{clean_token}".lower()
            if part2 in _SPACED_ABBREVIATIONS or part2 in abbrevs:
                return False
        if len(words) >= 3:
            w3 = words[-3].strip(_QUOTE_CHARS).rstrip(".")
            w2 = words[-2].strip(_QUOTE_CHARS).rstrip(".")
            part3 = f"{w3}.{w2}.{clean_token}".lower()
            if part3 in _SPACED_ABBREVIATIONS or part3 in abbrevs:
                return False

        # ①c 单字母缩写前缀否决（仅当与续行形成已知带空格缩写如 "z. B." 时否决；绝不否决句尾孤立大写单字母如 "Plan B.", "Vitamin D."）
        if len(clean_token) == 1 and remainder:
            m = re.match(r"^([A-Za-zÄÖÜäöü])\.", remainder)
            if m:
                nxt = m.group(1).lower()
                pair = f"{clean_token}.{nxt}"
                if pair in _SPACED_ABBREVIATIONS or pair in abbrevs:
                    return False
                if len(words) >= 2:
                    w2 = words[-2].strip(_QUOTE_CHARS).rstrip(".")
                    triple = f"{w2}.{clean_token}.{nxt}"
                    if triple in _SPACED_ABBREVIATIONS or triple in abbrevs:
                        return False
            m3 = re.match(r"^([A-Za-zÄÖÜäöü])\.\s*([A-Za-zÄÖÜäöü])\.", remainder)
            if m3:
                triple = f"{clean_token}.{m3.group(1).lower()}.{m3.group(2).lower()}"
                if triple in _SPACED_ABBREVIATIONS or triple in abbrevs:
                    return False

        # ② 序数否决："am 3. Mai" 这种日期序数自带句号——注意德语名词首字母
        #    大写，"Mai" 是大写的，规则③抓不到这种，必须单独否决。
        #    ☠️ 只否决 1-2 位数（能当序数/日期的范围）。以前这里是无条件
        #    `token.isdigit()`，于是**四位年份/大数结尾的句子永远不成句**：
        #    "Der Vertrag läuft bis 2030." 连 final=True 都放不出来（这条
        #    return 在 `if not remainder: return final` 之前），只能等
        #    IDLE_FLUSH_SEC 兜底，或者和下一句合并成一个翻译单元——中文因此
        #    整整晚一句。新闻直播是主场景，年份/金额结尾极常见。
        #    四位数几乎不可能是序数，两位数（"Es waren genau 20."）仍按老规则
        #    保守合并，代价只是两句合成一次请求。
        #    注意：三位数（如 100.）不可随意扩充否决，否则 "Das kostet 100." 接新句
        #    大写名词时会造成切句失败（中文同样整整晚一句），故严格维持 1-2 位数范围。
        if token.isdigit() and len(token) <= 2:
            return False

    # 省略号（"…" 或 "..."）句中停顿否决：若前一词是不完整词（介词/冠词/连词等），且后面仍有内容，不提前切句
    if remainder and (tail.endswith("…") or tail.endswith("...")):
        words = tail.rstrip("….").split()
        prev_word = words[-1].lower().strip(_PUNCT_STRIP) if words else ""
        if prev_word in _INCOMPLETE_BEFORE_ELLIPSIS:
            return False

    if not remainder:
        # ③b 句尾扣留：后面还没有词，看不出这个句号是真是假。
        # final=True（收尾/有界放行）时不再等，照常成句
        return final
    # ③a 续行否决：下一个词是小写 = 上一句还没说完
    return not remainder[0].islower()


def _split_sentences(text, final=False, lang=None, lookahead=""):
    """切出完整句子 + 剩余残句。返回 (sentences, rest)。

    模块级纯函数：单测直接测它，不用在测试里复制一份切分逻辑（复制必然漂移）。
    lang 缺省读 config.SOURCE_LANGUAGE；中文/日文走另一套终止符，见
    _SENTENCE_TERMINATOR_CJK 的注释。
    lookahead：仅当候选句的 remainder 为空（正好落在文本末尾）时，用它代替空 remainder
    去调 _boundary_is_real，实现 D1 lookahead（识别轮未稳定首词作为下文判定）。
    绝不拼进句子或 pending_text。无空格语言行为与改前完全相同。
    """
    sentences = []
    start = 0
    for m in _terminator_re(lang).finditer(text):
        end = m.end()
        candidate = text[start:end].strip()
        if not candidate:
            continue
        remainder = text[end:].lstrip()
        # D1 lookahead：当 remainder 为空（句尾正好在文本末尾）且有 lookahead 时，
        # 用 lookahead.lstrip() 做边界判定（只用于判定，不拼进句子）
        if not remainder and lookahead:
            boundary_remainder = lookahead.lstrip()
        else:
            boundary_remainder = remainder
        if not _boundary_is_real(candidate, boundary_remainder, final, lang):
            continue  # 不是真句尾：跳过这个终止符，接着往后找
        sentences.append(candidate)
        start = end
    return sentences, text[start:].strip()


def _pending_too_long(text, lang=None):
    """残句是不是长到该不等标点直接送翻译了（MAX_PENDING_WORDS 的入口）。

    ☠️ 不能一律用 `len(text.split())`：中文没有空格，整段永远等于 1 个"词"，
    这道兜底对中文是**死的**。无空格语言改按字符数（MAX_PENDING_CHARS）。
    """
    if _no_space_language(lang):
        return len(text) > getattr(config, "MAX_PENDING_CHARS", 60)
    return len(text.split()) > config.MAX_PENDING_WORDS


def _batch_max_chars(lang=None):
    """一次翻译请求最多合并多少字符（TRANSLATE_BATCH_MAX_CHARS 的入口）。

    ☠️ 字符不是等价单位：300 个汉字的信息量约等于 750 个德语字符，拿德语的
    上限去量中文，等于每次请求塞进 2.5 倍的内容——生成 token 数、单次延迟、
    跑进复读的概率跟着一起涨，而这三样正是这个上限要压住的东西。
    """
    if _no_space_language(lang):
        return getattr(config, "TRANSLATE_BATCH_MAX_CHARS_CJK", 120)
    return getattr(config, "TRANSLATE_BATCH_MAX_CHARS", 300)


def _draft_too_short(text, lang=None):
    """残句短到不值得出草稿吗（DRAFT_MIN_WORDS 的入口）。

    ☠️ 和上面那条完全同源：`len(text.split())` 对中文恒等于 1，`1 < 3` 永真，
    于是**中文源语言下草稿翻译从来没触发过**。加中→德那轮只改了
    _pending_too_long，这一处漏了。无空格语言按字符数（DRAFT_MIN_CHARS）。
    """
    if _no_space_language(lang):
        return len(text) < getattr(config, "DRAFT_MIN_CHARS", 8)
    return len(text.split()) < getattr(config, "DRAFT_MIN_WORDS", 3)


def _squash_repeats(sentences, keep_words=3, keep_sents=2):
    """压缩Whisper复读伪影（游戏噪音实测提交过"Get. Get. Get. Get. Get. Get."）：
    句内同词连续超过 keep_words 次收敛；一批里连续相同句子超过 keep_sents 条丢弃。
    真人口语的"ja, ja"重复不受影响（阈值3已经很宽）。"""
    out = []
    for s in sentences:
        words = s.split()
        squashed = []
        run = 0
        for w in words:
            if squashed and w.lower() == squashed[-1].lower():
                run += 1
                if run >= keep_words:
                    continue
            else:
                run = 0
            squashed.append(w)
        s = " ".join(squashed)
        if len(out) >= keep_sents and all(x == s for x in out[-keep_sents:]):
            continue
        out.append(s)
    return out
