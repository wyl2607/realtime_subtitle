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

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass

import numpy as np

import realtime_subtitle.config as config
from realtime_subtitle.translate.apple_translate import AppleTranslator
from realtime_subtitle.translate.text_rules import (
    _no_space_language,
    _split_sentences,
    _normalize_clock_times,
    _strip_translator_note,
    to_simplified_if_target,
)

SAMPLE_RATE = 16000
# Apple 单句通常 0.12s；超时留宽是为了冷启动，超过就算失败，不拖住后面的句子
APPLE_TIMEOUT_S = 5.0

# 节点翻译请求的 Ollama 租期。见下方 post 处注释。
NODE_OLLAMA_KEEP_ALIVE = "3m"
# keep_alive 3 分钟内用过的模型还在显存里，预热跳过；留 30 秒余量
WARM_SKIP_S = 150.0

# 节点识别是否开启词级时间戳。False 时每个 segment 按字符位置插值出「伪词」时间，
# 切句逻辑与真词完全相同；客户端只用 a0/a1 做秒级重叠/回放判定，±0.5s 可接受。
# 真机实测（M4 mini，2 分钟 tagesschau 切 7s 段，MLX turbo）：
# word_timestamps=True：p50 1.78s；False：p50 0.98s（两轮一致）。
NODE_WORD_TIMESTAMPS = False

_log = logging.getLogger("node.engines")


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

    def translate_ctx(self, text: str, src: str, dst: str, context: str = "") -> str | None:
        """带上一句原文的翻译。默认忽略上下文（Apple 翻译不吃上下文）。"""
        return self.translate(text, src, dst)

    def warm(self) -> None:
        """会话开始时预热（后台线程调用，不阻塞 ready）。默认什么都不做。"""

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


def _pseudo_words(seg, strip_spaces: bool) -> list[tuple[float, float, str]]:
    """没有词时间戳时，把一个 segment 的文本拆成「伪词」，按字符位置在 [start, end]
    里线性插值时间。拉丁语系按「前导空白+非空白」切，保持与 Whisper 词带前导空格一致；
    无空格语言逐字。之后走与真词完全相同的切句与映射逻辑。"""
    text = seg.text or ""
    toks = list(text.replace(" ", "")) if strip_spaces else re.findall(r"\s*\S+", text)
    total = sum(len(t) for t in toks)
    t0, t1 = float(seg.start), float(seg.end)
    if total == 0 or t1 < t0:
        return []
    out: list[tuple[float, float, str]] = []
    pos = 0
    for t in toks:
        a = t0 + (t1 - t0) * pos / total
        pos += len(t)
        out.append((a, t0 + (t1 - t0) * pos / total, t))
    return out


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
        if getattr(seg, "words", None):
            for w in seg.words:
                words.append((w.start, w.end, w.word.lstrip() if strip_spaces else w.word))
        else:
            words.extend(_pseudo_words(seg, strip_spaces))
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
        word_ts = NODE_WORD_TIMESTAMPS
        segments, _info = self._model.transcribe(
            audio,
            language=language,
            task=config.WHISPER_TASK,
            word_timestamps=word_ts,
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


# ---------------------------------------------------------------- 数字一致性
# 只做「原文数字 ⊆ 译文数字」：译文多出来的数字（如 Mai→5 月、一些里的「一」）不算错，
# 少了或变了（2.1 → 1.2）才重试。冠词 ein/eine 不当数字，只认 eins。

_DE_UNITS = {
    "null": 0, "eins": 1, "zwei": 2, "drei": 3, "vier": 4, "fünf": 5, "sechs": 6,
    "sieben": 7, "acht": 8, "neun": 9, "zehn": 10, "elf": 11, "zwölf": 12,
    "dreizehn": 13, "vierzehn": 14, "fünfzehn": 15, "sechzehn": 16, "siebzehn": 17,
    "achtzehn": 18, "neunzehn": 19,
}
_DE_TENS = {
    "zwanzig": 20, "dreißig": 30, "vierzig": 40, "fünfzig": 50, "sechzig": 60,
    "siebzig": 70, "achtzig": 80, "neunzig": 90,
}
# 复合词里的个位用词干：einundzwanzig、siebenundzwanzig
_DE_STEMS = {"ein": 1, **{k: v for k, v in _DE_UNITS.items() if 2 <= v <= 9}}


def _de_word_number(word: str) -> int | None:
    w = word.lower()
    if w in _DE_UNITS:
        return _DE_UNITS[w]
    if w in _DE_TENS:
        return _DE_TENS[w]
    if w in ("hundert", "einhundert"):
        return 100
    if w in ("tausend", "eintausend"):
        return 1000
    if "und" in w:
        unit, _, tens = w.partition("und")
        if unit in _DE_STEMS and tens in _DE_TENS:
            return _DE_STEMS[unit] + _DE_TENS[tens]
    for stem, v in _DE_STEMS.items():
        if v >= 2 and w == stem + "hundert":
            return v * 100
    return None


def _canon(value: float) -> str:
    return str(int(value)) if value == int(value) else f"{value:.6f}".rstrip("0").rstrip(".")


_DE_TOKEN = re.compile(r"\d+(?:\.\d{3})+(?!\d)|\d+(?:,\d+)?|[A-Za-zÄÖÜäöüß]+")


def source_numbers(text: str) -> set[str]:
    """德语原文里的数字（阿拉伯数字、1.000 千分位、2,1 小数、数词与「X Komma Y」）。"""
    vals: list[float | None] = []
    words: list[str] = []
    for tok in _DE_TOKEN.findall(text or ""):
        if tok[0].isdigit():
            if "." in tok:
                vals.append(float(tok.replace(".", "")))
            else:
                vals.append(float(tok.replace(",", ".")))
            words.append("")
        else:
            n = _de_word_number(tok)
            vals.append(None if n is None else float(n))
            words.append(tok.lower())
    out: set[str] = set()
    i = 0
    while i < len(vals):
        v = vals[i]
        if (v is not None and i + 2 < len(vals) and words[i + 1] == "komma"
                and vals[i + 2] is not None and vals[i + 2] == int(vals[i + 2])):
            out.add(_canon(float(f"{int(v)}.{int(vals[i + 2])}")))
            i += 3
            continue
        if v is not None:
            out.add(_canon(v))
        i += 1
    return out


_ZH_DIGIT = {"零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_ZH_NUM = re.compile(r"[零〇一二两三四五六七八九十百千万]+(?:点[零〇一二三四五六七八九]+(?!分))?")
_AR_NUM = re.compile(r"\d{1,3}(?:,\d{3})+(?!\d)|\d+(?:\.\d+)?")


def _zh_int(s: str) -> int | None:
    if not s:
        return None
    if all(c in _ZH_DIGIT for c in s) and len(s) > 1:  # 二〇二六 这种逐位写法
        return int("".join(str(_ZH_DIGIT[c]) for c in s))
    total, section, num = 0, 0, 0
    for c in s:
        if c in _ZH_DIGIT:
            num = _ZH_DIGIT[c]
        elif c == "十":
            section += (num or 1) * 10
            num = 0
        elif c == "百":
            section += num * 100
            num = 0
        elif c == "千":
            section += num * 1000
            num = 0
        elif c == "万":
            total += (section + num) * 10000
            section, num = 0, 0
    return total + section + num


def target_numbers(text: str) -> set[str]:
    """中文译文里的数字（阿拉伯数字含千分位/小数，中文数字含「X点Y」小数）。"""
    out: set[str] = set()
    t = text or ""
    for m in _AR_NUM.findall(t):
        out.add(_canon(float(m.replace(",", ""))))
    for m in _ZH_NUM.findall(t):
        whole, _, frac = m.partition("点")
        n = _zh_int(whole)
        if n is None or (n == 0 and not any(c in "零〇" for c in whole)):
            continue
        if frac:
            out.add(_canon(float(f"{n}." + "".join(str(_ZH_DIGIT[c]) for c in frac))))
        else:
            out.add(_canon(float(n)))
    return out


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
        self._last_used = float("-inf")

    def translate(self, text: str, src: str, dst: str) -> str | None:
        return self.translate_ctx(text, src, dst)

    def translate_ctx(self, text: str, src: str, dst: str, context: str = "") -> str | None:
        s, d = _LANG_NAMES.get(src, src), _LANG_NAMES.get(dst, dst)
        # 德语 "19.10 Uhr" 先规范成 19:10，与桌面 translator_queue 同口径
        text = _normalize_clock_times(text)
        context = _normalize_clock_times(context or "")
        ctx = f"上一句（仅供理解，不要翻译它）：\n{context}\n\n" if context else ""
        prompt = (
            f"你是{s}字幕翻译。请把下面这一条字幕翻译成自然、准确的{d}。\n"
            f"只翻译当前这一条，不要补充、解释、拒答，也不要输出{s}原文；"
            f"数字、人名、机构名和地名不得改写。\n\n"
            f"{ctx}{s}原文：\n{text}\n\n{d}译文："
        )
        out = self._generate(prompt)
        if out is None:
            return None
        # 数字一致性（2026-10-12 真机：qwen3.5:4b 把 "zwei Komma eins Prozent" 译成 1.2%）：
        # 原文的数字必须都出现在译文里，缺了就带着数字清单重试一次；仍不对就用第二次结果。
        want = source_numbers(text) if src == "de" else set()
        if want and not want <= target_numbers(out):
            hint = "、".join(sorted(want, key=float))
            retry = self._generate(prompt + f"\n（原文中的数字是：{hint}。译文必须准确保留这些数字。）\n{d}译文：")
            if retry is not None:
                if not want <= target_numbers(retry):
                    _log.warning("tx_number_mismatch want=%d", len(want))
                out = retry
        # 出口统一繁转简：目标语言是中文时小模型仍会偶发繁体（见 text_rules）
        return to_simplified_if_target(out, dst) or None

    def warm(self) -> None:
        # Ollama 约定：空 prompt 的 generate 只加载模型。keep_alive 内用过就不重复
        if time.monotonic() - self._last_used < WARM_SKIP_S:
            return
        self._post({"model": self._model, "prompt": "", "stream": False,
                    "keep_alive": NODE_OLLAMA_KEEP_ALIVE, "options": self._options()})
        self._last_used = time.monotonic()

    def _options(self) -> dict:
        # 与翻译/查词共用 num_ctx，否则 Ollama 会换 runner 重装模型（CLAUDE.md 第 4 节第 21 条）；
        # 预热也必须同一份，不然预热的 runner 用不上
        return {"temperature": 0.2, "num_predict": 512,
                "num_ctx": getattr(config, "OLLAMA_NUM_CTX", 4096)}

    def _post(self, body: dict) -> dict:
        resp = self._session.post(
            f"{self._tq.ollama_url()}/api/generate", json=body,
            timeout=max(30, int(getattr(config, "OLLAMA_TIMEOUT_COLD", 90))),
        )
        resp.raise_for_status()
        return resp.json()

    def _generate(self, prompt: str) -> str | None:
        try:
            data = self._post({
                "model": self._model,
                "prompt": prompt,
                "stream": False,
                "think": False,
                # 节点机往往也是用户自己的游戏/工作机：会话结束 2 分钟 worker 回收，
                # 翻译模型再多留 1 分钟就让出显存（2026-10-11 用户问「为什么 GPU 还被占着」）。
                "keep_alive": NODE_OLLAMA_KEEP_ALIVE,
                "options": self._options(),
            })
            out = _strip_translator_note(str(data.get("response", "")).strip())
        except Exception:
            return None
        self._last_used = time.monotonic()
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

