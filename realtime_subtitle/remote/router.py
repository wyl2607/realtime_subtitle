from __future__ import annotations

import abc
import logging
import os
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from realtime_subtitle.remote.node_client import NodeConfig, NodeInfo
from realtime_subtitle.remote.routing_rules import RoutingState

logger = logging.getLogger("realtime_subtitle.remote.router")


# --- 1. Provider 抽象与桩 ---


class Provider(abc.ABC):
    """
    RFC ARCH-02 Provider 抽象接口。
    云端、节点、本机三种不同后端实现同一种 Provider 语义。
    """

    @property
    @abc.abstractmethod
    def tier(self) -> str:
        """'cloud', 'node', 或 'local'"""
        pass

    @abc.abstractmethod
    def available(self) -> bool:
        """后端当前是否可用"""
        pass


class CloudProviderStub(Provider):
    """
    云端 Provider 桩（由 TK-006B 提供完整实现，本子 TK 用桩）。
    """

    def __init__(
        self,
        has_key: bool = False,
        cloud_enabled: bool = True,
        cloud_state: str = "available",
    ) -> None:
        self.has_key = has_key
        self.cloud_enabled = cloud_enabled
        self.cloud_state = cloud_state

    @property
    def tier(self) -> str:
        return "cloud"

    def available(self) -> bool:
        return self.has_key and self.cloud_enabled and self.cloud_state in {"available", "probe"}


class LocalProvider(Provider):
    """本机识别 Provider，永远可用。"""

    @property
    def tier(self) -> str:
        return "local"

    def available(self) -> bool:
        return True


# --- 2. 排除自己（不连自己） ---


def read_self_node_id() -> str | None:
    """
    读取本机 node_id：
    - Windows 读 %LOCALAPPDATA%\\rs-node\\node_id
    - 非 Windows 读 ~/rs-node/node_id
    - 读不到就不排除（返回 None）。
    """
    if sys.platform == "win32":
        local_appdata = os.environ.get("LOCALAPPDATA")
        if local_appdata:
            path = Path(local_appdata) / "rs-node" / "node_id"
        else:
            path = Path.home() / "AppData" / "Local" / "rs-node" / "node_id"
    else:
        path = Path.home() / "rs-node" / "node_id"

    try:
        if path.exists() and path.is_file():
            content = path.read_text(encoding="utf-8").strip()
            return content if content else None
    except Exception:
        return None
    return None


def is_self_node(node_id: str, self_node_id: str | None = None) -> bool:
    """判断给定 node_id 是否为本机节点。"""
    effective_self_id = self_node_id if self_node_id is not None else read_self_node_id()
    if not effective_self_id:
        return False
    return node_id == effective_self_id


# --- 3. 层内打分 RoutingDecider（与 Swift 对齐） ---


@dataclass
class RouteWeights:
    asr_rank: dict[str, float] = field(
        default_factory=lambda: {
            "whisper-large-v3-turbo": 40.0,
            "whisper-large-v3": 38.0,
            "fake": 30.0,
        }
    )
    translator_rank: dict[str, float] = field(
        default_factory=lambda: {
            "apple": 20.0,
            "fake": 18.0,
        }
    )
    speed_k: float = 20.0
    in_use_penalty: float = 12.0
    battery_penalty: float = 10.0
    rtt_penalty: float = 6.0
    local_under_p8_penalty: float = 1000.0
    no_network_bonus: float = 4.0
    migration_margin: float = 8.0


@dataclass
class RouteCandidate:
    id: str
    expected_node_id: str
    info: NodeInfo | None
    rtt_ms: float
    is_local: bool = False
    offline_until: float | None = None


@dataclass
class RouteScore:
    id: str
    score: float
    reason: str


class RoutingDecider:
    """
    节点打分器，与 Swift RoutingDecider 完全一致。
    """

    @staticmethod
    def score(
        candidate: RouteCandidate,
        now: float,
        local_accurate: bool = True,
        weights: RouteWeights | None = None,
    ) -> RouteScore | None:
        if weights is None:
            weights = RouteWeights()

        if candidate.offline_until is not None and candidate.offline_until > now:
            return None

        info = candidate.info
        if info is None or info.node_id != candidate.expected_node_id or info.busy:
            return None

        model = (info.asr.model or "") if info.asr else ""
        translator = info.translator
        asr = weights.asr_rank.get(model, 25.0)

        if translator in weights.translator_rank:
            tr = weights.translator_rank[translator]
        elif translator.startswith("ollama:"):
            tr = 16.0
        else:
            tr = 12.0

        rtf = (
            info.asr.rtf
            if (info.asr and info.asr.rtf is not None)
            else candidate.rtt_ms / 1000.0
        )

        penalties = 0.0
        if info.in_use:
            penalties += weights.in_use_penalty
        if not info.on_ac:
            penalties += weights.battery_penalty
        if candidate.rtt_ms > 100:
            penalties += weights.rtt_penalty
        if candidate.is_local and not local_accurate:
            penalties += weights.local_under_p8_penalty

        bonus = weights.no_network_bonus if candidate.is_local else 0.0
        score = asr + tr - weights.speed_k * rtf - penalties + bonus
        reason = (
            f"quality={asr + tr:.1f} speed={-weights.speed_k * rtf:.1f} "
            f"penalties={penalties:.1f} bonus={bonus:.1f} rtt_ms={candidate.rtt_ms:.0f}"
        )
        return RouteScore(id=candidate.id, score=score, reason=reason)

    @staticmethod
    def choose(
        candidates: Sequence[RouteCandidate],
        now: float,
        local_accurate: bool = True,
        weights: RouteWeights | None = None,
    ) -> RouteScore | None:
        scored = [
            RoutingDecider.score(c, now, local_accurate, weights)
            for c in candidates
        ]
        valid = [s for s in scored if s is not None]
        if not valid:
            return None
        return max(valid, key=lambda s: s.score)


# --- 4. 迁移门控 MigrationGate ---


@dataclass
class MigrationGate:
    """
    迁移门控：要求优势分达到 migration_margin 且连续 2 轮领先才允许迁移。
    """

    current_id: str | None = None
    pending_id: str | None = None
    consecutive_wins: int = 0
    weights: RouteWeights = field(default_factory=RouteWeights)

    def observe(self, best: RouteScore | None, current: RouteScore | None) -> bool:
        if (
            best is None
            or current is None
            or best.id == current.id
            or best.score < current.score + self.weights.migration_margin
        ):
            self.pending_id = None
            self.consecutive_wins = 0
            return False

        if self.pending_id == best.id:
            self.consecutive_wins += 1
        else:
            self.pending_id = best.id
            self.consecutive_wins = 1

        return self.consecutive_wins >= 2


# --- 5. 静音点判定 ---

SILENCE_THRESHOLD_RMS = 0.003
SILENCE_SECONDS_FOR_MIGRATION = 0.6


def calculate_pcm_rms(pcm_data: bytes) -> float:
    """计算 16kHz s16le PCM 的 RMS 音量。"""
    count = len(pcm_data) // 2
    if count == 0:
        return 0.0
    samples = struct.unpack(f"<{count}h", pcm_data[: count * 2])
    sum_sq = sum((s / 32768.0) ** 2 for s in samples)
    return (sum_sq / count) ** 0.5


def update_silence_run(
    current_silence: float,
    pcm_data: bytes,
    duration: float,
    threshold: float = SILENCE_THRESHOLD_RMS,
) -> float:
    """根据音频帧 RMS 更新连续静音时长。"""
    rms = calculate_pcm_rms(pcm_data)
    if rms < threshold:
        return current_silence + duration
    return 0.0


def can_migrate_at_silence(silence_run: float, threshold_sec: float = SILENCE_SECONDS_FOR_MIGRATION) -> bool:
    """静音点切流：连续静音达到阈值时才允许切节点。"""
    return silence_run >= threshold_sec


# --- 6. 回放缓冲（15s，首次接入回放，迁移不回放） ---

REPLAY_BUFFER_SECONDS = 15.0


@dataclass
class TimedAudioChunk:
    data: bytes
    start_seconds: float
    duration: float


class TimedAudioReplayBuffer:
    """
    路由音频回放缓冲（上限 15s）。
    """

    def __init__(self, max_seconds: float = REPLAY_BUFFER_SECONDS) -> None:
        self.max_seconds = max(0.0, max_seconds)
        self.frames: list[TimedAudioChunk] = []

    def append(self, chunk: TimedAudioChunk) -> None:
        if self.max_seconds <= 0 or chunk.duration <= 0:
            return
        self.frames.append(chunk)
        cutoff = chunk.start_seconds + chunk.duration - self.max_seconds
        while self.frames and (self.frames[0].start_seconds + self.frames[0].duration) <= cutoff:
            self.frames.pop(0)

    def frames_after(self, cursor: float | None) -> list[TimedAudioChunk]:
        if cursor is None:
            return list(self.frames)
        return [f for f in self.frames if (f.start_seconds + f.duration) > cursor]

    def clear(self) -> None:
        self.frames.clear()


def should_replay_buffered_audio(has_current_client: bool, has_current_node: bool) -> bool:
    """
    首次建会话回放 15s 缓冲；迁移不回放（同 TK-008 修法）。
    """
    return not has_current_client and not has_current_node


# --- 7. 分级路由器 RemoteRouter ---


@dataclass
class NodeStateRecord:
    info: NodeInfo | None = None
    rtf: float | None = None
    offline_until: float | None = None


class RemoteRouter:
    """
    分级路由器：
    - Tier 0: 云端 Groq（由 TK-006B 接入，本 TK 用桩）
    - Tier 1: 节点路由（RoutingDecider 打分，MigrationGate 静音点迁移，不连自己）
    - Tier 2: 本机兜底
    """

    def __init__(
        self,
        cloud_provider: Provider | None = None,
        node_configs: Sequence[NodeConfig] | None = None,
        self_node_id: str | None = None,
        local_accurate: bool = True,
        weights: RouteWeights | None = None,
    ) -> None:
        self.cloud_provider = cloud_provider or CloudProviderStub()
        self.local_provider = LocalProvider()
        self.node_configs = list(node_configs or [])
        self.self_node_id = self_node_id
        self.local_accurate = local_accurate
        self.weights = weights or RouteWeights()

        self.node_states: dict[str, NodeStateRecord] = {}
        self.gate = MigrationGate(weights=self.weights)
        self.replay_buffer = TimedAudioReplayBuffer(max_seconds=REPLAY_BUFFER_SECONDS)
        self.silence_run = 0.0

        self.current_node_id: str | None = None
        self.pending_migration_id: str | None = None
        self.node_enabled = True

    def select_tier(self) -> str:
        """调用 routing_rules 的 RoutingState 计算当前层级（Tier0 / Tier1 / Tier2）。"""
        has_healthy_node = self.has_available_node()
        state = RoutingState(
            has_key=getattr(self.cloud_provider, "has_key", False),
            cloud_enabled=getattr(self.cloud_provider, "cloud_enabled", True),
            cloud_state=getattr(self.cloud_provider, "cloud_state", "available"),
            node_enabled=self.node_enabled,
            node_healthy=has_healthy_node,
            node_is_self=False,
        )
        return state.tier()

    def filter_candidates(self) -> list[RouteConfigCandidate]:
        """过滤候选节点，排除本机 node_id。"""
        candidates: list[RouteConfigCandidate] = []
        for cfg in self.node_configs:
            if is_self_node(cfg.node_id, self.self_node_id):
                continue
            candidates.append(RouteConfigCandidate(cfg))
        return candidates

    def has_available_node(self) -> bool:
        """检查 Tier 1 是否有至少一个可用节点。"""
        return len(self.filter_candidates()) > 0

    def select_best_node(self, candidates: Sequence[RouteCandidate], now: float) -> RouteScore | None:
        """层内打分并挑选最佳节点。"""
        filtered = [c for c in candidates if not is_self_node(c.expected_node_id, self.self_node_id)]
        return RoutingDecider.choose(filtered, now=now, local_accurate=self.local_accurate, weights=self.weights)

    def observe_migration(self, best: RouteScore | None, current: RouteScore | None) -> bool:
        """观察迁移门控，领先 2 次且超过 margin 返回 True。"""
        return self.gate.observe(best, current)


class RouteConfigCandidate:
    def __init__(self, config: NodeConfig) -> None:
        self.config = config
