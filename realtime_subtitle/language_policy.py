"""语言对配置的唯一解析入口（设置面板和识别线程共用）。

☠️ **这个模块不许 import Qt / requests / translator_queue**。它存在的理由就是
让"配置里配了哪些语言对"这件事只有一份答案：以前 UI 和识别线程各写了一份
解析，同一份 config_local.py 下（LANGUAGE_PAIRS 为空 + 老的 LANGUAGE_CYCLE）
后台有两个语言对、面板一个按钮都没有，而且恢复上次选的源语言时 allowed
集合也是空的——用户从面板改不动语言，看不出任何报错。

面板要在 QApplication 之前就能读它，识别线程又不能因为读一份配置把 PyQt5
拉进导入链（CLAUDE.md 第 4 节第 1 条的 import 顺序是生死攸关的），所以这里
只依赖 config。
"""
from realtime_subtitle import config


def _clean_pair(item):
    """把配置里的一项规范成 (source, target)，坏项返回 None。

    LANGUAGE_PAIRS 来自 config_local.py，是人手写的：以前 `[(s, t) for s, t in
    pairs]` 遇到 ("en",) 直接 ValueError，而调用点在面板构造和识别线程里，
    一个手滑的括号会让程序起不来或按钮整排消失。坏项跳过、好项照用。
    """
    if isinstance(item, (str, bytes)) or not isinstance(item, (list, tuple)):
        return None
    if len(item) != 2:
        return None
    source, target = item
    if not isinstance(source, str) or not isinstance(target, str):
        return None
    source, target = source.strip(), target.strip()
    if not source or not target:
        return None
    return (source, target)


def language_pairs():
    """当前生效的「源语言→目标语言」列表，也是 Ctrl+Alt+L 的循环顺序。

    兼容老配置：config_local.py 里可能还写着 LANGUAGE_CYCLE = ["de","en"]
    （只列源语言，那时候目标语言是写死的中文）。那种情况下按 TARGET_LANGUAGE
    补齐成对，不让老配置失效。两者都没有时退回"当前这一对"，这样面板至少
    有一个按钮、且和后台的循环列表一致。
    """
    default_target = getattr(config, "TARGET_LANGUAGE", "zh")
    pairs = []
    for item in (getattr(config, "LANGUAGE_PAIRS", None) or []):
        pair = _clean_pair(item)
        if pair and pair not in pairs:
            pairs.append(pair)
    if pairs:
        return pairs
    for source in (getattr(config, "LANGUAGE_CYCLE", None) or []):
        pair = _clean_pair((source, default_target))
        if pair and pair not in pairs:
            pairs.append(pair)
    if pairs:
        return pairs
    return [(getattr(config, "SOURCE_LANGUAGE", "de"), default_target)]


def target_for(source_language):
    """这个源语言配的目标语言是哪个（查不到就用 TARGET_LANGUAGE）。"""
    for source, target in language_pairs():
        if source == source_language:
            return target
    return getattr(config, "TARGET_LANGUAGE", "zh")


def allowed_sources():
    """自动检测和"恢复上次选择"都只认这些源语言。"""
    return {source for source, _ in language_pairs()}


def allowed_pairs():
    """配置里有哪些「源→目标」组合。恢复上次选择时按**整对**校验。"""
    return set(language_pairs())


def is_explicit_target(source_language, target_language):
    """当前这个目标语言是用户挑的，还是按源语言算出来的默认？

    不额外存一个"用户是否显式选过"的状态位——那种状态很容易和 config 走散。
    判据就是"和默认值不一样"：配了 zh→en、zh→de 两条时 target_for("zh") 是
    en，用户选了 de 就自然被认出来是显式的。
    """
    if not target_language:
        return False
    return target_language != target_for(source_language)


def resolve_target(new_source, current_source=None, current_target=None):
    """自动检测切到 new_source 时，目标语言该是什么。

    ☠️ **自动检测只改源语言，不许覆盖用户显式选过的目标语言。**
    但显式目标也得讲得通：用户在中文下选了德语，自动检测切到德语时再保持
    德语就成了"德语→德语"（识别对了、prompt 还要求输出德语，模型把原句抄
    一遍，见 _apply_pending_lang_switch 的注释）。所以只有当这个显式目标在
    新源语言下**确实配过、且不等于新源语言**时才保留，否则退回默认。
    """
    default = target_for(new_source)
    if not is_explicit_target(current_source, current_target):
        return default
    if current_target == new_source:
        return default
    if (new_source, current_target) in allowed_pairs():
        return current_target
    return default


def language_name(language):
    return (getattr(config, "LANGUAGE_NAMES", None) or {}).get(language, language)


def pair_label(source, target=None):
    """面板按钮和状态行的「德语 → 中文」文案。"""
    tgt = target_for(source) if target is None else target
    return f"{language_name(source)} → {language_name(tgt)}"
