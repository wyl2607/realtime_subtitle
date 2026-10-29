"""app 把 remote/ 接进主链路。不实例化 SubtitleApp，不连网络。"""
import os
import threading
from pathlib import Path

os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import numpy as np  # noqa: E402

from realtime_subtitle.app import open_remote_bridge  # noqa: E402
from realtime_subtitle.remote.cloud_client import CloudTranscript, MappedSegment  # noqa: E402
from realtime_subtitle.remote.node_client import NodeConfig, SelfNodeError  # noqa: E402
from realtime_subtitle.remote.router import RemoteRouter  # noqa: E402
from realtime_subtitle.ui import design_tokens  # noqa: E402


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
            reset_requests_s=12.5,
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

    def note(self, duration_s, *, now=None, remaining_requests=None, reset_requests_s=None):
        self.notes.append((duration_s, remaining_requests, reset_requests_s))

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
    # 云端 key 热更测试一律显式给临时路径；其它桥接测试也不能碰用户真实配置。
    if kwargs.get("cloud_on") and "key_path" not in kwargs:
        kwargs["key_path"] = Path(__file__).with_name("_test_groq_key_missing")
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
        assert ledgers[0].notes == [(1.0, 9, 12.5)]
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


def test_route_label_uses_tokens_for_cooldown_and_invalid_key(tmp_path):
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.routing.cloud_state = "cooldown_rate_limited"
        bridge.routing.cooldown_seconds = 7
        assert bridge.route_label() == (
            design_tokens.TEXT_STATUS_LOCAL + design_tokens.TEXT_STATUS_CLOUD_COOLDOWN_SUFFIX)
        bridge.routing.cloud_state = "disabled_invalid_key"
        assert bridge.route_label() == design_tokens.TEXT_STATUS_CLOUD_KEY_INVALID
    finally:
        bridge.close()


def test_route_label_shows_node_loading_suffix(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=True,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
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
        bridge._node_loading_id = "desk"
        assert bridge.route_label() == "desk" + design_tokens.TEXT_NODE_LOADING_SUFFIX
        bridge._node_loading_id = None
        assert bridge.route_label().startswith(design_tokens.TEXT_STATUS_HYBRID_PREFIX)
    finally:
        bridge.close()


def test_route_label_keeps_cooldown_suffix_on_hybrid(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=True,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        node_client_factory=_boom,
        node_entries=[{
            "id": "mini2",
            "node_id": "tk007nodeid0001",
            "url": "ws://100.64.0.8:8791",
            "token_file": "unused",
        }],
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.routing.has_key = True
        bridge.routing.cloud_state = "cooldown_rate_limited"
        bridge.routing.cooldown_seconds = 3
        assert bridge.route_label() == (
            design_tokens.TEXT_STATUS_HYBRID_PREFIX + "mini2"
            + design_tokens.TEXT_STATUS_CLOUD_COOLDOWN_SUFFIX)
    finally:
        bridge.close()


def test_cloud_429_logs_cooldown_once_without_key(tmp_path, capsys):
    from realtime_subtitle.remote.cloud_client import CloudError

    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        quota_path=tmp_path / "quota.json",
    )
    try:
        error = CloudError(
            status=429, reason="rate limited", exc_type="HTTPError",
            retry_after=7, reset_requests_s=0.12, secret="gsk_test_fake_key_never_logged")
        bridge._on_cloud_exception(error, 0)
        assert bridge.ledger.notes == [(0.0, None, 0.12)]
        out = capsys.readouterr().out
        assert "rslite.cloud cooldown reason=429 retry_after=7" in out
        assert "gsk_test_fake_key_never_logged" not in out
        # 冷却还没结束时再来一次 429 不重复刷日志。
        bridge._on_cloud_exception(error, 0)
        assert "rslite.cloud cooldown" not in capsys.readouterr().out
    finally:
        bridge.close()


def test_replaced_key_late_401_is_not_marked_invalid_and_cancels_old_cloud(tmp_path, capsys):
    from realtime_subtitle.remote.cloud_client import CloudError

    old_key = "gsk_test_fake_old_key_4baca9"
    new_key = "gsk_test_fake_new_key_cde801"
    key_path = tmp_path / "groq.key"
    key_path.write_text(old_key + "\n", encoding="utf-8")
    cloud = _Cloud()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._on_cloud_exception(
            CloudError(status=401, reason="unauthorized", exc_type="HTTPStatus", secret=old_key),
            bridge._generation,
        )
        assert bridge.route_label() != design_tokens.TEXT_STATUS_CLOUD_KEY_INVALID
        assert bridge.routing.cloud_state != "disabled_invalid_key"
        assert cloud.cancelled == 1
        output = capsys.readouterr()
        assert old_key not in output.out
        assert new_key not in output.out
        assert old_key not in output.err
        assert new_key not in output.err
    finally:
        bridge.close()


def test_key_file_change_recovers_an_invalid_cloud_route(tmp_path):
    from realtime_subtitle.remote.cloud_client import CloudError

    old_key = "gsk_test_fake_invalid_key_49280d"
    new_key = "gsk_test_fake_replacement_key_0f1b2d"
    key_path = tmp_path / "groq.key"
    key_path.write_text(old_key + "\n", encoding="utf-8")
    cloud = _Cloud()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge._on_cloud_exception(
            CloudError(status=401, reason="unauthorized", exc_type="HTTPStatus", secret=old_key),
            bridge._generation,
        )
        assert bridge.routing.cloud_state == "disabled_invalid_key"
        generation = bridge._generation
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert bridge.routing.tier() == "cloud"
        assert bridge.route_label() == design_tokens.TEXT_STATUS_CLOUD
        assert bridge._generation == generation
    finally:
        bridge.close()


def test_unchanged_key_401_still_marks_cloud_key_invalid(tmp_path):
    from realtime_subtitle.remote.cloud_client import CloudError

    key = "gsk_test_fake_still_invalid_key_8a107c"
    key_path = tmp_path / "groq.key"
    key_path.write_text(key + "\n", encoding="utf-8")
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge._on_cloud_exception(
            CloudError(status=401, reason="unauthorized", exc_type="HTTPStatus", secret=key),
            bridge._generation,
        )
        assert bridge.routing.cloud_state == "disabled_invalid_key"
        assert bridge.route_label() == design_tokens.TEXT_STATUS_CLOUD_KEY_INVALID
    finally:
        bridge.close()


def test_touching_unchanged_key_file_does_not_advance_generation(tmp_path):
    key = "gsk_test_fake_unchanged_key_3779f2"
    key_path = tmp_path / "groq.key"
    key_path.write_text(key + "\n", encoding="utf-8")
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge._recover_routing()
        generation = bridge._generation
        stat = key_path.stat()
        os.utime(key_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert bridge._generation == generation
    finally:
        bridge.close()


def test_replaced_key_does_not_advance_node_session_generation(tmp_path, monkeypatch):
    """换 key 不能推进桥的代次：它同时是 NodeClient 的会话代次。"""
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    old_key = "gsk_test_fake_hybrid_old_3f9a1c"
    new_key = "gsk_test_fake_hybrid_new_7b2d40"
    key_path = tmp_path / "groq.key"
    key_path.write_text(old_key + "\n", encoding="utf-8")
    cloud = _Cloud()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=True,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        prepare=lambda cfg: ("100.64.0.8", object()),
        node_client_factory=lambda cfg, info, callbacks: _Node(),
        node_entries=[
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    closed = []
    bridge._close_node = lambda: closed.append(True)
    try:
        generation = bridge._generation
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert bridge._generation == generation
        assert closed == []
        assert cloud.cancelled == 1
    finally:
        bridge.close()


def test_key_change_cancels_cloud_once_and_is_idempotent(tmp_path):
    old_key = "gsk_test_fake_once_old_5d0e12"
    new_key = "gsk_test_fake_once_new_9c4b77"
    key_path = tmp_path / "groq.key"
    key_path.write_text(old_key + "\n", encoding="utf-8")
    cloud = _Cloud()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert cloud.cancelled == 0
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert cloud.cancelled == 1
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert cloud.cancelled == 1
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert cloud.cancelled == 1
    finally:
        bridge.close()


def test_401_with_new_key_fingerprint_after_recovery_marks_invalid(tmp_path):
    from realtime_subtitle.remote.cloud_client import CloudError

    old_key = "gsk_test_fake_recovering_old_1e6a90"
    new_key = "gsk_test_fake_recovering_new_8f3c25"
    key_path = tmp_path / "groq.key"
    key_path.write_text(old_key + "\n", encoding="utf-8")
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge._on_cloud_exception(
            CloudError(status=401, reason="unauthorized", exc_type="HTTPStatus", secret=old_key),
            bridge._generation,
        )
        assert bridge.routing.cloud_state == "disabled_invalid_key"
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert bridge.routing.tier() == "cloud"
        # 恢复后新 key 自己被打回 401，仍必须判成失效。
        bridge._on_cloud_exception(
            CloudError(status=401, reason="unauthorized", exc_type="HTTPStatus", secret=new_key),
            bridge._generation,
        )
        assert bridge.routing.cloud_state == "disabled_invalid_key"
        assert bridge.route_label() == design_tokens.TEXT_STATUS_CLOUD_KEY_INVALID
    finally:
        bridge.close()


def test_key_change_while_cloud_off_does_not_re_enable_cloud(tmp_path):
    old_key = "gsk_test_fake_off_old_2a7f41"
    new_key = "gsk_test_fake_off_new_6e04b8"
    key_path = tmp_path / "groq.key"
    key_path.write_text(old_key + "\n", encoding="utf-8")
    cloud = _Cloud()
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: cloud,
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        bridge.set_prefs(False, False)
        assert bridge.routing.cloud_enabled is False
        assert cloud.cancelled == 1
        key_path.write_text(new_key + "\n", encoding="utf-8")
        bridge._routing_checked_at = None
        bridge._recover_routing()
        assert bridge.routing.cloud_enabled is False
        assert bridge.routing.cloud_state == "off"
        assert cloud.cancelled == 1
    finally:
        bridge.close()


def test_bad_utf8_key_file_does_not_break_bridge_or_recover(tmp_path):
    key_path = tmp_path / "groq.key"
    key_path.write_bytes(b"\xff\xfe\x00\x80garbage")
    bridge, _ui, _translator = _open(
        cloud_on=True,
        node_on=False,
        cloud_client_factory=lambda key_path=None: _Cloud(),
        segmenter_factory=_Segmenter,
        ledger_factory=lambda path, routing=None: _Ledger(path, routing),
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        key_path=key_path,
        quota_path=tmp_path / "quota.json",
    )
    try:
        assert bridge._cloud_key_digest is None
        bridge._routing_checked_at = None
        bridge._recover_routing()
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        bridge.flush()
        assert bridge._cloud_key_digest is None
        assert bridge.routing.cloud_state == "available"
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


def _open_node_with_callbacks(tmp_path, monkeypatch):
    """节点桥 + 每次连接留下的 (node, callbacks)，用来模拟会话内的服务端事件。"""
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    built = []

    def factory(cfg, info, callbacks):
        node = _Node()
        built.append((node, callbacks))
        return node

    bridge, ui, translator = _open(
        cloud_on=False,
        node_on=True,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        prepare=lambda cfg: ("100.64.0.8", object()),
        node_client_factory=factory,
        node_entries=[
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
    )
    return bridge, ui, translator, built


def _reconnect_node(bridge):
    bridge._node_backoff_until = 0
    bridge.feed_audio(np.zeros(320, dtype=np.float32))
    bridge.flush()


def test_node_reconnect_same_sentence_id_does_not_overwrite_previous_session(tmp_path, monkeypatch):
    """节点句 id 每会话重编号：重连后 id=0 的译文不能写到上一会话的行上。"""
    bridge, ui, _translator, built = _open_node_with_callbacks(tmp_path, monkeypatch)
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        assert len(built) == 1
        generation = bridge._generation
        _node1, cb1 = built[0]
        cb1.on_final("desk", generation, 0, "Hallo", 1.0, 2.0)
        bridge.flush()
        assert [line["src"] for line in ui.lines] == ["Hallo"]

        cb1.on_closed("desk", generation, ConnectionError("gone"))
        _reconnect_node(bridge)
        assert len(built) == 2
        _node2, cb2 = built[1]

        cb2.on_final("desk", generation, 0, "Tschüss", 3.0, 4.0)
        bridge.flush()
        assert [line["src"] for line in ui.lines] == ["Hallo", "Tschüss"]

        cb2.on_translation("desk", generation, 0, "再见")
        bridge.flush()
        assert ui.lines[0]["dst"] == "中文译文"
        assert ui.lines[1]["dst"] == "再见"
    finally:
        bridge.close()


def test_node_reconnect_does_not_inherit_translate_failed_ids(tmp_path, monkeypatch):
    """上一会话翻译失败的 id 不能把新会话同号句的节点译文挡掉。"""
    bridge, ui, _translator, built = _open_node_with_callbacks(tmp_path, monkeypatch)
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        generation = bridge._generation
        _node1, cb1 = built[0]
        cb1.on_translate_failed("desk", generation, 1)

        cb1.on_closed("desk", generation, ConnectionError("gone"))
        _reconnect_node(bridge)
        assert len(built) == 2
        _node2, cb2 = built[1]

        cb2.on_final("desk", generation, 1, "Tschüss", 3.0, 4.0)
        bridge.flush()
        assert ui.lines[-1]["dst"] == "中文译文"

        cb2.on_translation("desk", generation, 1, "节点译文")
        bridge.flush()
        assert ui.lines[-1]["dst"] == "节点译文"
    finally:
        bridge.close()


def test_node_reconnect_translation_before_final_uses_new_session_row(tmp_path, monkeypatch):
    """新会话的译文先于 final 到达时，不能落到上一会话同号句的行上。"""
    bridge, ui, _translator, built = _open_node_with_callbacks(tmp_path, monkeypatch)
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        generation = bridge._generation
        _node1, cb1 = built[0]
        cb1.on_final("desk", generation, 0, "Hallo", 1.0, 2.0)
        bridge.flush()

        cb1.on_closed("desk", generation, ConnectionError("gone"))
        _reconnect_node(bridge)
        _node2, cb2 = built[1]

        cb2.on_translation("desk", generation, 0, "再见")
        bridge.flush()
        assert ui.lines[0]["dst"] == "中文译文"

        cb2.on_final("desk", generation, 0, "Tschüss", 3.0, 4.0)
        bridge.flush()
        assert [line["src"] for line in ui.lines] == ["Hallo", "Tschüss"]
        assert ui.lines[0]["dst"] == "中文译文"
        assert ui.lines[1]["dst"] == "再见"
    finally:
        bridge.close()


def test_late_events_from_previous_node_session_are_ignored(tmp_path, monkeypatch):
    """旧连接迟到的 closed/translation 不能影响重连后的新连接。"""
    bridge, ui, _translator, built = _open_node_with_callbacks(tmp_path, monkeypatch)
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        generation = bridge._generation
        _node1, cb1 = built[0]
        cb1.on_final("desk", generation, 0, "Hallo", 1.0, 2.0)
        bridge.flush()

        cb1.on_closed("desk", generation, ConnectionError("gone"))
        _reconnect_node(bridge)
        assert len(built) == 2
        node2, cb2 = built[1]
        assert bridge._node_client is node2

        cb1.on_closed("desk", generation, ConnectionError("late"))
        cb1.on_translation("desk", generation, 0, "旧译文")
        bridge.flush()
        assert bridge._node_client is node2
        assert [line["src"] for line in ui.lines] == ["Hallo"]
        assert ui.lines[0]["dst"] == "中文译文"
    finally:
        bridge.close()


def test_node_loading_during_connect_reaches_status_line(tmp_path, monkeypatch):
    """loading 在 connect 还没返回时就到（冷启动续等）：新连接还没挂上桥，也不能当成旧连接丢掉。"""
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    statuses: list[str] = []
    holder = {}

    class _LoadingNode(_Node):
        async def connect(self):
            holder["callbacks"].on_loading("desk", holder["bridge"]._generation)

    def factory(cfg, info, callbacks):
        holder["callbacks"] = callbacks
        return _LoadingNode()

    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=True,
        ui_status=statuses.append,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        prepare=lambda cfg: ("100.64.0.8", object()),
        node_client_factory=factory,
        node_entries=[
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
    )
    holder["bridge"] = bridge
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        bridge.flush()
        assert "desk" + design_tokens.TEXT_NODE_LOADING_SUFFIX in statuses
    finally:
        bridge.close()


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
        bridge._announce_route()
        assert bridge._announced == (
            design_tokens.TEXT_STATUS_LOCAL + design_tokens.TEXT_STATUS_CLOUD_COOLDOWN_SUFFIX)
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "cooldown_rate_limited"
        clock["t"] += 0.4
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "cooldown_rate_limited"
        clock["t"] += 5
        bridge.feed_audio(np.zeros(160, dtype=np.float32))
        assert bridge.routing.cloud_state == "probe"
        # 冷却结束（probe 会立刻按云端档再试）后状态行恢复原文案。
        assert bridge._announced == design_tokens.TEXT_STATUS_CLOUD

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


def _open_slow_node(tmp_path, monkeypatch, connect):
    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    box = {}

    def factory(cfg, info, callbacks):
        node = _Node()
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
        prepare=lambda cfg: ("100.64.0.8", object()),
        node_client_factory=factory,
        node_entries=[
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
    )
    return bridge, box


def test_node_connect_outer_timeout_covers_loading_cap(tmp_path, monkeypatch):
    """P1：connect 外层兜底超时不能比 connect 自己的截止（loading 最长 60s）先到期。"""
    from realtime_subtitle.remote import node_client

    async def connect():
        return None

    bridge, _box = _open_slow_node(tmp_path, monkeypatch, connect)
    seen = []
    real_run = bridge._run_on_loop

    def spy(coro, timeout=5):
        seen.append((getattr(coro, "__qualname__", ""), timeout))
        return real_run(coro, timeout=timeout)

    bridge._run_on_loop = spy
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        connects = [t for name, t in seen if name.endswith("connect")]
        assert len(connects) == 1
        assert connects[0] > node_client.READY_LOADING_MAX_SECONDS
        assert bridge._node_client is _box["node"]
    finally:
        bridge.close()


def test_node_connect_slower_than_5s_is_not_abandoned(tmp_path, monkeypatch):
    """P1：节点冷启动 >5s 才 ready 时，桌面端不能 5 秒就放弃（真实计时，约 5.5s）。"""
    import asyncio

    async def connect():
        await asyncio.sleep(5.5)

    bridge, box = _open_slow_node(tmp_path, monkeypatch, connect)
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        assert bridge._node_client is box["node"]
        assert box["node"].closed is False
        assert bridge._node_backoff_until == 0.0
    finally:
        bridge.close()


def test_route_status_never_names_the_wrong_node_while_connecting(tmp_path, monkeypatch):
    """P1 问题 3：打分挑中的是第二个候选时，连接返回那一刻状态行不能先闪一下第一个候选的名字。"""
    from types import SimpleNamespace

    monkeypatch.setattr(
        "realtime_subtitle.remote.router.read_self_node_id", lambda: "this-machine")
    monkeypatch.setattr(
        RemoteRouter, "select_best_node", lambda self, cands, now: SimpleNamespace(id="desk2"))
    statuses: list[str] = []
    bridge, _ui, _translator = _open(
        cloud_on=False,
        node_on=True,
        cloud_client_factory=_boom,
        segmenter_factory=_boom,
        ledger_factory=_boom,
        router_factory=lambda **kwargs: RemoteRouter(**kwargs),
        prepare=lambda cfg: ("100.64.0.8", object()),
        node_client_factory=lambda cfg, info, callbacks: _Node(),
        node_entries=[
            NodeConfig(id="desk", node_id="tk007nodeid0001", url="ws://100.64.0.8:8791", token_file="x"),
            NodeConfig(id="desk2", node_id="tk007nodeid0002", url="ws://100.64.0.9:8791", token_file="x"),
        ],
        quota_path=tmp_path / "quota.json",
        ui_status=statuses.append,
    )
    try:
        bridge.feed_audio(np.zeros(320, dtype=np.float32))
        assert bridge.router.current_node_id == "desk2"
        assert "路由：混合·desk" not in statuses, statuses
        assert statuses[-1] == "路由：混合·desk2", statuses
    finally:
        bridge.close()
