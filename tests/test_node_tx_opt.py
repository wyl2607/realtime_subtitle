"""节点翻译优化（2026-10-12）：会话开始预热、带上一句上下文、时间规范化、数字一致性重试。

真机背景：节点 Ollama keep_alive=3m，空闲后第一句等 9–15 秒；qwen3.5:4b 把
「zwei Komma eins Prozent」译成 1.2%（两台 M4 都错）。
"""
import threading
import time

import numpy as np

from realtime_subtitle.node import engines
from realtime_subtitle.node.engines import OllamaBackend, source_numbers, target_numbers

from test_node_worker import FakeTr, audio_frames, ctl, hello, run_worker, silence, tone


# ---------------------------------------------------------------- 数字提取


def test_source_numbers_reads_digits_words_and_komma():
    assert source_numbers("auf zwei Komma eins Prozent gesunken") == {"2.1"}
    assert source_numbers("bis zu achtzehn Grad") == {"18"}
    assert source_numbers("1.000 Euro und 2,5 Prozent") == {"1000", "2.5"}
    assert source_numbers("einundzwanzig Menschen") == {"21"}
    assert source_numbers("dreihundert") == {"300"}
    assert source_numbers("um 19:10 Uhr") == {"19", "10"}


def test_source_numbers_ignores_articles():
    assert source_numbers("Ein Mann und eine Frau kaufen einen Hund.") == set()


def test_target_numbers_reads_arabic_and_chinese():
    assert target_numbers("通胀率降至1.2%。") == {"1.2"}
    assert target_numbers("百分之二点一") == {"2.1"}
    assert target_numbers("最高十八度") == {"18"}
    assert target_numbers("1,000欧元") == {"1000"}
    assert target_numbers("七点十分") == {"7", "10"}
    assert target_numbers("二〇二六年") == {"2026"}


# ---------------------------------------------------------------- OllamaBackend


class _Resp:
    def __init__(self, text):
        self._text = text

    def raise_for_status(self):
        pass

    def json(self):
        return {"response": self._text}


class _Session:
    def __init__(self, replies):
        self.replies = list(replies)
        self.bodies = []

    def post(self, url, json=None, timeout=None):
        self.bodies.append(json)
        return _Resp(self.replies.pop(0) if self.replies else "")

    def close(self):
        pass


class _Tq:
    @staticmethod
    def ollama_url():
        return "http://127.0.0.1:11434"


def _backend(replies):
    b = OllamaBackend.__new__(OllamaBackend)
    b._session = _Session(replies)
    b._tq = _Tq()
    b._model = "qwen3.5:4b"
    b.name = "ollama:qwen3.5:4b"
    b._last_used = float("-inf")
    return b


SRC = "Die Inflationsrate ist im September auf zwei Komma eins Prozent gesunken."


def test_number_mismatch_retries_once_and_keeps_fixed_answer():
    b = _backend(["通胀率9月降至1.2%。", "通胀率9月降至2.1%。"])
    assert b.translate(SRC, "de", "zh") == "通胀率9月降至2.1%。"
    assert len(b._session.bodies) == 2
    assert "2.1" in b._session.bodies[1]["prompt"]


def test_number_mismatch_twice_returns_second_answer(caplog):
    b = _backend(["降至1.2%。", "降至1.3%。"])
    with caplog.at_level("WARNING", logger="node.engines"):
        assert b.translate(SRC, "de", "zh") == "降至1.3%。"
    assert len(b._session.bodies) == 2
    assert "tx_number_mismatch" in caplog.text
    assert "Inflation" not in caplog.text  # 日志不带原文


def test_no_numbers_means_single_request():
    b = _backend(["晚上好。"])
    assert b.translate("Guten Abend.", "de", "zh") == "晚上好。"
    assert len(b._session.bodies) == 1


def test_extra_numbers_in_translation_are_fine():
    b = _backend(["5月3日在柏林。"])
    assert b.translate("Am 3. Mai in Berlin.", "de", "zh") == "5月3日在柏林。"
    assert len(b._session.bodies) == 1


def test_context_goes_into_prompt_and_clock_is_normalized():
    b = _backend(["新闻在19:10开始。"])
    b.translate_ctx("Die Nachrichten beginnen um 19.10 Uhr.", "de", "zh",
                    context="Guten Abend, meine Damen und Herren.")
    prompt = b._session.bodies[0]["prompt"]
    assert "上一句（仅供理解，不要翻译它）" in prompt
    assert "Guten Abend, meine Damen und Herren." in prompt
    assert "19:10 Uhr" in prompt and "19.10 Uhr" not in prompt


def test_no_context_section_without_context():
    b = _backend(["你好。"])
    b.translate("Hallo.", "de", "zh")
    assert "上一句" not in b._session.bodies[0]["prompt"]


def test_traditional_output_still_converted():
    b = _backend(["這種概念"])
    assert b.translate("Dieses Konzept", "de", "zh") == "这种概念"


def test_warm_posts_empty_prompt_with_same_num_ctx_and_skips_when_recent():
    b = _backend([""])
    b.warm()
    assert len(b._session.bodies) == 1
    body = b._session.bodies[0]
    assert body["prompt"] == "" and body["keep_alive"] == engines.NODE_OLLAMA_KEEP_ALIVE
    b2 = _backend(["你好。"])
    b2.translate("Hallo.", "de", "zh")
    assert body["options"]["num_ctx"] == b2._session.bodies[0]["options"]["num_ctx"]
    b.warm()  # 刚预热过：不再请求
    assert len(b._session.bodies) == 1


# ---------------------------------------------------------------- worker 接线


class _SlowWarmTr(FakeTr):
    def __init__(self):
        super().__init__()
        self.warm_started = threading.Event()
        self.contexts = []

    def warm(self):
        self.warm_started.set()
        time.sleep(0.5)

    def translate_ctx(self, text, src, dst, context=""):
        self.contexts.append(context)
        return self.translate(text, src, dst)


class _BrokenWarmTr(FakeTr):
    def warm(self):
        raise RuntimeError("ollama down")


def test_worker_warms_in_background_without_delaying_ready():
    tr = _SlowWarmTr()
    t0 = time.perf_counter()
    stream = hello() + audio_frames(np.concatenate([silence(0.5), tone(1.0), silence(1.0)])) + ctl(type="drain")
    code, ev, err, _asr, _tr = run_worker(stream, translator=tr)
    assert tr.warm_started.is_set()
    assert ev[0]["ev"] == "ready"
    assert time.perf_counter() - t0 < 5
    # 第二句拿到第一句原文当上下文；第一句没有
    assert tr.contexts[0] == ""
    assert len(tr.contexts) >= 2 and tr.contexts[1] == tr.calls[0][0]


def test_worker_warm_failure_does_not_break_session():
    stream = hello() + audio_frames(np.concatenate([silence(0.5), tone(1.0), silence(1.0)])) + ctl(type="drain")
    code, ev, err, _asr, tr = run_worker(stream, translator=_BrokenWarmTr())
    assert any(e["ev"] == "translation" for e in ev)
    assert "translator_warm_failed" in err
