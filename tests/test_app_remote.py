"""app 把 remote/ 接进主链路。不实例化 SubtitleApp，不连网络。"""
import os
import threading

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

    def show(self, src, dst, t0, t1, key, source, revision=0):
        self.lines.append({
            "src": src, "dst": dst, "t0": t0, "t1": t1, "key": key, "source": source,
            "revision": revision,
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


class _BlockingCloud(_Cloud):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def transcribe(self, pcm, *, a0, language):
        self.entered.set()
        assert self.release.wait(timeout=2)
        return super().transcribe(pcm, a0=a0, language=language)


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

    def usage(self, now=None):
        return None


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


def test_node_translate_failed_with_id_uses_local_fallback_once_and_blocks_late_translation(tmp_path):
    statuses: list[str] = []
    bridge, ui, translator = _open(
        cloud_on=True,
        node_on=False,
        ui_status=statuses.append,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        generation = bridge._generation
        bridge._on_node_status_event("desk", generation, {"ev": "status", "code": "translate_failed", "id": 42})
        bridge._on_node_translate_failed("desk", generation, 42)
        bridge._on_node_status_event("desk", generation, {"ev": "status", "code": "translate_failed", "id": 43})
        bridge._on_node_translate_failed("desk", generation, 43)

        bridge._on_node_final("desk", generation, 42, "Hallo", 1.0, 2.0)
        bridge.flush()
        assert statuses == ["节点 desk：翻译暂不可用，改用本机翻译"]
        assert translator.calls == [("Hallo", "")]
        assert ui.lines[-1]["dst"] == "中文译文"

        bridge._on_node_translation("desk", generation, 42, "")
        bridge.flush()
        assert ui.lines[-1]["dst"] == "中文译文"
    finally:
        bridge.close()


def test_cloud_result_sent_before_language_switch_is_discarded(tmp_path, caplog):
    cloud = _BlockingCloud()
    bridge, ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        with caplog.at_level("DEBUG"):
            bridge.feed_audio(np.zeros(320, dtype=np.float32))
            assert cloud.entered.wait(timeout=2)
            bridge.on_language_applied("en", "zh", 1)
            cloud.release.set()
            bridge.flush()
        assert ui.lines == []
        assert not bridge.line_store.lines
        assert cloud.cancelled == 1
        assert any("stale remote" in record.message for record in caplog.records)
    finally:
        bridge.close()


def test_cloud_result_sent_after_language_switch_is_displayed(tmp_path):
    cloud = _Cloud()
    bridge, ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.on_language_applied("en", "zh", 1)
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        assert len(ui.lines) == 1
        assert ui.lines[0]["source"] == "remote"
        assert ui.lines[0]["revision"] == 1
        assert bridge.line_store.lines[0].src == "Hallo"
    finally:
        bridge.close()


def test_node_continuous_language_switches_accept_only_latest_generation(tmp_path, caplog):
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
        bridge.on_language_applied("en", "zh", 1)
        stale_generation = bridge._generation
        bridge.on_language_applied("de", "zh", 2)
        current_generation = bridge._generation
        with caplog.at_level("DEBUG"):
            bridge._on_node_final("desk", stale_generation, 1, "Alt", 0.0, 1.0)
            bridge._on_node_translation("desk", stale_generation, 1, "旧译文")
            bridge.flush()
        assert ui.lines == []
        assert any("stale remote" in record.message for record in caplog.records)

        bridge._on_node_final("desk", current_generation, 1, "Neu", 1.0, 2.0)
        bridge.flush()
        assert len(ui.lines) == 1
        assert ui.lines[0]["src"] == "Neu"
        assert ui.lines[0]["revision"] == 2
        bridge._on_node_translation("desk", stale_generation, 1, "旧译文")
        bridge._on_node_translation("desk", current_generation, 1, "新译文")
        bridge.flush()
        assert ui.lines[0]["dst"] == "新译文"
    finally:
        bridge.close()


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


def test_local_pair_forwards_language_revision(tmp_path):
    """BUG-01：本机句的 language revision 要原样交到上屏，不能被丢掉。"""
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
        bridge.on_local_pair("Hallo", "你好", 0.0, 1.0, "local-9", 4)
        assert ui.lines[-1]["revision"] == 4
        assert ui.lines[-1]["source"] == "local"
        bridge._publish_remote("cloud", 0.0, 1.0, "Hallo", "云端")
        remote = [line for line in ui.lines if line["source"] == "remote"]
        assert remote
        assert remote[-1]["revision"] == 4
    finally:
        bridge.close()


def test_show_line_uses_real_revision_and_inserts_remote_by_t0():
    """BUG-01/05：过期代数仍丢弃；远端句按 t0 插回原位，而不是追加到末尾。"""
    from realtime_subtitle.app import SubtitleApp

    app = SubtitleApp.__new__(SubtitleApp)

    class _Window:
        def __init__(self):
            self.language_revision = 2
            self.sentence_pairs = [
                ("early", "早", 1.0, 0.0, 1.0, "local", "early"),
                ("late", "晚", 2.0, 3.0, 4.0, "local", "late"),
            ]
            self.added = []

        def add_pair(self, german, chinese, t0=None, t1=None, key=None, revision=0):
            self.added.append(revision)
            if int(revision or 0) < self.language_revision:
                return
            self.sentence_pairs.append((german, chinese, 0.0, t0, t1, "local", key))

        def _render(self):
            return None

    window = _Window()
    app.subtitle_window = window
    app._ui_show_line("stale", "旧", 0.2, 0.4, "stale", "local", 0)
    assert window.added[-1] == 0
    assert all(item[6] != "stale" for item in window.sentence_pairs)

    app._ui_show_line("later", "后", 5.0, 6.0, "later", "local", 2)
    assert window.sentence_pairs[-1][6] == "later"

    # 更晚的本机句已经在末尾。远端替换更早的那句时，不能再追加到它后面。
    app._ui_drop_keys(["early"])
    app._ui_show_line("cloud", "云", 0.0, 1.0, -1, "remote", 0)
    assert window.added[-1] == 2
    assert [item[3] for item in window.sentence_pairs] == [0.0, 3.0, 5.0]
    assert window.sentence_pairs[0][0] == "cloud"
    assert window.sentence_pairs[0][5] == "remote"
    assert window.sentence_pairs[-1][0] == "later"


class _ClockSegmenter:
    def __init__(self):
        self.samples = 0

    def feed(self, pcm):
        a0 = self.samples / 16000.0
        self.samples += int(pcm.shape[0])
        return [(a0, self.samples / 16000.0, pcm)]


def test_segmenter_keeps_clock_when_tier_leaves_cloud(tmp_path):
    """BUG-02：cloud_on 期间音频一直喂给分段器；非云端档吐出的段不上传。"""
    cloud = _Cloud()
    segmenter = _ClockSegmenter()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=lambda: segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.routing.cloud_state = "cooldown_backoff"
        bridge._sync_provider()
        assert bridge.router.select_tier() != "cloud"
        bridge.feed_audio(np.zeros(1600, dtype=np.float32))
        bridge.flush()
        assert segmenter.samples == 1600
        assert cloud.calls == []
        bridge.routing.cloud_state = "available"
        bridge._sync_provider()
        bridge.feed_audio(np.zeros(1600, dtype=np.float32))
        bridge.flush()
        assert segmenter.samples == 3200
        assert cloud.calls
        assert cloud.calls[-1][0] == 1600 / 16000.0
    finally:
        bridge.close()


def test_feed_audio_expires_cooldown_and_recovers_quota(tmp_path, monkeypatch):
    """BUG-03：音频驱动下，冷却到期派发 cooldown_expired；用量掉到 80% 以下就恢复。"""
    import time

    from realtime_subtitle.remote.quota import QuotaLedger

    clock = {"t": 1_700_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    cloud = _Cloud()

    class _Quiet:
        def __init__(self):
            self.samples = 0

        def feed(self, pcm):
            self.samples += int(pcm.shape[0])
            return []

    segmenter = _Quiet()

    def ledger_factory(path, routing=None):
        return QuotaLedger(path, routing=routing)

    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=lambda: segmenter,
        ledger_factory=ledger_factory,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.routing.apply({"type": "http_status", "status": 429, "retry_after": 5})
        assert bridge.routing.cloud_state == "cooldown_rate_limited"
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "cooldown_rate_limited"
        clock["t"] += 0.4
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "cooldown_rate_limited"
        clock["t"] += 5
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "probe"

        for _ in range(18):
            bridge.ledger.note(1.0, now=clock["t"])
        assert bridge.routing.cloud_state == "cooldown_quota"
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "cooldown_quota"
        clock["t"] += 61
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "available"
        assert segmenter.samples > 0
    finally:
        bridge.close()


def test_node_reconnect_does_not_leak_threads(tmp_path, monkeypatch):
    """BUG-04：断开后重连复用或收掉旧 loop，线程数不随重连次数增长。"""
    import threading

    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")

    def prepare(cfg):
        return ("100.64.0.8", object())

    def factory(cfg, info, callbacks):
        return _Node()

    def remote_threads():
        return [item for item in threading.enumerate() if item.name == "remote-node" and item.is_alive()]

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
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
    )
    try:
        baseline = len(remote_threads())
        audio = np.zeros(320, dtype=np.float32)
        bridge.feed_audio(audio)
        bridge.flush()
        assert len(remote_threads()) == baseline + 1
        for _ in range(4):
            bridge._on_node_closed("desk", 0, ConnectionError("gone"))
            bridge._node_backoff_until = 0
            bridge.feed_audio(audio)
            bridge.flush()
            assert len(remote_threads()) == baseline + 1
    finally:
        bridge.close()
    assert len(remote_threads()) == baseline


def test_ensure_node_drops_client_if_closed_or_generation_moved(tmp_path, monkeypatch):
    """BUG-07：连接返回后若已关闭或代次变了，立刻关掉新客户端，不能留在桥上。"""
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    box = {}

    def prepare(cfg):
        return ("100.64.0.8", object())

    def factory(cfg, info, callbacks):
        node = _Node()

        async def connect():
            if box["mode"] == "closed":
                box["bridge"]._closed = True
            else:
                box["bridge"]._generation += 1

        node.connect = connect
        box["node"] = node
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
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
    )
    box["bridge"] = bridge
    try:
        audio = np.zeros(320, dtype=np.float32)
        box["mode"] = "closed"
        bridge.feed_audio(audio)
        assert box["node"].closed is True
        assert bridge._node_client is None

        bridge._closed = False
        bridge._node_backoff_until = 0
        box["mode"] = "generation"
        bridge.feed_audio(audio)
        assert box["node"].closed is True
        assert bridge._node_client is None
    finally:
        bridge.close()
