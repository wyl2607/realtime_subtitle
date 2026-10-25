"""译文目标为中文时的繁→简兜底（节点 + 桌面本机 Ollama 共用出口）。

小模型（qwen3.5:4b 等）即使 prompt 里写了"简体中文"仍会偶发繁体，实测例
「Mews的這種概念」。统一入口是 `text_rules.to_simplified_if_target()`：
目标语言是 zh / zh-Hans 才转，de 等原样不动；缺 opencc 只记一次 warning
并原样返回，不崩。
"""
import sys

import realtime_subtitle.config as config
from realtime_subtitle.translate import text_rules
from realtime_subtitle.translate.text_rules import to_simplified_if_target


def _reset_opencc(monkeypatch):
    """每个用例从"还没加载过"开始，免得被上一个用例缓存住影响断言。"""
    monkeypatch.setattr(text_rules, "_opencc_converter", None)
    monkeypatch.setattr(text_rules, "_opencc_failed", False)


def test_traditional_translated_to_simplified(monkeypatch):
    _reset_opencc(monkeypatch)
    assert to_simplified_if_target("Mews的這種概念", "zh") == "Mews的这种概念"


def test_simplified_input_unchanged(monkeypatch):
    _reset_opencc(monkeypatch)
    text = "Mews的这种概念"
    assert to_simplified_if_target(text, "zh") == text


def test_zh_hans_target_converted(monkeypatch):
    _reset_opencc(monkeypatch)
    assert to_simplified_if_target("軟體與網路", "zh-Hans") == "软体与网路"


def test_non_chinese_target_untouched(monkeypatch):
    _reset_opencc(monkeypatch)
    assert to_simplified_if_target("Mews的這種概念", "de") == "Mews的這種概念"
    assert to_simplified_if_target("Mews的這種概念", "en") == "Mews的這種概念"


def test_mixed_content_preserved(monkeypatch):
    """中英数字标点混排只动繁体字，别的原样保留。"""
    _reset_opencc(monkeypatch)
    assert (to_simplified_if_target("Mews的這種概念，2024年10月，CPU/GPT-4！", "zh")
            == "Mews的这种概念，2024年10月，CPU/GPT-4！")


def test_empty_or_missing_target_untouched(monkeypatch):
    _reset_opencc(monkeypatch)
    assert to_simplified_if_target("", "zh") == ""
    assert to_simplified_if_target("這種", None) == "這種"
    assert to_simplified_if_target("這種", "") == "這種"


def test_missing_library_returns_input_and_warns_once(monkeypatch, caplog):
    """opencc 缺失：原样返回、不崩，而且只记一次 warning。"""
    _reset_opencc(monkeypatch)
    # sys.modules 里把 opencc 置成 None → `from opencc import OpenCC` 抛 ImportError
    monkeypatch.setitem(sys.modules, "opencc", None)

    with caplog.at_level("WARNING"):
        assert to_simplified_if_target("Mews的這種概念", "zh") == "Mews的這種概念"
        # 再来一次：失败已粘住，不该再记一条
        assert to_simplified_if_target("軟體", "zh") == "軟體"

    warns = [r for r in caplog.records if "opencc" in r.getMessage()]
    assert len(warns) == 1


def test_node_ollama_output_is_simplified(monkeypatch):
    """节点 Ollama 出口同样生效：目标 zh 转简，目标 de 原样。"""
    import requests

    from realtime_subtitle.node import engines
    from realtime_subtitle.translate import translator_queue as tq

    _reset_opencc(monkeypatch)
    monkeypatch.setattr(config, "ALLOW_REMOTE_OLLAMA", False)
    monkeypatch.setattr(tq, "_assert_local_ollama", lambda url: True)
    monkeypatch.setattr(tq, "ollama_url", lambda: "http://127.0.0.1:59999")

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": "Mews的這種概念"}

    monkeypatch.setattr(requests.Session, "post",
                        lambda self, url, **kw: Resp())
    be = engines.OllamaBackend()
    assert be.translate("Mews' concept.", "de", "zh") == "Mews的这种概念"
    assert be.translate("Mews' concept.", "de", "de") == "Mews的這種概念"
