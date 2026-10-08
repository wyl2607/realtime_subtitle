"""节点识别子进程：stdin 收音频与控制消息，stdout 发事件（RFC P3）。

    python -m realtime_subtitle.node.worker

线路格式（仅本机进程间，gateway <-> worker）：

  上行（stdin）两种消息交错：
    * 控制消息：一行一个 JSON（`hello` / `flush` / `drain`），以 `{` 开头；
    * 音频帧：4 字节大端长度 + s16le PCM，长度 ≤ 64KB（P2 的单帧上限）。
  两种消息靠首字节区分：合法长度 ≤ 0x00010000，大端首字节恒为 0x00，
  而 JSON 首字节是 0x7B，不会混淆。其他首字节一律按协议错误处理。
  下行（stdout）：一行一个 JSON 事件，格式同 P2（ready/final/translation/status/drained）。

退出码（P3 要求 gateway 能据此记录错误码）：
  0 正常（stdin 关闭 / 收到 SIGTERM）  2 协议错误  3 引擎加载失败  4 内部错误

会话：一个 worker 进程可以按顺序服务多个会话（RFC：会话结束后 worker 保温
120s）。每条 `hello` 开启新会话：句子 id 与 a0/a1 时间轴从零重新计，上一会话
遗留的待处理段直接丢弃（gateway 应该先 drain 再 hello）。

线程：主线程读 stdin 并跑 VAD 分段（几毫秒/帧）；`asr` 线程逐段识别并立即发
`final`；`tx` 线程翻译并发 `translation`。翻译独立成线程是为了让下一段的识别
不必等 Apple/Ollama，final 的延迟因此只取决于识别。

日志（S7）：只写 stderr，只记事件类型、id、时长、字节数、错误类名；
任何转录/译文正文都不进日志，`_log` 还会把形状不像「代号」的字符串值抹掉，
作为防手滑的第二道闸。stdout 是协议通道，所以 `main()` 一开始就把 fd 1 指向
stderr，避免依赖库的 print 污染事件流。
"""
from __future__ import annotations

import json
import os
import queue
import re
import signal
import sys
import threading
import time

import numpy as np

from realtime_subtitle.node.engines import AsrEngine, Translator, select_translator
from realtime_subtitle.node.segmenter import Segmenter, Segment, VadFn

EXIT_OK = 0
EXIT_PROTOCOL = 2
EXIT_ENGINE = 3
EXIT_INTERNAL = 4

MAX_FRAME = 64 * 1024
SAMPLE_RATE = 16000
# hello/flush/drain 都是几十字节；设上限防止恶意长行把内存吃光（S4 同理）
MAX_LINE = 4096

_CODE = re.compile(r"^[A-Za-z0-9_.:-]{1,48}$")


class ProtocolError(Exception):
    """协议违规。消息是固定短语，不夹带输入内容。"""


def _log(stream, event: str, **fields) -> None:
    parts = []
    for k, v in fields.items():
        if isinstance(v, bool | int):
            parts.append(f"{k}={v}")
        elif isinstance(v, float):
            parts.append(f"{k}={v:.3f}")
        elif isinstance(v, str) and _CODE.match(v):
            parts.append(f"{k}={v}")
        else:
            parts.append(f"{k}=?")
    print(f"[node.worker] {event} " + " ".join(parts), file=stream, flush=True)


class Worker:
    def __init__(self, inp, out, asr: AsrEngine, vad: VadFn,
                 translator_factory=None, err=None):
        self._in = inp
        self._out = out
        self._err = err if err is not None else sys.stderr
        self._asr = asr
        self._vad = vad
        self._translator_factory = translator_factory or select_translator
        self._translator: Translator | None = None
        self._tr_pair: tuple[str, str] | None = None
        self._loaded = False

        self._emit_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._gen = 0
        self._next_id = 1
        self._src = ""
        self._dst = ""
        self._seg: Segmenter | None = None
        self._asr_q: queue.Queue = queue.Queue()
        self._tx_q: queue.Queue = queue.Queue()
        self._threads_started = False
        self._pipe_dead = False

    # ------------------------------------------------------------ 输出

    def _emit(self, event: dict) -> None:
        if self._pipe_dead:
            return
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8")
        with self._emit_lock:
            try:
                self._out.write(data)
                self._out.flush()
            except (BrokenPipeError, ValueError, OSError):
                # gateway 已经走了：不再写，主循环会在读到 EOF 时正常退出
                self._pipe_dead = True

    def _status(self, code: str, text: str) -> None:
        self._emit({"ev": "status", "code": code, "text": text})

    def log(self, event: str, **fields) -> None:
        _log(self._err, event, **fields)

    # ------------------------------------------------------------ 输入

    def _read_exact(self, n: int) -> bytes:
        buf = self._in.read(n)
        if buf is None or len(buf) != n:
            raise ProtocolError("truncated_message")
        return buf

    def _read_message(self):
        """返回 ("ctl", dict) / ("pcm", bytes) / None（EOF）。"""
        first = self._in.read(1)
        if not first:
            return None
        if first == b"{":
            line = first + self._in.readline(MAX_LINE)
            if not line.endswith(b"\n"):
                raise ProtocolError("control_line_too_long_or_truncated")
            try:
                msg = json.loads(line)
            except ValueError:
                raise ProtocolError("control_not_json") from None
            if not isinstance(msg, dict):
                raise ProtocolError("control_not_object")
            return "ctl", msg
        if first == b"\x00":
            n = int.from_bytes(first + self._read_exact(3), "big")
            if n > MAX_FRAME:
                raise ProtocolError("frame_too_large")
            if n % 2:
                raise ProtocolError("frame_not_s16le")
            return "pcm", self._read_exact(n) if n else b""
        raise ProtocolError("bad_message_prefix")

    # ------------------------------------------------------------ 主循环

    def run(self) -> int:
        try:
            return self._loop()
        except ProtocolError as e:
            self.log("protocol_error", code=str(e))
            self._status("protocol_error", "协议错误")
            return EXIT_PROTOCOL
        except Exception as e:  # noqa: BLE001 - 兜底：只记类名，消息可能夹带原文
            self.log("internal_error", err=type(e).__name__)
            self._status("internal_error", "内部错误")
            return EXIT_INTERNAL
        finally:
            if self._translator is not None:
                self._translator.close()

    def _loop(self) -> int:
        while True:
            msg = self._read_message()
            if msg is None:
                self.log("stdin_closed")
                return EXIT_OK
            kind, body = msg
            if kind == "pcm":
                if self._seg is None:
                    raise ProtocolError("audio_before_hello")
                self._on_audio(body)
                continue
            t = body.get("type")
            if t == "hello":
                code = self._on_hello(body)
                if code is not None:
                    return code
            elif t in ("flush", "drain"):
                if self._seg is None:
                    raise ProtocolError("control_before_hello")
                self._enqueue(self._seg.flush())
                if t == "drain":
                    self._drain()
            else:
                raise ProtocolError("unknown_control")

    # ------------------------------------------------------------ 会话

    def _on_hello(self, msg: dict) -> int | None:
        if (msg.get("v") != 2 or msg.get("sample_rate") != SAMPLE_RATE
                or msg.get("format") != "s16le"
                or not isinstance(msg.get("src"), str)
                or not isinstance(msg.get("dst"), str)):
            raise ProtocolError("bad_hello")
        src, dst = msg["src"], msg["dst"]

        cold = not self._loaded
        load_s = 0.0
        if cold:
            t0 = time.perf_counter()
            try:
                self._asr.load()
            except Exception as e:  # noqa: BLE001
                self.log("engine_load_failed", err=type(e).__name__)
                self._status("engine_load_failed", "识别引擎加载失败")
                return EXIT_ENGINE
            self._loaded = True
            load_s = time.perf_counter() - t0

        tr_note = None
        if self._tr_pair != (src, dst):
            if self._translator is not None:
                self._translator.close()
                self._translator = None
            try:
                self._translator = self._translator_factory(src, dst)
            except Exception as e:  # noqa: BLE001
                self.log("translator_unavailable", err=type(e).__name__)
                tr_note = ("translator_unavailable", "翻译不可用，只输出原文")
            self._tr_pair = (src, dst)

        with self._state_lock:
            self._gen += 1
            self._next_id = 1
            self._src, self._dst = src, dst
            if hasattr(self._vad, "reset"):
                self._vad.reset()
            self._seg = Segmenter(self._vad)
        self._start_threads()

        engine = dict(self._asr.info())
        engine["translator"] = self._translator.name if self._translator else "none"
        self._emit({"ev": "ready", "cold": cold, "load_s": round(load_s, 3),
                    "engine": engine})
        self.log("ready", cold=cold, load_s=load_s)
        if tr_note:
            self._status(*tr_note)
        return None

    def _on_audio(self, data: bytes) -> None:
        pcm = np.frombuffer(data, dtype="<i2")
        self._enqueue(self._seg.feed(pcm))

    def _enqueue(self, segments: list[Segment]) -> None:
        for s in segments:
            self._asr_q.put((self._gen, s))

    def _drain(self) -> None:
        # asr 线程先放空（它会往 tx 队列里放东西），再等 tx 放空，顺序不能反
        self._asr_q.join()
        self._tx_q.join()
        self._emit({"ev": "drained"})
        self.log("drained")

    # ------------------------------------------------------------ 工作线程

    def _start_threads(self) -> None:
        if self._threads_started:
            return
        self._threads_started = True
        threading.Thread(target=self._asr_loop, daemon=True, name="node-asr").start()
        threading.Thread(target=self._tx_loop, daemon=True, name="node-tx").start()

    def _asr_loop(self) -> None:
        while True:
            gen, seg = self._asr_q.get()
            try:
                if gen == self._gen:
                    self._recognize(gen, seg)
            except Exception as e:  # noqa: BLE001
                self.log("asr_error", err=type(e).__name__)
                self._status("asr_error", "识别失败，已跳过一段")
            finally:
                self._asr_q.task_done()

    def _recognize(self, gen: int, seg: Segment) -> None:
        t0 = time.perf_counter()
        utts = self._asr.transcribe(seg.audio, self._src)
        asr_s = time.perf_counter() - t0
        lo = seg.audio_t0
        hi = seg.audio_t0 + len(seg.audio) / SAMPLE_RATE
        with self._state_lock:
            # 识别期间如果来了新 hello，这批结果属于已结束的会话，丢弃
            if gen != self._gen:
                return
            for u in utts:
                a0 = min(max(seg.audio_t0 + u.start, lo), hi)
                a1 = min(max(seg.audio_t0 + u.end, a0), hi)
                sid = self._next_id
                self._next_id += 1
                self._emit({"ev": "final", "id": sid, "a0": round(a0, 3),
                            "a1": round(a1, 3), "text": u.text})
                self._tx_q.put((gen, sid, u.text))
        self.log("segment", seg_s=seg.a1 - seg.a0, asr_s=asr_s, sentences=len(utts))

    def _tx_loop(self) -> None:
        while True:
            gen, sid, text = self._tx_q.get()
            try:
                if gen == self._gen and self._translator is not None:
                    t0 = time.perf_counter()
                    out = self._translator.translate(text, self._src, self._dst)
                    if gen != self._gen:
                        continue
                    if out:
                        self._emit({"ev": "translation", "id": sid, "text": out})
                        self.log("translated", id=sid, tx_s=time.perf_counter() - t0)
                    else:
                        self._status("translate_failed", "一句翻译失败，已跳过")
                        self.log("translate_failed", id=sid)
            except Exception as e:  # noqa: BLE001
                self.log("translate_error", id=sid, err=type(e).__name__)
                self._status("translate_failed", "一句翻译失败，已跳过")
            finally:
                self._tx_q.task_done()


def main() -> None:
    # 先保住协议通道：fd 1 复制给事件流专用，再把 fd 1 / sys.stdout 都指向 stderr，
    # 之后任何库（含子进程继承 fd 1）的 print 都只会落到日志里
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr

    def _term(_signum, _frame):
        sys.stderr.flush()
        os._exit(EXIT_OK)

    signal.signal(signal.SIGTERM, _term)

    from realtime_subtitle.node.engines import WhisperEngine
    from realtime_subtitle.node.segmenter import SileroVad

    worker = Worker(sys.stdin.buffer, out, WhisperEngine(), SileroVad())
    sys.exit(worker.run())


if __name__ == "__main__":
    main()
