"""离线任务的两笔资源账：显存（翻译模型别白占两小时）和磁盘（断点别 O(n²) 重写）。"""
import ctypes
import json
import sys
import uuid

import pytest

import realtime_subtitle.offline as offline
from realtime_subtitle import config


def _stub_ollama_local(monkeypatch):
    import realtime_subtitle.translate.translator_queue as tq
    monkeypatch.setattr(tq, "_assert_local_ollama", lambda url: True)
    monkeypatch.setattr(tq, "ollama_url", lambda: "http://127.0.0.1:11434")
    monkeypatch.setattr(offline.time, "sleep", lambda s: None)


def _rows(n):
    return [{"start": i, "end": i + 1, "text": f"Satz {i}"} for i in range(n)]


@pytest.fixture
def frozen_clock(monkeypatch):
    """时钟停住 = 翻译无限快：节流窗口一直没过，只剩强制落盘那几次。"""
    monkeypatch.setattr(offline.time, "monotonic", lambda: 1000.0)


@pytest.fixture
def save_spy(monkeypatch):
    saved = []
    real = offline.save_checkpoint

    def spy(path, data):
        saved.append(json.loads(json.dumps(data)))
        real(path, data)
    monkeypatch.setattr(offline, "save_checkpoint", spy)
    return saved


# ------------------------------------------------------------------
# 断点落盘节流
# ------------------------------------------------------------------

def test_checkpoint_is_not_rewritten_for_every_row(tmp_path, monkeypatch,
                                                   frozen_clock, save_spy):
    """☠️ 以前每翻一条就把整份断点（全部 rows + 译文表）重写一遍，两三千条的
    长视频累计写好几个 GB。现在 2 秒内最多一次，结束时强制补一次。"""
    _stub_ollama_local(monkeypatch)
    monkeypatch.setattr(offline, "_ollama_request", lambda *a, **k: "译")
    rows = _rows(200)
    offline.translate_segments(rows, "de", "zh",
                               checkpoint_path=tmp_path / "_job_checkpoint.json")
    assert len(save_spy) == 2, f"200 条写了 {len(save_spy)} 次断点"
    final = save_spy[-1]
    assert all(r.get("translation") == "译" for r in final["rows"]), \
        "结束时那次强制落盘必须带上全部译文"


def test_throttle_still_saves_as_time_passes(tmp_path, monkeypatch, save_spy):
    """节流不是"只在结束时写"：时间走过窗口就照常落盘。"""
    _stub_ollama_local(monkeypatch)
    clock = {"t": 0.0}

    def tick():
        clock["t"] += 0.5          # 每次调用前进半秒 ≈ 每条翻译半秒
        return clock["t"]
    monkeypatch.setattr(offline.time, "monotonic", tick)
    monkeypatch.setattr(offline, "_ollama_request", lambda *a, **k: "译")
    offline.translate_segments(_rows(40), "de", "zh",
                               checkpoint_path=tmp_path / "_job_checkpoint.json")
    assert 5 <= len(save_spy) <= 15, len(save_spy)


def test_interrupt_flushes_rows_translated_since_last_save(tmp_path, monkeypatch,
                                                           frozen_clock, save_spy):
    """Ctrl+C 落在节流窗口里：已经翻好、还没落盘的那几条不能丢。"""
    _stub_ollama_local(monkeypatch)
    calls = {"n": 0}

    def fake_req(*a, **k):
        calls["n"] += 1
        if calls["n"] == 6:
            raise KeyboardInterrupt
        return "译"
    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    ckpt = tmp_path / "_job_checkpoint.json"
    with pytest.raises(KeyboardInterrupt):
        offline.translate_segments(_rows(10), "de", "zh", checkpoint_path=ckpt)
    on_disk = json.loads(ckpt.read_text(encoding="utf-8"))
    done = [r for r in on_disk["rows"] if r.get("translation")]
    assert len(done) == 5, "中断前翻好的 5 条都要在断点里"


def test_failure_flushes_before_raising(tmp_path, monkeypatch, frozen_clock, save_spy):
    _stub_ollama_local(monkeypatch)

    def fake_req(session, url, model, prompt, num_predict=512):
        if "Satz 3" in prompt.split("原文")[-1]:
            raise RuntimeError("boom")
        return "译"
    monkeypatch.setattr(offline, "_ollama_request", fake_req)
    ckpt = tmp_path / "_job_checkpoint.json"
    with pytest.raises(offline.OfflineSubtitleError):
        offline.translate_segments(_rows(6), "de", "zh", checkpoint_path=ckpt)
    on_disk = json.loads(ckpt.read_text(encoding="utf-8"))
    assert [bool(r.get("translation")) for r in on_disk["rows"][:3]] == [True] * 3


# ------------------------------------------------------------------
# 显存：短租期 + 结束时主动卸载（实时字幕开着时不卸）
# ------------------------------------------------------------------

class _FakeResp:
    def __init__(self, body=None):
        self._body = body or {}
        self.status_code = 200

    def json(self):
        return self._body

    def raise_for_status(self):
        pass

    def close(self):
        pass


class _FakeSession:
    def __init__(self, loaded=()):
        self.loaded = list(loaded)
        self.posts = []
        self.gets = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, timeout=None):
        self.gets.append(url)
        return _FakeResp({"models": [{"name": n} for n in self.loaded]})

    def post(self, url, json=None, timeout=None):
        self.posts.append(json)
        return _FakeResp({"response": "译", "done": True})


def test_offline_requests_do_not_pin_model_for_two_hours(monkeypatch):
    monkeypatch.setattr(offline, "_models_used", set())
    session = _FakeSession()
    monkeypatch.setattr(offline, "_parse_ollama_payload", lambda p: "译")
    offline._ollama_request(session, "http://127.0.0.1:11434", "qwen3.5:9b", "p")
    assert session.posts[0]["keep_alive"] == config.OFFLINE_OLLAMA_KEEP_ALIVE
    assert session.posts[0]["keep_alive"] != "2h"
    assert offline._models_used == {("http://127.0.0.1:11434", "qwen3.5:9b")}


def test_release_unloads_only_models_this_process_used(monkeypatch):
    session = _FakeSession(loaded=["qwen3.5:9b", "llama3:8b"])
    monkeypatch.setattr(offline.requests, "Session", lambda: session)
    monkeypatch.setattr(offline, "_realtime_subtitle_running", lambda: False)
    monkeypatch.setattr(offline, "_models_used",
                        {("http://127.0.0.1:11434", "qwen3.5")})  # 不带 tag 也要认
    assert offline.release_offline_models() == ["qwen3.5:9b"]
    assert session.posts == [{"model": "qwen3.5:9b", "prompt": "", "keep_alive": 0}]
    assert offline._models_used == set()


def test_release_keeps_model_while_realtime_subtitle_runs(monkeypatch):
    """☠️ 两边用同一个翻译模型：卸了等于让正在看直播的人白付一次冷加载。"""
    session = _FakeSession(loaded=["qwen3.5:9b"])
    monkeypatch.setattr(offline.requests, "Session", lambda: session)
    monkeypatch.setattr(offline, "_realtime_subtitle_running", lambda: True)
    monkeypatch.setattr(offline, "_models_used",
                        {("http://127.0.0.1:11434", "qwen3.5:9b")})
    assert offline.release_offline_models() == []
    assert session.posts == [] and session.gets == []


def test_main_releases_models_even_when_job_fails(monkeypatch):
    called = []
    monkeypatch.setattr(offline, "release_offline_models", lambda: called.append(1))

    def boom(*a, **k):
        raise offline.OfflineSubtitleError("下载失败")
    monkeypatch.setattr(offline, "process_input", boom)
    assert offline.main(["https://example.com/v"]) == 1
    assert called == [1]


@pytest.mark.skipif(sys.platform != "win32", reason="命名 mutex 是 Windows 的")
def test_realtime_detection_reads_the_real_mutex(monkeypatch):
    """用真的命名 mutex 验：有人持有 → True；放掉 → False。
    名字换成随机的，免得开发机上正开着的实时字幕干扰结论。"""
    name = f"realtime_subtitle_test_{uuid.uuid4().hex}"
    monkeypatch.setattr(offline, "_REALTIME_MUTEX", name)
    assert offline._realtime_subtitle_running() is False
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    k32.CreateMutexW.restype = ctypes.c_void_p
    k32.CloseHandle.argtypes = (ctypes.c_void_p,)
    h = k32.CreateMutexW(None, False, name)
    try:
        assert offline._realtime_subtitle_running() is True
    finally:
        k32.CloseHandle(h)
    assert offline._realtime_subtitle_running() is False


def test_mutex_name_matches_the_realtime_app():
    """两边各写一份字符串：app.py 那边改名这边就瞎了，钉住。"""
    from pathlib import Path
    src = (Path(offline.__file__).parent / "app.py").read_text(encoding="utf-8")
    assert f'"{offline._REALTIME_MUTEX}"' in src
