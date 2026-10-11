from __future__ import annotations

import time
from pathlib import Path

from realtime_subtitle.remote.node_client import (
    NodeASRInfo,
    NodeConfig,
    NodeIDMismatchError,
    NodeInfo,
    SelfNodeError,
)
from realtime_subtitle.remote.router import (
    CloudProviderStub,
    MigrationGate,
    NodeStateRecord,
    RemoteRouter,
    RouteCandidate,
    RouteScore,
    RouteWeights,
    RoutingDecider,
    TimedAudioChunk,
    TimedAudioReplayBuffer,
    can_migrate_at_silence,
    is_self_node,
    read_self_node_id,
    should_replay_buffered_audio,
    update_silence_run,
)


def _make_info(
    node_id: str = "n1",
    model: str = "whisper-large-v3-turbo",
    rtf: float = 0.08,
    translator: str = "apple",
    busy: bool = False,
    on_ac: bool = True,
    in_use: bool = False,
) -> NodeInfo:
    return NodeInfo(
        v=2,
        node_id=node_id,
        hw_hash="h",
        asr=NodeASRInfo(model=model, backend="mlx", rtf=rtf),
        translator=translator,
        busy=busy,
        on_ac=on_ac,
        in_use=in_use,
        worker="warm",
    )


# --- 1. Tier 分级选择测试（调用 routing_rules） ---


def test_tier_selection_cloud_first():
    cloud = CloudProviderStub(has_key=True, cloud_enabled=True, cloud_state="available")
    router = RemoteRouter(cloud_provider=cloud, node_configs=[])
    assert router.select_tier() == "cloud"


def test_tier_selection_fallback_to_node_when_cloud_unavailable():
    cloud = CloudProviderStub(has_key=False, cloud_enabled=True)
    cfg = NodeConfig(id="node1", node_id="n1", url="ws://100.64.0.1:8791", token_file="/tmp/t")
    router = RemoteRouter(cloud_provider=cloud, node_configs=[cfg])
    assert router.select_tier() == "node"


def test_tier_selection_fallback_to_local_when_no_nodes():
    cloud = CloudProviderStub(has_key=False, cloud_enabled=True)
    router = RemoteRouter(cloud_provider=cloud, node_configs=[])
    assert router.select_tier() == "local"


def test_tier_selection_fallback_to_local_when_node_disabled():
    cloud = CloudProviderStub(has_key=False, cloud_enabled=True)
    cfg = NodeConfig(id="node1", node_id="n1", url="ws://100.64.0.1:8791", token_file="/tmp/t")
    router = RemoteRouter(cloud_provider=cloud, node_configs=[cfg])
    router.node_enabled = False
    assert router.select_tier() == "local"


# --- 2. RoutingDecider 打分测试（与 Swift 对齐） ---


def test_routing_decider_chooses_better_node():
    good_info = _make_info(node_id="n1", rtf=0.08)
    better_info = _make_info(node_id="n2", rtf=0.02)

    base = RouteCandidate(id="base", expected_node_id="n1", info=good_info, rtt_ms=20)
    better = RouteCandidate(id="better", expected_node_id="n2", info=better_info, rtt_ms=20)

    weights = RouteWeights()
    chosen = RoutingDecider.choose([base, better], now=0.0, local_accurate=True, weights=weights)
    assert chosen is not None
    assert chosen.id == "better"


def test_routing_decider_excludes_offline_busy_and_mismatch():
    good_info = _make_info(node_id="n1")
    busy_info = _make_info(node_id="n2", busy=True)

    # offline_until 在未来
    c_off = RouteCandidate(id="off", expected_node_id="n1", info=good_info, rtt_ms=10, offline_until=100.0)
    assert RoutingDecider.choose([c_off], now=1.0) is None

    # busy 节点排除
    c_busy = RouteCandidate(id="busy", expected_node_id="n2", info=busy_info, rtt_ms=10)
    assert RoutingDecider.choose([c_busy], now=0.0) is None

    # node_id 不匹配排除
    c_mismatch = RouteCandidate(id="mismatch", expected_node_id="wrong_id", info=good_info, rtt_ms=10)
    assert RoutingDecider.choose([c_mismatch], now=0.0) is None


def test_routing_decider_penalties_and_bonuses():
    ac_info = _make_info(node_id="n1", on_ac=True)
    battery_info = _make_info(node_id="n2", on_ac=False)

    c_ac = RouteCandidate(id="ac", expected_node_id="n1", info=ac_info, rtt_ms=10)
    c_batt = RouteCandidate(id="batt", expected_node_id="n2", info=battery_info, rtt_ms=10)

    score_ac = RoutingDecider.score(c_ac, now=0.0)
    score_batt = RoutingDecider.score(c_batt, now=0.0)
    assert score_ac is not None and score_batt is not None
    # 电池供电扣 10 分
    assert score_batt.score < score_ac.score

    # 本机节点加分与 P8 惩罚
    local_candidate = RouteCandidate(id="local_node", expected_node_id="n1", info=ac_info, rtt_ms=1, is_local=True)
    score_local_good = RoutingDecider.score(local_candidate, now=0.0, local_accurate=True)
    score_local_bad = RoutingDecider.score(local_candidate, now=0.0, local_accurate=False)
    assert score_local_good is not None and score_local_bad is not None
    assert score_local_good.score > score_ac.score  # 无网络加分
    assert score_local_bad.score < score_ac.score - 500  # P8 不满足大幅扣分


# --- 3. MigrationGate 测试 ---


def test_migration_gate_two_consecutive_wins_with_margin():
    gate = MigrationGate()
    c = RouteScore(id="base", score=50.0, reason="")
    b = RouteScore(id="better", score=70.0, reason="")

    # 首次领先不切
    assert not gate.observe(best=b, current=c)
    assert gate.consecutive_wins == 1

    # 连续第二次领先且差值 >= 8.0 切
    assert gate.observe(best=b, current=c)
    assert gate.consecutive_wins == 2

    # 差值未达 margin (8.0) 不切并重置
    small_better = RouteScore(id="small", score=55.0, reason="")
    assert not gate.observe(best=small_better, current=c)
    assert gate.consecutive_wins == 0


# --- 4. 回放缓冲测试（首次回放 15s，迁移不回放） ---


def test_should_replay_buffered_audio_logic():
    # 首次接入 / 回退本机后重连：无当前 client 无当前 node -> 回放
    assert should_replay_buffered_audio(has_current_client=False, has_current_node=False)

    # 已有当前节点进行迁移：不得回放历史音频
    assert not should_replay_buffered_audio(has_current_client=True, has_current_node=True)
    assert not should_replay_buffered_audio(has_current_client=True, has_current_node=False)
    assert not should_replay_buffered_audio(has_current_client=False, has_current_node=True)


def test_timed_audio_replay_buffer_max_15s_trim():
    buffer = TimedAudioReplayBuffer(max_seconds=15.0)
    for i in range(20):
        chunk = TimedAudioChunk(data=b"\x00" * 32000, start_seconds=float(i), duration=1.0)
        buffer.append(chunk)

    frames = buffer.frames_after(None)
    assert len(frames) == 15
    assert frames[0].start_seconds == 5.0

    # 按游标读取未发音频
    after_10 = buffer.frames_after(10.0)
    assert after_10[0].start_seconds == 10.0


# --- 5. 静音点切流判定测试 ---


def test_silence_detection_and_migration():
    # 静音 PCM (全 0)
    silent_pcm = b"\x00" * 3200
    silence_run = 0.0
    silence_run = update_silence_run(silence_run, silent_pcm, duration=0.3)
    assert silence_run == 0.3
    assert not can_migrate_at_silence(silence_run)

    silence_run = update_silence_run(silence_run, silent_pcm, duration=0.3)
    assert silence_run == 0.6
    assert can_migrate_at_silence(silence_run)

    # 出现声音 (非静音) 连续静音归零
    loud_pcm = b"\x7f\x50" * 1600
    silence_run = update_silence_run(silence_run, loud_pcm, duration=0.1)
    assert silence_run == 0.0
    assert not can_migrate_at_silence(silence_run)


# --- 6. 不连自己测试（node_id 判据） ---


def test_self_node_exclusion_from_file(tmp_path: Path, monkeypatch):
    import realtime_subtitle.node.info as node_info

    home = tmp_path / "home"
    state = tmp_path / "state"
    home.mkdir()
    state.mkdir()
    legacy = home / "rs-node"
    legacy.mkdir()
    (legacy / "node_id").write_text("legacy_id\n", encoding="utf-8")
    (state / node_info.NODE_ID_FILENAME).write_text("my_self_node_id\n", encoding="utf-8")

    monkeypatch.setattr(Path, "home", lambda: home)
    monkeypatch.setattr(node_info, "STATE_DIR", state)

    self_id = read_self_node_id()
    assert self_id == "my_self_node_id"

    assert is_self_node("my_self_node_id", self_node_id="my_self_node_id")
    assert not is_self_node("other_node_id", self_node_id="my_self_node_id")


def test_router_skips_self_node_error():
    cfg = NodeConfig(id="probed", node_id="remote_1", url="ws://100.64.0.9:8791", token_file="/tmp/t")
    other = NodeConfig(id="other", node_id="remote_2", url="ws://100.64.0.8:8791", token_file="/tmp/t")
    router = RemoteRouter(node_configs=[cfg, other])

    assert router.should_skip_node(cfg.node_id, SelfNodeError("local")) is True
    kept = router.configs_skipping_self({"probed": SelfNodeError("local")})
    assert [item.id for item in kept] == ["other"]

    # 其他探测错误不当作「跳过自己」
    kept_mismatch = router.configs_skipping_self({"probed": NodeIDMismatchError("mismatch")})
    assert [item.id for item in kept_mismatch] == ["probed", "other"]


def test_has_available_node_false_when_all_offline():
    cfg_a = NodeConfig(id="a", node_id="n1", url="ws://100.64.0.1:8791", token_file="/tmp/t")
    cfg_b = NodeConfig(id="b", node_id="n2", url="ws://100.64.0.2:8791", token_file="/tmp/t")
    now = 1_000.0
    router = RemoteRouter(
        cloud_provider=CloudProviderStub(has_key=False),
        node_configs=[cfg_a, cfg_b],
    )
    router.node_states["a"] = NodeStateRecord(offline_until=now + 10)
    router.node_states["b"] = NodeStateRecord(offline_until=now + 20)
    assert router.has_available_node(now=now) is False

    # 有一个已过期的离线窗口，节点恢复可用
    router.node_states["b"] = NodeStateRecord(offline_until=now - 1)
    assert router.has_available_node(now=now) is True

    # 墙钟时间下全部仍在离线窗口内时，层级落到本机
    router.node_states["a"] = NodeStateRecord(offline_until=time.time() + 3600)
    router.node_states["b"] = NodeStateRecord(offline_until=time.time() + 3600)
    assert router.has_available_node() is False
    assert router.select_tier() == "local"


def test_router_excludes_self_node_candidate():
    cfg_self = NodeConfig(id="self", node_id="self_123", url="ws://100.64.0.1:8791", token_file="/tmp/t")
    cfg_other = NodeConfig(id="other", node_id="remote_456", url="ws://100.64.0.2:8791", token_file="/tmp/t")

    router = RemoteRouter(
        node_configs=[cfg_self, cfg_other],
        self_node_id="self_123",
    )

    filtered = router.filter_candidates()
    assert len(filtered) == 1
    assert filtered[0].config.id == "other"
