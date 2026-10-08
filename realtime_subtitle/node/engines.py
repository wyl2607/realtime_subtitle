"""节点 worker 的识别 / 翻译引擎接口与 Mac 实现。

接口（`AsrEngine` / `Translator`）是为了让 worker 的管道逻辑能用假引擎测，
也让下一期 Windows 节点只换实现、不动 worker（RFC 架构选择第 2、3 条）。

☠️ 刻意不用 `WhisperQueueTranslator`：它把识别、断句、翻译和 UI 回调绑在一起，
时间戳在翻译层丢失（RFC「已被驳回的备选项」第一行）。这里识别只走
`asr.backends.create_whisper_model`（MLX 下所有调用已收进单线程执行器，见
`asr/mlx_backend.py` 的 `_on_mlx_thread`），翻译只走 Apple / Ollama 适配器。

日志规则（S7）：本模块不写任何日志；失败一律用异常类名向上报，异常消息可能
夹带原文，不进日志。
"""
from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

import realtime_subtitle.config as config
from realtime_subtitle.translate.apple_translate import AppleTranslator
from realtime_subtitle.translate.text_rules import (
    _no_space_language,
    _split_sentences,
    _strip_translator_note,
)

SAMPLE_RATE = 16000
# Apple 单句通常 0.12s；超时留宽是为了冷启动，超过就算失败，不拖住后面的句子
APPLE_TIMEOUT_S = 5.0


@dataclass
class Utterance:
    """一句识别结果。start/end 相对于送进 `transcribe` 的那段音频（秒）。"""
    start: float
    end: float
    text: str


class AsrEngine(ABC):
    @abstractmethod
    def load(self) -> None:
        """加载模型（阻塞）。worker 在 hello 之后、发 ready 之前调用。"""

    @abstractmethod
    def transcribe(self, audio: np.ndarray, language: str) -> list[Utterance]:
        """audio: float32、16k、单声道的**一个**语音段。返回已按句拆好的结果。"""

    @abstractmethod
    def info(self) -> dict:
        """P1 `asr` 字段的内容：{"model","backend","rtf"}。"""


class Translator(ABC):
    #: P1 `translator` 字段："apple" 或 "ollama:<model>"
    name: str

    @abstractmethod
    def translate(self, text: str, src: str, dst: str) -> str | None:
        """失败返回 None（调用方只发 status，不重试、不阻塞后面的句子）。"""

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- Whisper


def _is_hallucination(text: str) -> bool:
    """整段丢弃的判定，口径与 OnlineASRProcessor._is_hallucination 一致。

    不直接调用那个方法：它是实例方法，而实时管线的实例带着一堆流式状态，节点
    不应该为了一个纯函数去构造它。长度门和黑名单都来自同一处（类常量 + config），
    不会漂移。
    """
    from realtime_subtitle.asr.streaming_asr import OnlineASRProcessor

    stripped = (text or "").strip()
    if OnlineASRProcessor._weighted_len(stripped) > OnlineASRProcessor.HALLUCINATION_MAX_CHARS:
        return False
    lowered = stripped.lower()
    return any(p in lowered for p in config.HALLUCINATION_BLACKLIST)


def utterances_from_segments(segments, language: str) -> list[Utterance]:
    """Whisper 的 segment（带词时间戳）→ 按句拆分的 Utterance。

    断句交给 text_rules（德语缩写/序数/年份规则都在那里），再用字符偏移把每个
    句子映射回词，句子的起止时间取首词 start / 末词 end。这样一个 Whisper
    segment 里有两句话、或一句话跨两个 segment，都能得到准确的句级时间。
    """
    strip_spaces = _no_space_language(language)
    words: list[tuple[float, float, str]] = []
    for seg in segments:
        if getattr(seg, "no_speech_prob", 0.0) > 0.9:
            continue
        if _is_hallucination(seg.text):
            continue
        for w in seg.words:
            words.append((w.start, w.end, w.word.lstrip() if strip_spaces else w.word))
    if not words:
        return []

    text = "".join(w for _, _, w in words)
    # 每个词在 text 里的字符区间（左端含前导空格，匹配时用词首非空白位置）
    spans = []
    pos = 0
    for _s, _e, w in words:
        spans.append((pos + (len(w) - len(w.lstrip())), pos + len(w)))
        pos += len(w)

    sentences, rest = _split_sentences(text, final=True, lang=language)
    if rest:
        sentences.append(rest)

    out: list[Utterance] = []
    cursor = 0
    for sent in sentences:
        at = text.find(sent, cursor)
        if at < 0:
            continue
        end = at + len(sent)
        cursor = end
        idx = [i for i, (b, _x) in enumerate(spans) if at <= b < end]
        if not idx:
            continue
        start_t = words[idx[0]][0]
        end_t = max(words[idx[-1]][1], start_t)
        out.append(Utterance(start_t, end_t, sent))
    return out


class WhisperEngine(AsrEngine):
    """走 `create_whisper_model`：Apple Silicon 上是 MLX turbo，其他平台是 faster-whisper。

    两个后端的 `transcribe` 签名一致（mlx_backend.MlxWhisperModel 就是照着
    faster-whisper 写的适配层），所以这一个类够用；Windows 节点不需要新实现。
    """

    def __init__(self):
        self._model = None
        self._backend = ""
        self._model_name = ""
        self._rtf: float | None = None

    def load(self) -> None:
        from realtime_subtitle.asr.backends import create_whisper_model, selected_whisper_backend

        self._backend = selected_whisper_backend()
        self._model_name = (config.WHISPER_MLX_REPO if self._backend == "mlx"
                            else config.WHISPER_MODEL)
        self._model = create_whisper_model()

    def transcribe(self, audio: np.ndarray, language: str) -> list[Utterance]:
        t0 = time.perf_counter()
        # vad_filter=False：段本身就是 Silero 切出来的，再过一遍 VAD 只是白耗时，
        # 还会让词时间戳多一层映射。condition_on_previous_text=False：每段独立
        # 识别，上一段的错字不该带进这一段
        segments, _info = self._model.transcribe(
            audio,
            language=language,
            task=config.WHISPER_TASK,
            word_timestamps=True,
            condition_on_previous_text=False,
            vad_filter=False,
        )
        out = utterances_from_segments(list(segments), language)
        dur = len(audio) / SAMPLE_RATE
        if dur > 0:
            # 滑动平均：P1 要求 rtf 随会话持续更新
            cur = (time.perf_counter() - t0) / dur
            self._rtf = cur if self._rtf is None else 0.7 * self._rtf + 0.3 * cur
        return out

    def info(self) -> dict:
        return {
            "model": self._model_name,
            "backend": self._backend,
            "rtf": None if self._rtf is None else round(self._rtf, 3),
        }


# ---------------------------------------------------------------- 翻译


class AppleBackend(Translator):
    name = "apple"

    def __init__(self, helper: AppleTranslator):
        self._helper = helper

    def translate(self, text: str, src: str, dst: str) -> str | None:
        return self._helper.translate(text, src, dst, APPLE_TIMEOUT_S)

    def close(self) -> None:
        self._helper.close()


_LANG_NAMES = {"de": "德语", "en": "英语", "zh": "中文", "ja": "日语", "da": "丹麦语"}


class OllamaBackend(Translator):
    """Ollama 回退。

    S1：构造时先过 `_assert_local_ollama`（拒绝指向本机之外的配置），之后每个
    请求的地址只经 `ollama_url()` 取——那里返回的是校验时钉住的 IP 字面量，
    DNS 事后改指向也搬不动。不要在这里直接读 `config.OLLAMA_BASE_URL`。
    """

    def __init__(self):
        import requests

        from realtime_subtitle.translate import translator_queue

        # S1：节点是服务端，没有「允许远端 Ollama」的逃生口。该开关会让下面的
        # 校验放行且不钉地址，转录可能发往远端，所以开着就直接拒绝构造
        if getattr(config, "ALLOW_REMOTE_OLLAMA", False):
            raise RuntimeError("remote_ollama_not_allowed_on_node")
        # 非本机会抛 RemoteOllamaRefused；解析失败返回 False，此时 ollama_url()
        # 会拒绝给出地址，所以这里直接失败比等到首个请求再失败更早暴露
        if not translator_queue._assert_local_ollama(config.OLLAMA_BASE_URL):
            raise RuntimeError("ollama_not_verified_local")
        self._tq = translator_queue
        # trust_env=False：不读 HTTP(S)_PROXY / 系统代理，回环请求不经任何代理
        self._session = requests.Session()
        self._session.trust_env = False
        self._model = config.OLLAMA_MODEL
        self.name = f"ollama:{self._model}"

    def translate(self, text: str, src: str, dst: str) -> str | None:
        s, d = _LANG_NAMES.get(src, src), _LANG_NAMES.get(dst, dst)
        prompt = (
            f"你是{s}字幕翻译。请把下面这一条字幕翻译成自然、准确的{d}。\n"
            f"只翻译当前这一条，不要补充、解释、拒答，也不要输出{s}原文；"
            f"数字、人名、机构名和地名不得改写。\n\n"
            f"{s}原文：\n{text}\n\n{d}译文："
        )
        try:
            resp = self._session.post(
                f"{self._tq.ollama_url()}/api/generate",
                json={
                    "model": self._model,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "keep_alive": getattr(config, "OFFLINE_OLLAMA_KEEP_ALIVE", "10m"),
                    # 与翻译/查词共用 num_ctx，否则 Ollama 会换 runner 重装模型
                    # （CLAUDE.md 第 4 节第 21 条）
                    "options": {"temperature": 0.2, "num_predict": 512,
                                "num_ctx": getattr(config, "OLLAMA_NUM_CTX", 4096)},
                },
                timeout=max(30, int(getattr(config, "OLLAMA_TIMEOUT_COLD", 90))),
            )
            resp.raise_for_status()
            out = _strip_translator_note(str(resp.json().get("response", "")).strip())
        except Exception:
            return None
        return out or None

    def close(self) -> None:
        self._session.close()


def select_translator(src: str, dst: str, apple_factory=AppleTranslator,
                      ollama_factory=OllamaBackend) -> Translator:
    """RFC 架构选择第 3 条：先 Apple，语言对状态不是 installed 才退回 Ollama。

    helper 不存在或没回应时 status 为 None，同样走 Ollama。
    """
    apple = apple_factory()
    if apple.status(src, dst) == "installed":
        return AppleBackend(apple)
    apple.close()
    return ollama_factory()

