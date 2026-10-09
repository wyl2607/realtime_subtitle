"""节点 worker：P3 管道协议、事件序列、S1（Ollama 只经本机校验）、S7（日志无正文）。

引擎与翻译都用假实现，不加载模型；真模型冒烟在 TK 验收里单独跑。
"""
import io
import json
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

import realtime_subtitle.config as config
from realtime_subtitle.node import engines, worker as worker_mod
from realtime_subtitle.node.engines import (
    AppleBackend,
    AsrEngine,
    OllamaBackend,
    Translator,
    Utterance,
    WhisperEngine,
    select_translator,
    utterances_from_segments,
)
from realtime_subtitle.node.worker import (
    EXIT_ENGINE,
    EXIT_INTERNAL,
    EXIT_OK,
    EXIT_PROTOCOL,
    MAX_FRAME,
    Worker,
    _log,
)

SR = 16000
REPO = Path(__file__).resolve().parent.parent

# 用来在日志里「抓正文泄露」的哨兵串：含空格、非 ASCII、标点，真实正文长这样
SECRET_SRC = "Geheimer Satz über Äpfel, nummer 42."
SECRET_DST = "机密译文：关于苹果的第四十二句。"


def tone(seconds, amp=0.3):
    t = np.arange(int(seconds * SR)) / SR
    return (amp * np.sin(2 * np.pi * 220 * t) * 32767).astype(np.int16)


def silence(seconds):
    return np.zeros(int(seconds * SR), dtype=np.int16)


class EnergyVad:
    def __call__(self, window):
        return 1.0 if float(np.sqrt((window ** 2).mean())) > 0.02 else 0.0

    def reset(self):
        pass


class FakeAsr(AsrEngine):
    def __init__(self, utterances=None, fail_on=None, load_error=None):
        self.utterances = utterances or [Utterance(0.1, 0.5, SECRET_SRC),
                                         Utterance(0.6, 0.9, "Zweiter Satz.")]
        self.loads = 0
        self.calls = 0
        self.fail_on = fail_on
        self.load_error = load_error
        self.seen_lang = []

    def load(self):
        self.loads += 1
        if self.load_error:
            raise self.load_error

    def transcribe(self, audio, language):
        self.calls += 1
        self.seen_lang.append(language)
        if self.fail_on == self.calls:
            raise RuntimeError(f"boom {SECRET_SRC}")
        return list(self.utterances)

    def info(self):
        return {"model": "fake", "backend": "fake", "rtf": 0.1}


class FakeTr(Translator):
    name = "fake"

    def __init__(self, result=SECRET_DST):
        self.result = result
        self.calls = []
        self.closed = False

    def translate(self, text, src, dst):
        self.calls.append((text, src, dst))
        return self.result

    def close(self):
        self.closed = True


def ctl(**msg):
    return (json.dumps(msg) + "\n").encode()


def hello(**over):
    msg = {"type": "hello", "v": 2, "src": "de", "dst": "zh",
           "sample_rate": 16000, "format": "s16le"}
    msg.update(over)
    return ctl(**msg)


def frame(pcm):
    data = pcm.astype("<i2").tobytes()
    return len(data).to_bytes(4, "big") + data


def audio_frames(pcm, chunk=1600):
    return b"".join(frame(pcm[i:i + chunk]) for i in range(0, len(pcm), chunk))


def run_worker(stream, asr=None, translator=None, factory=None, vad=None):
    out, err = io.BytesIO(), io.StringIO()
    asr = asr or FakeAsr()
    tr = translator if translator is not None else FakeTr()
    w = Worker(io.BytesIO(stream), out, asr, vad or EnergyVad(),
               translator_factory=factory or (lambda s, d: tr), err=err)
    code = w.run()
    events = [json.loads(line) for line in out.getvalue().decode().splitlines()]
    return code, events, err.getvalue(), asr, tr


# ---------------------------------------------------------------- 事件序列


def test_hello_audio_drain_event_sequence():
    stream = (hello() + audio_frames(np.concatenate([silence(0.5), tone(1.0), silence(1.0)]))
              + ctl(type="drain"))
    code, ev, _err, asr, tr = run_worker(stream)
    assert code == EXIT_OK
    assert ev[0]["ev"] == "ready" and ev[0]["cold"] is True
    assert ev[0]["engine"]["translator"] == "fake" and ev[0]["engine"]["model"] == "fake"
    finals = [e for e in ev if e["ev"] == "final"]
    trans = [e for e in ev if e["ev"] == "translation"]
    assert [e["id"] for e in finals] == [1, 2]
    assert [e["id"] for e in trans] == [1, 2]
    assert ev[-1] == {"ev": "drained"}
    # drained 之前，final 与 translation 都已发完
    assert ev.index(ev[-1]) > max(ev.index(e) for e in finals + trans)
    # 某句的 translation 不会早于它自己的 final
    for f in finals:
        t = next(e for e in trans if e["id"] == f["id"])
        assert ev.index(t) > ev.index(f)
    assert finals[0]["text"] == SECRET_SRC and trans[0]["text"] == SECRET_DST
    assert tr.calls[0] == (SECRET_SRC, "de", "zh")
    assert asr.seen_lang == ["de"]


def test_final_times_are_session_seconds_from_sample_count():
    # 语音从 0.512s（16 个 VAD 窗口）起，前垫 0.1s → audio_t0 = 0.412
    pcm = np.concatenate([silence(0.512), tone(1.024), silence(1.0)])
    _c, ev, *_ = run_worker(hello() + audio_frames(pcm) + ctl(type="drain"))
    f1, f2 = [e for e in ev if e["ev"] == "final"]
    assert f1["a0"] == pytest.approx(0.412 + 0.1, abs=0.01)
    assert f1["a1"] == pytest.approx(0.412 + 0.5, abs=0.01)
    assert f2["a0"] == pytest.approx(0.412 + 0.6, abs=0.01)
    assert f2["a1"] == pytest.approx(0.412 + 0.9, abs=0.01)
    assert f1["a1"] <= f2["a0"]


def test_utterance_times_are_clamped_into_the_unpadded_speech_span():
    # 夹到不含垫的 [seg.a0, seg.a1]（语音 0.0–1.0s），而不是含垫的音频边界（-0.0–1.2s）
    asr = FakeAsr([Utterance(-5.0, 99.0, "x")])
    pcm = np.concatenate([tone(1.0), silence(1.0)])
    _c, ev, *_ = run_worker(hello() + audio_frames(pcm) + ctl(type="drain"), asr=asr)
    f = next(e for e in ev if e["ev"] == "final")
    assert f["a0"] == pytest.approx(0.0, abs=0.04)
    assert f["a1"] == pytest.approx(1.0, abs=0.04)
    assert 0.0 <= f["a0"] <= f["a1"] <= 1.0 + 0.04


class SpanAsr(FakeAsr):
    """每段报一句覆盖整段音频的话（含垫），最糟情况：模型把垫也算进去。"""

    def transcribe(self, audio, language):
        self.calls += 1
        return [Utterance(-1.0, len(audio) / SR + 1.0, "x")]


def test_force_cut_finals_are_monotonic_non_overlapping_and_within_speech():
    # 20s 连续说话（13.0s 处有短凹口让强切落在那里），说完 1s 静音
    dip = int(13.0 * SR)
    voice = tone(20.0)
    voice[dip:dip + SR // 10] //= 20
    pcm = np.concatenate([voice, silence(1.0)])
    _c, ev, *_ = run_worker(hello() + audio_frames(pcm) + ctl(type="drain"), asr=SpanAsr())
    finals = [e for e in ev if e["ev"] == "final"]
    assert len(finals) == 2
    for a, b in zip(finals, finals[1:]):
        assert a["a0"] <= a["a1"] <= b["a0"] <= b["a1"]  # 单调且不重叠
    assert finals[0]["a0"] >= -0.001
    assert finals[-1]["a1"] <= 20.0 + 0.04  # 不越过语音终点


def test_flush_closes_running_segment():
    stream = hello() + audio_frames(tone(1.0)) + ctl(type="flush") + ctl(type="drain")
    _c, ev, *_ = run_worker(stream)
    assert len([e for e in ev if e["ev"] == "final"]) == 2
    assert ev[-1] == {"ev": "drained"}


def test_drain_with_nothing_pending_replies_immediately():
    _c, ev, *_ = run_worker(hello() + ctl(type="drain"))
    assert [e["ev"] for e in ev] == ["ready", "drained"]


def test_second_hello_resets_ids_and_stays_warm():
    one = audio_frames(np.concatenate([tone(1.0), silence(1.0)])) + ctl(type="drain")
    _c, ev, _e, asr, _t = run_worker(hello() + one + hello() + one)
    readies = [e for e in ev if e["ev"] == "ready"]
    assert [r["cold"] for r in readies] == [True, False]
    assert asr.loads == 1
    ids = [e["id"] for e in ev if e["ev"] == "final"]
    assert ids == [1, 2, 1, 2]


def test_eof_is_normal_exit_without_events():
    code, ev, *_ = run_worker(b"")
    assert code == EXIT_OK and ev == []


# ---------------------------------------------------------------- 降级路径


def test_translator_unavailable_still_delivers_finals():
    def boom(src, dst):
        raise RuntimeError(SECRET_SRC)

    stream = hello() + audio_frames(np.concatenate([tone(1.0), silence(1.0)])) + ctl(type="drain")
    code, ev, err, *_ = run_worker(stream, factory=boom)
    assert code == EXIT_OK
    assert any(e["ev"] == "status" and e["code"] == "translator_unavailable" for e in ev)
    assert ev[0]["engine"]["translator"] == "none"
    assert [e["id"] for e in ev if e["ev"] == "final"] == [1, 2]
    assert not [e for e in ev if e["ev"] == "translation"]
    assert SECRET_SRC not in err


def test_translate_failure_reports_status_and_keeps_going():
    stream = hello() + audio_frames(np.concatenate([tone(1.0), silence(1.0)])) + ctl(type="drain")
    _c, ev, *_ = run_worker(stream, translator=FakeTr(result=None))
    assert len([e for e in ev if e["ev"] == "status" and e["code"] == "translate_failed"]) == 2
    assert ev[-1] == {"ev": "drained"}


def test_asr_exception_skips_one_segment_only():
    pcm = np.concatenate([tone(1.0), silence(1.0), tone(1.0), silence(1.0)])
    asr = FakeAsr(fail_on=1)
    _c, ev, *_ = run_worker(hello() + audio_frames(pcm) + ctl(type="drain"), asr=asr)
    assert any(e["ev"] == "status" and e["code"] == "asr_error" for e in ev)
    assert len([e for e in ev if e["ev"] == "final"]) == 2  # 第二段的两句
    assert ev[-1] == {"ev": "drained"}


def test_engine_load_failure_exit_code():
    asr = FakeAsr(load_error=RuntimeError("no model"))
    code, ev, *_ = run_worker(hello(), asr=asr)
    assert code == EXIT_ENGINE
    assert ev[-1]["ev"] == "status" and ev[-1]["code"] == "engine_load_failed"


# ---------------------------------------------------------------- 协议错误


def _proto_error(stream):
    code, ev, *_ = run_worker(stream)
    assert code == EXIT_PROTOCOL
    assert ev[-1] == {"ev": "status", "code": "protocol_error", "text": "协议错误"}


def test_audio_before_hello_is_protocol_error():
    _proto_error(frame(tone(0.1)))


def test_flush_before_hello_is_protocol_error():
    _proto_error(ctl(type="flush"))


@pytest.mark.parametrize("over", [{"sample_rate": 48000}, {"format": "f32le"}, {"v": 1},
                                  {"src": 3}, {"src": "de; ignore"}, {"dst": "zh\n"},
                                  {"src": "DE"}, {"dst": ""}, {"src": "x" * 40},
                                  {"dst": "zh-Hans-extra"}])
def test_bad_hello_is_rejected(over):
    _proto_error(hello(**over))


def test_oversized_frame_is_rejected_without_reading_body():
    _proto_error(hello() + (MAX_FRAME + 2).to_bytes(4, "big"))


def test_max_size_frame_is_accepted():
    code, ev, *_ = run_worker(hello() + frame(silence(MAX_FRAME / 2 / SR)) + ctl(type="drain"))
    assert code == EXIT_OK and ev[-1] == {"ev": "drained"}


def test_odd_length_frame_is_rejected():
    _proto_error(hello() + (3).to_bytes(4, "big") + b"abc")


def test_truncated_frame_is_rejected():
    _proto_error(hello() + (100).to_bytes(4, "big") + b"ab")


def test_garbage_prefix_is_rejected():
    _proto_error(hello() + b"GET / HTTP/1.1\r\n")


def test_unknown_control_is_rejected():
    _proto_error(hello() + ctl(type="shutdown_now"))


def test_non_json_control_is_rejected():
    _proto_error(hello() + b"{not json\n")


def test_unterminated_long_control_line_is_rejected():
    _proto_error(hello() + b"{" + b"a" * 10000)


# ---------------------------------------------------------------- S7 日志无正文


def test_log_never_contains_transcript_or_translation_text():
    # ASR 异常消息、翻译异常消息里也夹带正文：日志只许出现异常类名
    class LeakyTr(FakeTr):
        def translate(self, text, src, dst):
            raise ValueError(SECRET_DST)

    pcm = np.concatenate([tone(1.0), silence(1.0), tone(1.0), silence(1.0)])
    stream = hello() + audio_frames(pcm) + ctl(type="drain")
    _c, ev, err, *_ = run_worker(stream, asr=FakeAsr(fail_on=1), translator=LeakyTr())
    assert err.strip(), "日志不应为空，否则这条测试什么也没证明"
    for secret in (SECRET_SRC, SECRET_DST, "Zweiter", "Äpfel", "机密", "boom"):
        assert secret not in err
    # 事件流里则必须有正文（它们是产品输出，不是日志）
    assert any(SECRET_SRC in json.dumps(e, ensure_ascii=False) for e in ev)


def test_log_masks_free_text_values():
    buf = io.StringIO()
    _log(buf, "x", ok="asr_error", n=3, f=1.5, bad="Hallo Welt über", worse="机密")
    line = buf.getvalue()
    assert "ok=asr_error" in line and "n=3" in line and "f=1.500" in line
    assert "Hallo" not in line and "机密" not in line
    assert "bad=?" in line and "worse=?" in line


# ---------------------------------------------------------------- S1 / 翻译选择


def _stub_tq(monkeypatch, assert_result=True, raises=None):
    from realtime_subtitle.translate import translator_queue as tq

    calls = SimpleNamespace(asserted=[], urls=[])

    def fake_assert(base_url):
        calls.asserted.append(base_url)
        if raises:
            raise raises
        return assert_result

    monkeypatch.setattr(tq, "_assert_local_ollama", fake_assert)
    monkeypatch.setattr(tq, "ollama_url", lambda: "http://127.0.0.1:59999")
    return tq, calls


def test_ollama_backend_asserts_local_on_construction(monkeypatch):
    _tq, calls = _stub_tq(monkeypatch)
    be = OllamaBackend()
    assert calls.asserted == [config.OLLAMA_BASE_URL]
    assert be.name == f"ollama:{config.OLLAMA_MODEL}"


def test_ollama_backend_refuses_remote(monkeypatch):
    from realtime_subtitle.translate import translator_queue as tq

    _stub_tq(monkeypatch, raises=tq.RemoteOllamaRefused("remote"))
    with pytest.raises(tq.RemoteOllamaRefused):
        OllamaBackend()


def test_ollama_backend_refuses_unverified(monkeypatch):
    _stub_tq(monkeypatch, assert_result=False)
    with pytest.raises(RuntimeError):
        OllamaBackend()


def test_ollama_requests_use_ollama_url_not_config(monkeypatch):
    import requests

    _stub_tq(monkeypatch)
    monkeypatch.setattr(config, "OLLAMA_BASE_URL", "http://evil.example:11434")
    seen = []

    class Resp:
        def raise_for_status(self):
            pass

        def json(self):
            return {"response": " 你好 "}

    def fake_post(url, **kw):
        seen.append(url)
        return Resp()

    monkeypatch.setattr(requests.Session, "post", lambda self, url, **kw: fake_post(url, **kw))
    assert OllamaBackend().translate("Hallo", "de", "zh") == "你好"
    assert seen == ["http://127.0.0.1:59999/api/generate"]


def test_ollama_backend_refuses_when_remote_allowed(monkeypatch):
    _tq, calls = _stub_tq(monkeypatch)
    monkeypatch.setattr(config, "ALLOW_REMOTE_OLLAMA", True)
    with pytest.raises(RuntimeError):
        OllamaBackend()
    assert calls.asserted == []  # 在校验之前就拒绝


def test_ollama_backend_ignores_proxy_environment(monkeypatch):
    import requests

    _stub_tq(monkeypatch)
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setenv("http_proxy", "http://proxy.invalid:3128")
    be = OllamaBackend()
    assert isinstance(be._session, requests.Session)
    assert be._session.trust_env is False
    # 即使环境里有代理，这个 session 为回环请求解析出的代理也必须为空
    kw = be._session.merge_environment_settings("http://127.0.0.1:59999/api/generate",
                                                {}, None, None, None)
    assert not kw["proxies"]


def test_engines_source_only_touches_ollama_base_url_for_the_assert():
    # 用 AST 数真正的属性访问，注释/文档字符串里的提及不算
    import ast

    src = (REPO / "realtime_subtitle/node/engines.py").read_text(encoding="utf-8")
    uses = [n for n in ast.walk(ast.parse(src))
            if isinstance(n, ast.Attribute) and n.attr == "OLLAMA_BASE_URL"]
    assert len(uses) == 1
    assert "_assert_local_ollama(config.OLLAMA_BASE_URL)" in src
    tree = ast.parse(src)
    url_calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
                 and isinstance(n.func, ast.Attribute) and n.func.attr == "ollama_url"]
    api_urls = [n for n in ast.walk(tree) if isinstance(n, ast.JoinedStr)
                and any(isinstance(v, ast.Constant) and "/api/" in str(v.value)
                        for v in n.values)]
    assert api_urls and len(url_calls) == len(api_urls)


def test_node_does_not_use_whisper_queue_translator():
    for name in ("worker.py", "engines.py", "segmenter.py"):
        src = (REPO / "realtime_subtitle/node" / name).read_text(encoding="utf-8")
        assert "import WhisperQueueTranslator" not in src
        assert "WhisperQueueTranslator(" not in src


class _Apple:
    def __init__(self, status):
        self._status = status
        self.closed = False

    def status(self, src, dst):
        return self._status

    def close(self):
        self.closed = True


def test_select_translator_prefers_apple_when_installed():
    apple = _Apple("installed")
    tr = select_translator("de", "zh", apple_factory=lambda: apple,
                           ollama_factory=lambda: pytest.fail("不该走 Ollama"))
    assert isinstance(tr, AppleBackend) and tr.name == "apple"


@pytest.mark.parametrize("status", ["supported", "unsupported", None])
def test_select_translator_falls_back_to_ollama(status):
    apple = _Apple(status)
    sentinel = FakeTr()
    tr = select_translator("de", "zh", apple_factory=lambda: apple,
                           ollama_factory=lambda: sentinel)
    assert tr is sentinel and apple.closed


# ---------------------------------------------------------------- 识别引擎


def W(start, end, word):
    return SimpleNamespace(start=start, end=end, word=word)


def S(text, words, no_speech=0.0):
    return SimpleNamespace(text=text, words=words, no_speech_prob=no_speech)


def test_utterances_split_sentences_with_word_times():
    seg = S("Das ist gut. Wir gehen jetzt.", [
        W(0.0, 0.3, " Das"), W(0.3, 0.5, " ist"), W(0.5, 1.0, " gut."),
        W(1.2, 1.5, " Wir"), W(1.5, 1.8, " gehen"), W(1.8, 2.4, " jetzt."),
    ])
    got = utterances_from_segments([seg], "de")
    assert [(u.text, u.start, u.end) for u in got] == [
        ("Das ist gut.", 0.0, 1.0), ("Wir gehen jetzt.", 1.2, 2.4)]


def test_utterance_spanning_two_whisper_segments_is_one_sentence():
    a = S("Das ist", [W(0.0, 0.3, " Das"), W(0.3, 0.6, " ist")])
    b = S("sehr gut.", [W(0.7, 1.0, " sehr"), W(1.0, 1.4, " gut.")])
    got = utterances_from_segments([a, b], "de")
    assert [(u.text, u.start, u.end) for u in got] == [("Das ist sehr gut.", 0.0, 1.4)]


def test_trailing_text_without_terminator_is_kept():
    seg = S("Und dann", [W(0.0, 0.2, " Und"), W(0.2, 0.5, " dann")])
    assert [u.text for u in utterances_from_segments([seg], "de")] == ["Und dann"]


def test_hallucination_and_no_speech_segments_are_dropped():
    junk = S("Untertitelung des ZDF, 2020", [W(0.0, 1.0, " Untertitelung")])
    quiet = S("Hallo.", [W(0.0, 0.5, " Hallo.")], no_speech=0.95)
    real = S("Hallo.", [W(1.0, 1.5, " Hallo.")])
    got = utterances_from_segments([junk, quiet, real], "de")
    assert [(u.text, u.start) for u in got] == [("Hallo.", 1.0)]


def test_chinese_words_have_no_inserted_spaces():
    seg = S("你好。再见。", [W(0.0, 0.3, " 你"), W(0.3, 0.6, "好。"),
                            W(0.8, 1.0, " 再"), W(1.0, 1.3, "见。")])
    assert [u.text for u in utterances_from_segments([seg], "zh")] == ["你好。", "再见。"]


def test_whisper_engine_calls_model_without_second_vad():
    calls = {}

    class Model:
        def transcribe(self, audio, **kw):
            calls.update(kw)
            return iter([S("Hallo.", [W(0.0, 0.4, " Hallo.")])]), None

    eng = WhisperEngine()
    eng._model = Model()
    eng._backend, eng._model_name = "mlx", "m"
    out = eng.transcribe(np.zeros(SR, dtype=np.float32), "de")
    assert [u.text for u in out] == ["Hallo."]
    assert calls["vad_filter"] is False and calls["word_timestamps"] is True
    assert calls["language"] == "de" and calls["condition_on_previous_text"] is False
    info = eng.info()
    assert info["backend"] == "mlx" and info["model"] == "m" and info["rtf"] is not None


def test_engines_module_is_importable_without_mlx_or_models():
    assert engines.Utterance(0, 1, "x").text == "x"


# ---------------------------------------------------------------- 真子进程：fd 1 保护与 SIGTERM

_CHILD = r"""
import sys
import numpy as np
from realtime_subtitle.node import engines, segmenter, worker


class A(engines.AsrEngine):
    def load(self):
        print("noise from a library on stdout")  # 不能混进事件流
    def transcribe(self, audio, language):
        return [engines.Utterance(0.1, 0.5, "Hallo Welt.")]
    def info(self):
        return {"model": "fake", "backend": "fake", "rtf": None}


class Tr(engines.Translator):
    name = "fake"
    def translate(self, text, src, dst):
        return "你好世界。"


class V:
    def __call__(self, w):
        return 1.0 if float(np.sqrt((w ** 2).mean())) > 0.02 else 0.0
    def reset(self):
        pass


engines.WhisperEngine = A
segmenter.SileroVad = V
worker.select_translator = lambda s, d: Tr()
worker.main()
"""


def _spawn():
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.Popen([sys.executable, "-c", _CHILD], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(REPO), env=env)


def test_subprocess_stdout_carries_only_json_events():
    p = _spawn()
    pcm = np.concatenate([tone(1.0), silence(1.0)])
    out, err = p.communicate(hello() + audio_frames(pcm) + ctl(type="drain"), timeout=60)
    assert p.returncode == 0
    events = [json.loads(line) for line in out.decode().splitlines()]  # 混入噪声会在这里炸
    assert [e["ev"] for e in events if e["ev"] != "translation"] == ["ready", "final", "drained"]
    # S-F1：fd 1 / fd 2 都已指向 /dev/null，第三方库的 print 哪儿都不会出现
    assert b"noise from a library" not in err and b"noise from a library" not in out
    assert b"[node.worker] ready" in err  # 真 stderr 仍能写日志（正对照）
    assert "Hallo Welt" not in err.decode()


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 的 terminate() 是 TerminateProcess，没有 SIGTERM 处理函数可测")
def test_subprocess_exits_zero_on_sigterm():
    p = _spawn()
    p.stdin.write(hello())
    p.stdin.flush()
    ready = threading.Event()

    def wait_ready():
        p.stdout.readline()
        ready.set()

    threading.Thread(target=wait_ready, daemon=True).start()
    assert ready.wait(30)
    p.send_signal(signal.SIGTERM)
    assert p.wait(10) == 0
    p.stdin.close()
    p.stdout.close()
    p.stderr.close()


def test_main_module_is_runnable_entry():
    assert callable(worker_mod.main)


# ---------------------------------------------------------------- CR-002 第 1 轮修复


def _wait(pred, timeout=10.0):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


def _events(out):
    return [json.loads(line) for line in out.getvalue().decode().splitlines()]


def test_asr_backlog_is_bounded_and_reports_status():
    gate = threading.Event()

    class SlowAsr(FakeAsr):
        def transcribe(self, audio, language):
            gate.wait(30)
            return [Utterance(0.0, 0.2, "x")]

    n_seg = worker_mod.MAX_ASR_BACKLOG * 3
    pcm = np.concatenate([np.concatenate([tone(0.5), silence(0.8)]) for _ in range(n_seg)])
    out, err = io.BytesIO(), io.StringIO()
    w = Worker(io.BytesIO(hello() + audio_frames(pcm)), out, SlowAsr(), EnergyVad(),
               translator_factory=lambda s, d: FakeTr(), err=err)
    try:
        assert w.run() == EXIT_OK
        assert w._asr_q.qsize() <= worker_mod.MAX_ASR_BACKLOG
        st = [e for e in _events(out) if e["ev"] == "status" and e["code"] == "backlog_dropped"]
        assert st and st[0]["text"] == "处理积压，已丢弃最旧内容"
        assert "backlog_dropped" in err.getvalue() and "dropped=" in err.getvalue()
    finally:
        gate.set()
    # 丢弃时 task_done 补了账：放行后 join 能返回，不会永远卡住
    assert _wait(lambda: w._asr_q.unfinished_tasks == 0)


def test_tx_backlog_is_bounded_and_reports_status():
    gate = threading.Event()
    entered = threading.Event()

    class SlowTr(FakeTr):
        def translate(self, text, src, dst):
            entered.set()
            gate.wait(30)
            return "译"

    many = [Utterance(0.0, 0.2, f"Satz {i}.") for i in range(10)]
    pcm = np.concatenate([np.concatenate([tone(0.5), silence(0.8)]) for _ in range(12)])
    out, err = io.BytesIO(), io.StringIO()
    w = Worker(io.BytesIO(hello() + audio_frames(pcm) + ctl(type="flush")), out,
               FakeAsr(many), EnergyVad(), translator_factory=lambda s, d: SlowTr(), err=err)
    try:
        assert w.run() == EXIT_OK
        assert entered.wait(10)
        # 12 段 × 10 句 = 120 句 > 上限；等 asr 线程把它们都处理完
        assert _wait(lambda: w._asr_q.unfinished_tasks == 0)
        assert w._tx_q.qsize() <= worker_mod.MAX_TX_BACKLOG
        assert any(e["ev"] == "status" and e["code"] == "backlog_dropped" for e in _events(out))
    finally:
        gate.set()
    assert _wait(lambda: w._tx_q.unfinished_tasks == 0)


def test_stale_translation_is_not_attributed_to_the_next_session():
    release = threading.Event()
    entered = threading.Event()

    class GatedTr(FakeTr):
        def translate(self, text, src, dst):
            entered.set()
            release.wait(30)
            return "旧会话译文"

    out, err = io.BytesIO(), io.StringIO()
    tr = GatedTr()
    w = Worker(io.BytesIO(b""), out, FakeAsr([Utterance(0.0, 0.2, "Eins.")]), EnergyVad(),
               translator_factory=lambda s, d: tr, err=err)
    msg = json.loads(hello())
    try:
        assert w._on_hello(msg) is None
        w._on_audio(np.concatenate([tone(1.0), silence(1.0)]).astype("<i2").tobytes())
        assert entered.wait(10)  # 旧会话的翻译正卡在 translator 里
        assert w._on_hello(msg) is None  # 同语言对的新会话：translator 保留，id 重新从 1 计
    finally:
        release.set()
    assert _wait(lambda: w._tx_q.unfinished_tasks == 0)
    ev = _events(out)
    second_ready = [i for i, e in enumerate(ev) if e["ev"] == "ready"][1]
    assert not [e for e in ev[second_ready:] if e["ev"] in ("translation", "status")]


def test_event_writes_survive_partial_writes():
    class Dribble:
        def __init__(self):
            self.buf = bytearray()

        def write(self, view):
            chunk = bytes(view[:3])
            self.buf += chunk
            return len(chunk)

        def flush(self):
            pass

    out = Dribble()
    pcm = np.concatenate([tone(1.0), silence(1.0)])
    w = Worker(io.BytesIO(hello() + audio_frames(pcm) + ctl(type="drain")), out, FakeAsr(),
               EnergyVad(), translator_factory=lambda s, d: FakeTr(), err=io.StringIO())
    assert w.run() == EXIT_OK
    lines = bytes(out.buf).decode("utf-8").splitlines()
    evs = [json.loads(line) for line in lines]  # 任何一帧被截断都会在这里炸
    assert evs[0]["ev"] == "ready" and evs[-1] == {"ev": "drained"}
    assert [e["text"] for e in evs if e["ev"] == "final"][0] == SECRET_SRC


def test_failed_translator_construction_is_retried_on_same_pair_hello():
    calls = []

    def factory(src, dst):
        calls.append((src, dst))
        if len(calls) < 3:
            raise RuntimeError(SECRET_SRC)
        return FakeTr()

    _c, ev, *_ = run_worker(hello() + hello() + hello(), factory=factory)
    assert len(calls) == 3
    notes = [e for e in ev if e["ev"] == "status" and e["code"] == "translator_unavailable"]
    assert len(notes) == 2
    readies = [e for e in ev if e["ev"] == "ready"]
    assert [r["engine"]["translator"] for r in readies] == ["none", "none", "fake"]


def test_lone_surrogate_in_text_does_not_drop_the_sentence():
    asr = FakeAsr([Utterance(0.0, 0.5, "ab\ud800cd.")])
    pcm = np.concatenate([tone(1.0), silence(1.0)])
    code, ev, *_ = run_worker(hello() + audio_frames(pcm) + ctl(type="drain"), asr=asr,
                              translator=FakeTr(result="译\udc00文"))
    assert code == EXIT_OK
    final = next(e for e in ev if e["ev"] == "final")
    assert final["text"].startswith("ab") and final["text"].endswith("cd.")
    assert any(e["ev"] == "translation" for e in ev)


# S-F1：真 AppleTranslator + 吐正文的假 helper，以子进程方式跑 worker

_CHILD_APPLE = r"""
import os, subprocess, sys
import numpy as np
from realtime_subtitle.node import engines, segmenter, worker
from realtime_subtitle.translate.apple_translate import AppleTranslator

SECRET = os.environ["LEAK_SECRET"]


class A(engines.AsrEngine):
    def load(self):
        print(SECRET, file=sys.stderr)                      # 第三方库 print 到 stderr
        print(SECRET)                                       # ……到 stdout
        subprocess.run([sys.executable, "-c",               # 继承 fd 2 的子进程
                        "import sys; print(sys.argv[1], file=sys.stderr)", SECRET])
    def transcribe(self, audio, language):
        return [engines.Utterance(0.1, 0.5, "Hallo Welt.")]
    def info(self):
        return {"model": "fake", "backend": "fake", "rtf": None}


class V:
    def __call__(self, w):
        return 1.0 if float(np.sqrt((w ** 2).mean())) > 0.02 else 0.0
    def reset(self):
        pass


engines.WhisperEngine = A
segmenter.SileroVad = V
orig = engines.select_translator
worker.select_translator = lambda s, d: orig(
    s, d, apple_factory=lambda: AppleTranslator(helper_path=os.environ["FAKE_HELPER"]))
worker.main()
"""

_FAKE_HELPER = """#!{py}
import json, sys
SECRET = {secret!r}
print(json.dumps({{"ready": True}}), flush=True)
for line in sys.stdin:
    req = json.loads(line)
    if req["op"] == "status":
        print(json.dumps({{"id": req["id"], "status": "installed"}}), flush=True)
        continue
    open({marker!r}, "w").write("translate")
    print("kaputt " + SECRET, flush=True)                      # 非 JSON 行
    print(SECRET, file=sys.stderr, flush=True)                 # helper 自己的 stderr
    print(json.dumps({{"id": req["id"], "error": SECRET}}), flush=True)  # error 字段
"""


@pytest.mark.skipif(sys.platform == "win32", reason="假 rstranslate helper 靠 shebang 启动，Windows 上 exec 不了")
def test_subprocess_apple_helper_output_never_reaches_stderr(tmp_path):
    secret = "GEHEIM-SATZ-4711-NICHT-LOGGEN"
    marker = tmp_path / "translate-called"
    helper = tmp_path / "rstranslate"
    helper.write_text(_FAKE_HELPER.format(py=sys.executable, secret=secret,
                                          marker=str(marker)), encoding="utf-8")
    helper.chmod(0o755)
    script = tmp_path / "child_apple.py"
    script.write_text(_CHILD_APPLE, encoding="utf-8")
    env = dict(os.environ, PYTHONPATH=str(REPO), LEAK_SECRET=secret, FAKE_HELPER=str(helper))
    p = subprocess.Popen([sys.executable, str(script)], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(REPO), env=env)
    pcm = np.concatenate([tone(1.0), silence(1.0)])
    out, err = p.communicate(hello() + audio_frames(pcm) + ctl(type="drain"), timeout=60)
    assert p.returncode == 0
    assert marker.exists(), "假 helper 没被真 AppleTranslator 调到，这条测试什么也没证明"
    events = [json.loads(line) for line in out.decode().splitlines()]
    assert any(e["ev"] == "status" and e["code"] == "translate_failed" for e in events)
    assert b"[node.worker]" in err  # 正对照：日志通道本身是通的
    assert secret.encode() not in err
    assert secret.encode() not in out


# ---------------------------------------------------------------- CR-002 第 2 轮 R2-D1：崩溃留痕

_SECRET = "SECRET-TRANSCRIPT-TEXT"

_CHILD_FAIL = r"""
import sys, threading
from realtime_subtitle.node import engines, segmenter, worker

mode = sys.argv[1]


class A(engines.AsrEngine):
    def __init__(self):
        if mode == "ctor":
            raise ValueError("%(s)s")
        import faulthandler
        if mode == "fh":
            assert faulthandler.is_enabled()
            raise SystemExit(0)
    def load(self): pass
    def transcribe(self, audio, language): return []
    def info(self): return {}


class V:
    def __call__(self, w): return 0.0
    def reset(self): pass


def boom():
    raise KeyError("%(s)s")


def run_and_crash(self):
    t = threading.Thread(target=boom, name="w1")
    t.start(); t.join()
    raise ZeroDivisionError("%(s)s")


if mode == "thread":
    worker.Worker.run = run_and_crash

engines.WhisperEngine = A
segmenter.SileroVad = V
worker.main()
""" % {"s": _SECRET}


def _run_fail(mode):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    p = subprocess.run([sys.executable, "-c", _CHILD_FAIL, mode], input=b"",
                       capture_output=True, cwd=str(REPO), env=env, timeout=60)
    return p.returncode, p.stdout.decode(), p.stderr.decode()


def test_engine_construction_failure_is_logged_with_engine_exit_code():
    code, out, err = _run_fail("ctor")
    assert code == EXIT_ENGINE
    assert "engine_load_failed" in err and "ValueError" in err
    assert _SECRET not in err and _SECRET not in out
    assert "Traceback" not in err


def test_thread_and_main_uncaught_exceptions_leave_class_name_only():
    code, out, err = _run_fail("thread")
    assert code == 1
    assert "thread_exception" in err and "KeyError" in err and "w1" in err
    assert "uncaught_exception" in err and "ZeroDivisionError" in err
    assert _SECRET not in err and _SECRET not in out
    assert "Traceback" not in err


def test_faulthandler_is_enabled_in_main():
    code, _out, err = _run_fail("fh")
    assert code == 0 and "AssertionError" not in err


# ---------------------------------------------------------------- CR-002 第 3 轮 R3-D1：工作线程意外死亡

_CHILD_DEATH = r"""
import sys
from realtime_subtitle.node import engines, segmenter, worker

mode = sys.argv[1]


class A(engines.AsrEngine):
    def __init__(self):
        secret_local = "%(s)s"  # noqa: F841  faulthandler 不得带出局部变量
        if mode == "segv":
            import faulthandler
            faulthandler._sigsegv()
    def load(self): pass
    def transcribe(self, audio, language):
        raise SystemExit(5)
    def info(self): return {}


class V:
    def __call__(self, w): return 0.9
    def reset(self): pass


engines.WhisperEngine = A
segmenter.SileroVad = V
worker.main()
""" % {"s": _SECRET}


def _spawn_death(mode):
    env = dict(os.environ, PYTHONPATH=str(REPO))
    return subprocess.Popen([sys.executable, "-c", _CHILD_DEATH, mode],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, cwd=str(REPO), env=env)


def test_asr_thread_baseexception_death_makes_worker_exit_internal():
    p = _spawn_death("sysexit")
    try:
        pcm = tone(1.0)
        # stdin 保持打开：退出必须来自线程死亡处理，而不是 EOF。
        p.stdin.write(hello() + audio_frames(pcm) + ctl(type="flush") + ctl(type="drain"))
        p.stdin.flush()
        deadline = time.time() + 10
        while p.poll() is None and time.time() < deadline:
            time.sleep(0.05)
        assert p.poll() is not None, "worker 在工作线程死亡后挂起"
        assert p.returncode == EXIT_INTERNAL
    finally:
        if p.poll() is None:
            p.kill()
        p.stdin.close()
        out, err = p.stdout.read().decode(), p.stderr.read().decode()
        p.stdout.close(); p.stderr.close(); p.wait()
    assert "thread_exception" in err and "SystemExit" in err and "node-asr" in err
    assert "Traceback" not in err
    assert _SECRET not in err and _SECRET not in out


def test_faulthandler_dumps_frames_without_locals_on_native_crash():
    p = _spawn_death("segv")
    try:
        out, err = p.communicate(b"", timeout=30)
    finally:
        if p.poll() is None:
            p.kill()
    err = err.decode()
    assert p.returncode != 0
    assert "Fatal Python error" in err
    assert 'File "' in err
    assert _SECRET not in err and _SECRET not in out.decode()


# ---------------------------------------------------------------- rtf 滑动更新（TK-001b）

from realtime_subtitle.node import info as info_mod  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_state_dir(monkeypatch, tmp_path):
    # 没显式注入目录的用例不许碰真实的 ~/Library/Application Support/rs-node
    monkeypatch.setattr(worker_mod, "STATE_DIR", tmp_path / "no-such-state-dir")


class ClockAsr(FakeAsr):
    """每次识别让假时钟前进 cost 秒，rtf 因此是确定值，不靠真实耗时/sleep。"""

    def __init__(self, clock, cost, **kw):
        super().__init__(**kw)
        self.clock, self.cost = clock, cost

    def transcribe(self, audio, language):
        self.clock[0] += self.cost
        return super().transcribe(audio, language)


class RecordingWorker(Worker):
    """记录每个被识别段的语音时长（a1-a0），用来独立推出期望的 rtf。"""

    seg_durations: list

    def _recognize(self, gen, seg, src):
        self.seg_durations.append(seg.a1 - seg.a0)
        super()._recognize(gen, seg, src)


def speech_session(n_segments, speech_s=3.0):
    """hello 之后 n 段「语音+静音」，最后 drain 保证识别全部落地。"""
    pcm = np.concatenate([np.concatenate([tone(speech_s), silence(1.0)])
                          for _ in range(n_segments)])
    return hello() + audio_frames(pcm) + ctl(type="drain")


def run_rtf(stream, state_dir, cost=0.5, utterances=None, tr_name="fake", factory=None):
    clock = [0.0]
    asr = ClockAsr(clock, cost, utterances=utterances)
    out, err = io.BytesIO(), io.StringIO()
    tr = FakeTr()
    tr.name = tr_name
    w = RecordingWorker(io.BytesIO(stream), out, asr, EnergyVad(),
                        translator_factory=factory or (lambda s, d: tr), err=err,
                        state_dir=state_dir)
    w.seg_durations = []
    worker_mod_time = worker_mod.time
    worker_mod.time = SimpleNamespace(perf_counter=lambda: clock[0])
    try:
        code = w.run()
    finally:
        worker_mod.time = worker_mod_time
    return code, w, err.getvalue()


def read_state(d):
    return json.loads((d / info_mod.ASR_STATE_FILENAME).read_text(encoding="utf-8"))


def test_session_end_creates_state_file_with_session_rtf(tmp_path):
    code, w, _ = run_rtf(speech_session(3), tmp_path, cost=0.5)
    assert code == EXIT_OK
    audio_s = sum(w.seg_durations)
    assert audio_s >= worker_mod.RTF_MIN_AUDIO_S
    expected = 0.5 * len(w.seg_durations) / audio_s
    state = read_state(tmp_path)
    assert state["rtf"] == pytest.approx(expected, abs=1e-3)
    assert state["model"] == "fake" and state["backend"] == "fake"
    assert state["translator"] == "fake"


def test_next_hello_also_ends_session_and_blends_with_old(tmp_path):
    (tmp_path / info_mod.ASR_STATE_FILENAME).write_text(
        json.dumps({"model": "m0", "backend": "b0", "rtf": 0.8, "translator": "apple"}))
    # 第一会话由第二条 hello 结束；第二会话只有 hello，没音频 → 不再更新
    stream = speech_session(3) + hello()
    _, w, _ = run_rtf(stream, tmp_path, cost=0.5)
    session = 0.5 * len(w.seg_durations) / sum(w.seg_durations)
    state = read_state(tmp_path)
    assert state["rtf"] == pytest.approx(0.7 * 0.8 + 0.3 * session, abs=1e-3)
    # 已有的 model/backend 保留；translator 以本会话真实翻译器为准（FakeTr.name == "fake"），不再沿用旧值
    assert (state["model"], state["backend"], state["translator"]) == ("m0", "b0", "fake")


@pytest.mark.parametrize("old", ['not json', '[]', '{"rtf": -1}', '{"rtf": "0.3"}',
                                 '{"rtf": true}', '{"rtf": NaN}', '{}'])
def test_invalid_old_value_is_overwritten_by_session_rtf(tmp_path, old):
    (tmp_path / info_mod.ASR_STATE_FILENAME).write_text(old)
    _, w, _ = run_rtf(speech_session(3), tmp_path, cost=0.5)
    session = 0.5 * len(w.seg_durations) / sum(w.seg_durations)
    assert read_state(tmp_path)["rtf"] == pytest.approx(session, abs=1e-3)


def test_session_shorter_than_threshold_does_not_update(tmp_path):
    path = tmp_path / info_mod.ASR_STATE_FILENAME
    path.write_text(json.dumps({"rtf": 0.8}))
    _, w, err = run_rtf(speech_session(1, speech_s=2.0), tmp_path)
    assert sum(w.seg_durations) < worker_mod.RTF_MIN_AUDIO_S
    assert json.loads(path.read_text()) == {"rtf": 0.8}
    assert "rtf_skipped" in err
    # 没有旧文件时也不凭空创建
    other = tmp_path / "other"
    other.mkdir()
    run_rtf(speech_session(1, speech_s=2.0), other)
    assert not (other / info_mod.ASR_STATE_FILENAME).exists()


def test_sessions_do_not_leak_samples_into_each_other(tmp_path):
    # 第一会话 3 段（够长）、第二会话 1 段（太短）：第二会话不得带着第一会话的累计更新
    stream = speech_session(3) + speech_session(1, speech_s=2.0)
    _, w, _ = run_rtf(stream, tmp_path, cost=0.5)
    first = w.seg_durations[:-1]
    assert read_state(tmp_path)["rtf"] == pytest.approx(
        0.5 * len(first) / sum(first), abs=1e-3)


def test_state_file_has_no_transcript_text_and_is_0600(tmp_path):
    utts = [Utterance(0.1, 0.5, SECRET_SRC)]
    _, _, err = run_rtf(speech_session(3), tmp_path, utterances=utts)
    path = tmp_path / info_mod.ASR_STATE_FILENAME
    raw = path.read_text(encoding="utf-8")
    assert SECRET_SRC not in raw and SECRET_DST not in raw
    assert set(json.loads(raw)) == {"model", "backend", "rtf", "translator"}
    assert (path.stat().st_mode & 0o777) == 0o600
    assert SECRET_SRC not in err and SECRET_DST not in err
    # 不留临时文件
    assert [p.name for p in tmp_path.iterdir()] == [info_mod.ASR_STATE_FILENAME]


def test_gateway_info_reader_reads_back_what_worker_wrote(tmp_path):
    # 假名字 "fake" 不合 info 的 translator 白名单，换成真实格式才能证明两端对齐
    _, w, _ = run_rtf(speech_session(3), tmp_path, cost=0.5, tr_name="ollama:fake")
    asr, translator = info_mod.NodeInfo(state_dir=tmp_path).asr_and_translator()
    assert asr["rtf"] == pytest.approx(read_state(tmp_path)["rtf"])
    assert asr["rtf"] > 0 and translator == "ollama:fake"
    assert asr["model"] == "fake"


def _write_old_state(d, translator):
    (d / info_mod.ASR_STATE_FILENAME).write_text(json.dumps(
        {"model": "m0", "backend": "b0", "rtf": 0.8, "translator": translator}))


def test_session_translator_overrides_stale_old_value(tmp_path):
    # 安装自检写了 ollama:x，之后装好语言包，本会话实际走 apple → 必须改写成 apple
    _write_old_state(tmp_path, "ollama:x")
    run_rtf(speech_session(3), tmp_path, tr_name="apple")
    assert read_state(tmp_path)["translator"] == "apple"
    _, translator = info_mod.NodeInfo(state_dir=tmp_path).asr_and_translator()
    assert translator == "apple"


def test_failed_translator_construction_keeps_old_valid_value(tmp_path):
    def boom(s, d):
        raise RuntimeError("no language pack")
    _write_old_state(tmp_path, "apple")
    run_rtf(speech_session(3), tmp_path, factory=boom)
    assert read_state(tmp_path)["translator"] == "apple"
    _, translator = info_mod.NodeInfo(state_dir=tmp_path).asr_and_translator()
    assert translator == "apple"


def test_no_old_value_and_no_translator_writes_default(tmp_path):
    def boom(s, d):
        raise RuntimeError("no language pack")
    (tmp_path / info_mod.ASR_STATE_FILENAME).write_text(json.dumps({"rtf": 0.8}))
    run_rtf(speech_session(3), tmp_path, factory=boom)
    assert read_state(tmp_path)["translator"] == info_mod.DEFAULT_TRANSLATOR
    _, translator = info_mod.NodeInfo(state_dir=tmp_path).asr_and_translator()
    assert translator == info_mod.DEFAULT_TRANSLATOR


def test_missing_state_dir_is_logged_and_keeps_exit_code(tmp_path):
    missing = tmp_path / "nope"
    code, _, err = run_rtf(speech_session(3), missing)
    assert code == EXIT_OK
    assert not missing.exists()  # worker 不负责建目录
    assert "rtf_state_dir_missing" in err


def test_write_failure_is_logged_and_does_not_change_exit_code(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise PermissionError(SECRET_SRC)
    monkeypatch.setattr(worker_mod.os, "replace", boom)
    code, _, err = run_rtf(speech_session(3), tmp_path)
    assert code == EXIT_OK
    assert "rtf_write_failed err=PermissionError" in err
    assert SECRET_SRC not in err
    assert list(tmp_path.iterdir()) == []  # 临时文件已清理


# ---------------------------------------------------------------- SIGTERM 回收也要收口 rtf（CR-004 F1/F2）

_CHILD_RTF = _CHILD.replace(
    "worker.main()",
    "from pathlib import Path\nworker.STATE_DIR = Path(os.environ['RTF_DIR'])\nworker.main()",
).replace("import sys\n", "import os, sys\n", 1)


def _read_events_until(p, pred, timeout=30.0):
    """从子进程 stdout 逐行读事件直到 pred 满足；用读线程 + 事件，不靠固定 sleep。"""
    got, done = [], threading.Event()

    def reader():
        for line in p.stdout:
            ev = json.loads(line)
            got.append(ev)
            if pred(got):
                done.set()
                return

    threading.Thread(target=reader, daemon=True).start()
    assert done.wait(timeout), f"timeout; got {got}"
    return got


@pytest.mark.skipif(sys.platform == "win32", reason="Windows 的 terminate() 没有 SIGTERM 处理函数")
def test_subprocess_sigterm_mid_session_still_updates_rtf(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    env = dict(os.environ, PYTHONPATH=str(REPO), RTF_DIR=str(state))
    p = subprocess.Popen([sys.executable, "-c", _CHILD_RTF], stdin=subprocess.PIPE,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                         cwd=str(REPO), env=env)
    try:
        pcm = np.concatenate([tone(6.0), silence(0.4)])  # 一段 >=5s，flush 后识别并累计
        p.stdin.write(hello() + audio_frames(pcm) + ctl(type="flush"))
        p.stdin.flush()  # 不关 stdin：模拟 gateway 保温到期只发 SIGTERM
        _read_events_until(
            p, lambda evs: any(e["ev"] == "translation" for e in evs))
        p.send_signal(signal.SIGTERM)
        assert p.wait(10) == 0
        err = p.stderr.read().decode()
    finally:
        p.kill()
        p.stdin.close()
        p.stdout.close()
        p.stderr.close()
    f = state / info_mod.ASR_STATE_FILENAME
    assert f.exists()
    assert (f.stat().st_mode & 0o777) == 0o600
    data = json.loads(f.read_text(encoding="utf-8"))
    assert 0 <= data["rtf"] < 1
    assert set(data) == {"model", "backend", "rtf", "translator"}
    assert "Hallo Welt" not in f.read_text(encoding="utf-8") + err
    assert "你好世界" not in f.read_text(encoding="utf-8") + err
