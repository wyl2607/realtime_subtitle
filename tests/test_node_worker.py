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


def test_utterance_times_are_clamped_into_the_segment_audio():
    asr = FakeAsr([Utterance(-5.0, 99.0, "x")])
    pcm = np.concatenate([tone(1.0), silence(1.0)])
    _c, ev, *_ = run_worker(hello() + audio_frames(pcm) + ctl(type="drain"), asr=asr)
    f = next(e for e in ev if e["ev"] == "final")
    assert 0.0 <= f["a0"] <= f["a1"] <= 1.4


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
                                  {"src": 3}])
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

    monkeypatch.setattr(requests, "post", fake_post)
    assert OllamaBackend().translate("Hallo", "de", "zh") == "你好"
    assert seen == ["http://127.0.0.1:59999/api/generate"]


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
    assert b"noise from a library" in err
    assert "Hallo Welt" not in err.decode()


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
