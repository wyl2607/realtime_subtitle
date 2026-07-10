"""不加载 Whisper/Ollama 的管线辅助逻辑单测。

运行: venv\\Scripts\\python.exe -m pytest test_pipeline_helpers.py -q
  或: venv\\Scripts\\python.exe test_pipeline_helpers.py
"""
import numpy as np

import config
from streaming_asr import HypothesisBuffer, OnlineASRProcessor
from translator_queue import _SENTENCE_END


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
