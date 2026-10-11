"""app 把 remote/ 接进主链路。不实例化 SubtitleApp，不连网络。"""
import os

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

from realtime_subtitle.app import open_remote_bridge  # noqa: E402
from realtime_subtitle.remote.cloud_client import CloudTranscript, MappedSegment  # noqa: E402
from realtime_subtitle.remote.node_client import NodeConfig, SelfNodeError  # noqa: E402
from realtime_subtitle.remote.router import RemoteRouter  # noqa: E402


class _UI:
    def __init__(self):
        self.lines = []

    def show(self, src, dst, t0, t1, key, source):
        self.lines.append({
            "src": src, "dst": dst, "t0": t0, "t1": t1, "key": key, "source": source,
        })

    def drop(self, keys):
        gone = set(keys or [])
        self.lines = [line for line in self.lines if line["key"] not in gone]

    def update_dst(self, key, dst):
        for line in self.lines:
            if line["key"] == key:
                line["dst"] = dst


class _Translator:
    def __init__(self):
        self.calls = []
        self._sample_clock_samples = 0

    def _translate_single_sentence(self, sentence, german_context, on_partial=None):
        self.calls.append((sentence, german_context))
        return "中文译文"


class _Cloud:
    def __init__(self):
        self.cancelled = 0
        self.calls = []

    def transcribe(self, pcm, *, a0, language):
        self.calls.append((a0, language, int(len(pcm))))
        return CloudTranscript(
            text="Hallo",
            segments=(MappedSegment(start=a0, end=a0 + 1.0, text="Hallo"),),
            groq_id=None,
            audio_s=1.0,
            duration_s=1.0,
            status=200,
            remaining_requests=9,
        )

    def cancel(self):
        self.cancelled += 1


class _Segmenter:
    def __init__(self):
        self.fed = 0

    def feed(self, pcm):
        self.fed += 1
        return [(0.0, 1.0, pcm)]


class _Ledger:
    def __init__(self, path, routing=None):
        self.path = path
        self.routing = routing
        self.notes = []

    def note(self, duration_s, *, now=None, remaining_requests=None):
        self.notes.append((duration_s, remaining_requests))


class _Node:
    def __init__(self):
        self.sent = []
        self.closed = False

    async def connect(self):
        return None

    async def send(self, data, start_seconds):
        self.sent.append((bytes(data), start_seconds))

    async def close(self):
        self.closed = True


def _boom(*_args, **_kwargs):
    raise AssertionError("远端工厂不该被调用")


def _open(**kwargs):
    ui = kwargs.pop("ui", None) or _UI()
    translator = kwargs.pop("translator", None) or _Translator()
    bridge = open_remote_bridge(
        translator=translator,
        ui_show=ui.show,
        ui_drop=ui.drop,
        ui_update_dst=ui.update_dst,
        **kwargs,
    )
    return bridge, ui, translator


def test_both_off_constructs_nothing():
    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=False,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=_boom,
        node_client_factory=_boom,
    )
    assert bridge is None


def test_cloud_on_builds_cloud_stack_not_node_client(tmp_path):
    cloud = _Cloud()
    segmenter = _Segmenter()
    ledgers = []
    node_calls = []

    def ledger_factory(path, routing=None):
        ledgers.append(_Ledger(path, routing))
        return ledgers[-1]

    bridge, ui, translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=lambda: segmenter,
        ledger_factory=ledger_factory,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        node_client_factory=lambda *args, **kwargs: node_calls.append(args),
        quota_path=tmp_path / "quota.json",
    )
    try:
        assert bridge is not None
        assert bridge.router.node_enabled is False
        assert node_calls == []
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        assert segmenter.fed == 1
        assert cloud.calls and cloud.calls[0][1]
        assert translator.calls == [("Hallo", "")]
        assert ledgers[0].notes
        assert any(line["dst"] == "中文译文" and line["source"] == "remote" for line in ui.lines)
        assert node_calls == []
    finally:
        bridge.close()
    assert cloud.cancelled >= 1


def test_node_on_does_not_build_cloud(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    seen = {}

    def router_factory(**kwargs):
        seen["kwargs"] = kwargs
        return RemoteRouter(**kwargs)

    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=True,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=router_factory,
        node_client_factory=_boom,
        node_entries=[{
            "id": "desk",
            "node_id": "tk007nodeid0001",
            "url": "ws://100.64.0.8:8791",
            "token_file": "unused",
        }],
        quota_path=tmp_path / "quota.json",
    )
    try:
        assert bridge.cloud is None
        assert bridge.segmenter is None
        assert bridge.ledger is None
        assert bridge.router.node_enabled is True
        assert seen["kwargs"]["node_configs"][0].id == "desk"
        assert bridge.route_label().startswith("混合·")
    finally:
        bridge.close()


def test_local_pair_reaches_ui_and_remote_replaces_it(tmp_path):
    bridge, ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.on_local_pair("Hallo", "你好", 0.0, 2.0, "local-1", 0)
        assert ui.lines[-1]["key"] == "local-1"
        assert ui.lines[-1]["source"] == "local"
        assert any(line.key == "local-1" for line in bridge.line_store.lines)
        bridge._publish_remote("cloud", 0.0, 2.0, "Hallo", "云端")
        assert all(line["key"] != "local-1" for line in ui.lines)
        assert any(line["source"] == "remote" and line["dst"] == "云端" for line in ui.lines)
        assert all(line.key != "local-1" for line in bridge.line_store.lines)
    finally:
        bridge.close()


def test_set_prefs_off_cancels_cloud(tmp_path):
    cloud = _Cloud()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.set_prefs(False, False)
        assert cloud.cancelled == 1
        assert bridge.cloud_on is False
        assert bridge.route_label() == "本机"
    finally:
        bridge.close()


def test_node_feed_connects_once_and_skips_self(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    prepared = []
    nodes = []

    def prepare(cfg):
        prepared.append(cfg.id)
        if cfg.id == "self":
            raise SelfNodeError("self")
        return ("100.64.0.8", object())

    def factory(cfg, info, callbacks):
        node = _Node()
        nodes.append((cfg.id, node))
        return node

    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=True,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        prepare=prepare,
        node_client_factory=factory,
        node_entries=[
            NodeConfig(id="self", node_id="s" * 16, url="ws://100.64.0.9:8791", token_file="x"),
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
    )
    try:
        assert nodes == []
        audio = np.zeros(320, dtype=np.float32)
        bridge.feed_audio(audio)
        bridge.flush()
        assert prepared == ["self", "desk"]
        assert [item[0] for item in nodes] == ["desk"]
        assert nodes[0][1].sent
        bridge.feed_audio(audio)
        bridge.flush()
        assert prepared == ["self", "desk"]
    finally:
        bridge.close()
    assert nodes[0][1].closed


def test_cloud_defaults_use_the_remote_classes(monkeypatch, tmp_path):
    import realtime_subtitle.remote.cloud_client as cloud_client
    import realtime_subtitle.remote.cloud_segmenter as cloud_segmenter
    import realtime_subtitle.remote.quota as quota

    created = {}

    class _Seg:
        def __init__(self, vad=None):
            created["seg"] = True

    class _Cli:
        def __init__(self, *, key_path=None, session_factory=None):
            created["cli"] = key_path

        def cancel(self):
            created["cancel"] = True

    class _Led:
        def __init__(self, path, *, routing=None, now=None):
            created["led"] = path

    monkeypatch.setattr(cloud_segmenter, "CloudSegmenter", _Seg)
    monkeypatch.setattr(cloud_client, "CloudClient", _Cli)
    monkeypatch.setattr(quota, "QuotaLedger", _Led)
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        quota_path=tmp_path / "quota.json",
        node_client_factory=_boom,
    )
    try:
        assert created["seg"] is True
        assert created["cli"] is not None
        assert created["led"] == tmp_path / "quota.json"
        assert type(bridge.router) is RemoteRouter
    finally:
        bridge.close()
    assert created.get("cancel") is True
