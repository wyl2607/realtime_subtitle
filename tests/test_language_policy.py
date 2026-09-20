"""语言对配置只解析一次（审核 B03）。

以前 UI（settings_window.configured_language_pairs）和识别线程
（translator_queue.language_pairs）各写了一份解析：同一份 config_local.py 下
LANGUAGE_PAIRS 为空 + 老的 LANGUAGE_CYCLE，后台有两个语言对、面板却一个按钮
都没有，恢复 SOURCE_LANGUAGE 时 allowed 集合也是空的（改不动语言）。
现在两边都问 realtime_subtitle.language_policy，这个文件盯着"两边必须一致"。
"""
import realtime_subtitle.config as config
from realtime_subtitle import language_policy
from realtime_subtitle.translate import translator_queue as tq
from realtime_subtitle.ui import settings_window as sw


def _set(monkeypatch, **kw):
    for key, val in kw.items():
        monkeypatch.setattr(config, key, val, raising=False)


def test_policy_module_stays_free_of_qt_and_model_deps():
    """它要能被 UI 和识别线程同时 import，所以不许把 Qt/requests 拖进来。

    扫 import 语句（含函数体里的延迟 import，缩进的一样要扫，见 CLAUDE.md
    第 4 节第 26 条），不扫注释——注释里正要写清楚为什么不许 import 它们。
    """
    import ast

    banned = {"PyQt6", "requests", "torch", "faster_whisper",
              "realtime_subtitle.translate", "realtime_subtitle.ui"}
    tree = ast.parse(open(language_policy.__file__, encoding="utf-8").read())
    seen = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            seen.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            seen.add(node.module or "")
            seen.update(f"{node.module or ''}.{a.name}" for a in node.names)
    for name in seen:
        assert not any(name.startswith(bad) for bad in banned), name


def test_ui_and_worker_agree_on_default_config():
    assert sw.configured_language_pairs() == tq.language_pairs()
    assert sw.configured_language_pairs() == language_policy.language_pairs()


def test_ui_and_worker_agree_on_legacy_language_cycle(monkeypatch):
    _set(monkeypatch, LANGUAGE_PAIRS=[], LANGUAGE_CYCLE=["de", "en"],
         TARGET_LANGUAGE="zh")

    assert tq.language_pairs() == [("de", "zh"), ("en", "zh")]
    assert sw.configured_language_pairs() == tq.language_pairs()
    assert sw.pair_target("en") == "zh"


def test_ui_and_worker_agree_when_nothing_is_configured(monkeypatch):
    _set(monkeypatch, LANGUAGE_PAIRS=[], LANGUAGE_CYCLE=None,
         SOURCE_LANGUAGE="de", TARGET_LANGUAGE="zh")

    assert tq.language_pairs() == [("de", "zh")]
    assert sw.configured_language_pairs() == tq.language_pairs()


def test_malformed_pairs_are_dropped_instead_of_raising(monkeypatch):
    _set(monkeypatch, LANGUAGE_PAIRS=[("de", "zh"), ("en",), None, ("", "zh"),
                                      ("zh", ""), ("zh", "de"), ("de", "zh")],
         LANGUAGE_CYCLE=None, TARGET_LANGUAGE="zh")

    assert language_policy.language_pairs() == [("de", "zh"), ("zh", "de")]
    assert sw.configured_language_pairs() == tq.language_pairs()


def test_unknown_source_falls_back_to_target_language(monkeypatch):
    _set(monkeypatch, LANGUAGE_PAIRS=[("de", "zh")], LANGUAGE_CYCLE=None,
         TARGET_LANGUAGE="zh")

    assert tq.target_for("fr") == "zh"
    assert sw.pair_target("fr") == tq.target_for("fr")


def test_allowed_sources_matches_configured_pairs(monkeypatch):
    _set(monkeypatch, LANGUAGE_PAIRS=[], LANGUAGE_CYCLE=["de", "en"],
         TARGET_LANGUAGE="zh")

    assert language_policy.allowed_sources() == {"de", "en"}


def test_apply_tuning_restores_source_language_under_legacy_cycle(monkeypatch):
    """老配置下也要能把上次选的源语言恢复回来（allowed 集合不能是空的）。"""
    _set(monkeypatch, LANGUAGE_PAIRS=[], LANGUAGE_CYCLE=["de", "en"],
         SOURCE_LANGUAGE="de", TARGET_LANGUAGE="zh")

    sw.apply_tuning({"SOURCE_LANGUAGE": "en"})

    assert config.SOURCE_LANGUAGE == "en"
    assert config.TARGET_LANGUAGE == "zh"


def test_apply_tuning_still_rejects_unconfigured_language(monkeypatch):
    _set(monkeypatch, LANGUAGE_PAIRS=[("de", "zh")], LANGUAGE_CYCLE=None,
         SOURCE_LANGUAGE="de", TARGET_LANGUAGE="zh")

    sw.apply_tuning({"SOURCE_LANGUAGE": "fr"})

    assert config.SOURCE_LANGUAGE == "de"
