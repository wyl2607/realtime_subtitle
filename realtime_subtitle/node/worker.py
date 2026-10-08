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

import faulthandler
import json
import math
import os
import queue
import re
import signal
import sys
import threading
import time
from pathlib import Path

import numpy as np

from realtime_subtitle.node.engines import AsrEngine, Translator, select_translator
from realtime_subtitle.node.info import (
    ASR_STATE_FILENAME,
    DEFAULT_TRANSLATOR,
    STATE_DIR,
)
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
# hello 的语言代号白名单（S5）：它会原样进 Whisper `language=` 与 Ollama prompt，
# 所以只放 ISO 639 风格的 "de" / "zh" / "zh-Hans"，其余一律按 bad_hello 拒绝
# （用 fullmatch：`$` 会放过结尾的换行）
_LANG = re.compile(r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")

# 队列上限（RFC S4：入队有上限、丢最旧并告警）。
# asr 队列按「段」计：一段 ≤15s，8 段 ≈ 最多落后 2 分钟音频、约 8MB float32；
# 落后超过两分钟的字幕对直播已无意义，宁可丢旧段让新语音能及时出字。
# tx 队列按「句」计：一段通常 1–10 句，64 句 ≈ 6–8 段的译文量，只占几十 KB。
MAX_ASR_BACKLOG = 8
MAX_TX_BACKLOG = 64

# 会话被识别音频不足 5 秒就不更新 rtf：样本太少时，一次模型预热抖动或一段
# 极短的段就能把比值带偏几倍，而旧值是在更长的音频上攒出来的，不该被它冲掉。
RTF_MIN_AUDIO_S = 5.0
# 滑动合并 new = 0.7*old + 0.3*session：与 WhisperEngine 内部的段间平滑同一套
# 系数——单个会话的负载（别的程序占 GPU、音频内容）有偶然性，只给 30% 权重，
# 既能在几个会话内跟上硬件的真实变化，又不会被一次异常会话拉走。
RTF_OLD_WEIGHT = 0.7
RTF_NEW_WEIGHT = 0.3


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
                 translator_factory=None, err=None, state_dir=None):
        self._in = inp
        self._out = out
        self._err = err if err is not None else sys.stderr
        self._asr = asr
        self._vad = vad
        self._translator_factory = translator_factory or select_translator
        self._translator: Translator | None = None
        self._tr_pair: tuple[str, str] | None = None
        self._loaded = False
        # 测试注入点；None 表示用 info.STATE_DIR（运行时以它为准，读取时才解析）
        self._state_dir = Path(state_dir) if state_dir is not None else None
        # 本会话累计：被识别的音频秒数与识别耗时，受 _state_lock 保护
        self._sess_audio_s = 0.0
        self._sess_asr_s = 0.0

        self._emit_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._gen = 0
        self._next_id = 1
        self._src = ""
        self._dst = ""
        self._seg: Segmenter | None = None
        self._asr_q: queue.Queue = queue.Queue()
        self._tx_q: queue.Queue = queue.Queue()
        self._put_lock = threading.Lock()
        self._threads_started = False
        self._pipe_dead = False

    # ------------------------------------------------------------ 输出

    def _emit(self, event: dict) -> None:
        if self._pipe_dead:
            return
        # errors="replace"：ASR/翻译偶尔吐出孤立 surrogate，strict 会抛错丢掉整句
        data = (json.dumps(event, ensure_ascii=False) + "\n").encode("utf-8", errors="replace")
        with self._emit_lock:
            try:
                # 管道上的 write 可能只写出一部分（fd 是无缓冲 FileIO）：写满为止，
                # 否则事件帧被截断，gateway 会读到半行 JSON
                view = memoryview(data)
                while view:
                    n = self._out.write(view)
                    if n is None:
                        n = len(view)
                    if n <= 0:
                        raise OSError("zero_write")
                    view = view[n:]
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
                self._finish_session()
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
                or not isinstance(msg.get("dst"), str)
                or not _LANG.fullmatch(msg["src"]) or not _LANG.fullmatch(msg["dst"])):
            raise ProtocolError("bad_hello")
        src, dst = msg["src"], msg["dst"]

        # 上一会话在这里结束：先收口它的 rtf（hello 校验通过之后再收，
        # 协议错误的坏 hello 不算会话边界）
        self._finish_session()

        # 先递增代数再动翻译器/引擎：此后 tx/asr 线程里属于旧会话的结果在
        # 「查代数 + 发事件」那把锁里一律被拒，不会挂到新会话 id 上
        with self._state_lock:
            self._gen += 1

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
            self._tr_pair = None
            try:
                self._translator = self._translator_factory(src, dst)
                self._tr_pair = (src, dst)
            except Exception as e:  # noqa: BLE001
                # 构造失败不记语言对：同语言对的下一次 hello 要重试并再报告
                self.log("translator_unavailable", err=type(e).__name__)
                tr_note = ("translator_unavailable", "翻译不可用，只输出原文")

        with self._state_lock:
            self._gen += 1
            self._next_id = 1
            # 第一次递增之后旧会话的识别结果已被拒收，这里清零才不会混进新会话
            self._sess_audio_s = self._sess_asr_s = 0.0
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

    # ------------------------------------------------------------ rtf 持久化

    def _finish_session(self) -> None:
        """会话结束（下一条 hello / stdin EOF）：把本会话 rtf 滑动并入 asr_state.json。

        任何失败只记日志：这只是给 /v1/info 的路由提示，不能影响会话与退出码。
        """
        with self._state_lock:
            audio_s, asr_s = self._sess_audio_s, self._sess_asr_s
            self._sess_audio_s = self._sess_asr_s = 0.0
        if self._seg is None:
            return  # 还没有过会话
        if audio_s < RTF_MIN_AUDIO_S:
            self.log("rtf_skipped", reason="short_session", audio_s=audio_s)
            return
        try:
            self._update_rtf_file(asr_s / audio_s, audio_s)
        except Exception as e:  # noqa: BLE001 - 只记类名
            self.log("rtf_write_failed", err=type(e).__name__)

    def _update_rtf_file(self, session_rtf: float, audio_s: float) -> None:
        state_dir = self._state_dir if self._state_dir is not None else STATE_DIR
        # 目录由安装脚本 / gateway 建（权限也由它们定），worker 不越俎代庖
        if not state_dir.is_dir():
            self.log("rtf_state_dir_missing")
            return
        path = state_dir / ASR_STATE_FILENAME
        old: dict = {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                old = raw
        except (OSError, ValueError):
            pass
        old_rtf = old.get("rtf")
        valid = (isinstance(old_rtf, int | float) and not isinstance(old_rtf, bool)
                 and math.isfinite(old_rtf) and old_rtf >= 0)
        new_rtf = (RTF_OLD_WEIGHT * old_rtf + RTF_NEW_WEIGHT * session_rtf
                   if valid else session_rtf)

        engine = self._asr.info()
        state = {}
        for key in ("model", "backend"):
            val = old.get(key)
            state[key] = val if isinstance(val, str) and val else engine.get(key)
        state["rtf"] = round(new_rtf, 4)
        tr = old.get("translator")
        state["translator"] = (tr if isinstance(tr, str) and tr else
                               self._translator.name if self._translator else DEFAULT_TRANSLATOR)

        # 同目录临时文件 + replace：gateway 随时在读，不能让它读到写了一半的 JSON
        tmp = state_dir / f".{ASR_STATE_FILENAME}.{os.getpid()}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(state, f, allow_nan=False)
            os.chmod(tmp, 0o600)  # 临时文件若是上次崩溃遗留，open 不会改它的权限
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        self.log("rtf_updated", rtf=new_rtf, session_rtf=session_rtf, audio_s=audio_s)

    def _on_audio(self, data: bytes) -> None:
        pcm = np.frombuffer(data, dtype="<i2")
        self._enqueue(self._seg.feed(pcm))

    def _put_bounded(self, q: queue.Queue, item, cap: int, what: str) -> None:
        """入队；超过上限就丢最旧的（task_done 补账，join 才不会卡住）并告警。"""
        dropped = 0
        with self._put_lock:
            while q.qsize() >= cap:
                try:
                    q.get_nowait()
                except queue.Empty:
                    break
                q.task_done()
                dropped += 1
            q.put(item)
        if dropped:
            self.log("backlog_dropped", queue=what, dropped=dropped, cap=cap)
            self._status("backlog_dropped", "处理积压，已丢弃最旧内容")

    def _enqueue(self, segments: list[Segment]) -> None:
        for s in segments:
            self._put_bounded(self._asr_q, (self._gen, s), MAX_ASR_BACKLOG, "asr")

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
                with self._state_lock:
                    live = gen == self._gen
                    src = self._src
                if live:
                    self._recognize(gen, seg, src)
            except Exception as e:  # noqa: BLE001
                self.log("asr_error", err=type(e).__name__)
                self._status("asr_error", "识别失败，已跳过一段")
            finally:
                self._asr_q.task_done()

    def _recognize(self, gen: int, seg: Segment, src: str) -> None:
        t0 = time.perf_counter()
        utts = self._asr.transcribe(seg.audio, src)
        asr_s = time.perf_counter() - t0
        # 夹到不含垫的 [a0, a1]：垫只为多喂一点上下文，不是语音的一部分；
        # 夹到含垫边界会让强切相邻两段的 final 时间重叠（P5 按时间替换会吃掉邻行）
        lo, hi = seg.a0, seg.a1
        with self._state_lock:
            # 识别期间如果来了新 hello，这批结果属于已结束的会话，丢弃
            if gen != self._gen:
                return
            self._sess_audio_s += max(seg.a1 - seg.a0, 0.0)
            self._sess_asr_s += asr_s
            for u in utts:
                a0 = min(max(seg.audio_t0 + u.start, lo), hi)
                a1 = min(max(seg.audio_t0 + u.end, a0), hi)
                sid = self._next_id
                self._next_id += 1
                self._emit({"ev": "final", "id": sid, "a0": round(a0, 3),
                            "a1": round(a1, 3), "text": u.text})
                self._put_bounded(self._tx_q, (gen, sid, u.text), MAX_TX_BACKLOG, "tx")
        self.log("segment", seg_s=seg.a1 - seg.a0, asr_s=asr_s, sentences=len(utts))

    def _tx_loop(self) -> None:
        while True:
            gen, sid, text = self._tx_q.get()
            try:
                with self._state_lock:
                    if gen != self._gen or self._translator is None:
                        continue
                    tr, src, dst = self._translator, self._src, self._dst
                t0 = time.perf_counter()
                out = tr.translate(text, src, dst)
                # 「查代数 + 发事件」必须在同一把锁里：翻译期间 hello 可能已开新会话
                with self._state_lock:
                    if gen != self._gen:
                        continue
                    if out:
                        self._emit({"ev": "translation", "id": sid, "text": out})
                    else:
                        self._status("translate_failed", "一句翻译失败，已跳过")
                if out:
                    self.log("translated", id=sid, tx_s=time.perf_counter() - t0)
                else:
                    self.log("translate_failed", id=sid)
            except Exception as e:  # noqa: BLE001
                self.log("translate_error", id=sid, err=type(e).__name__)
                with self._state_lock:
                    if gen == self._gen:
                        self._status("translate_failed", "一句翻译失败，已跳过")
            finally:
                self._tx_q.task_done()


def main() -> None:
    # 先保住协议通道：fd 1 复制给事件流专用；fd 2 复制出来仅供 `_log` 使用；
    # 然后 fd 1 / fd 2 / sys.stdout / sys.stderr 全部指向 /dev/null——第三方库的
    # print 和子进程（如 Apple helper，继承 fd 2）的输出都落空，不可能把转录或
    # 译文正文带进日志（S7）。dup 出来的 fd 默认不可继承，子进程拿不到这两个。
    out = os.fdopen(os.dup(1), "wb", buffering=0)
    err = os.fdopen(os.dup(2), "w", buffering=1, encoding="utf-8", errors="replace")
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2)
    os.close(devnull)
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")  # noqa: SIM115

    def _term(_signum, _frame):
        try:
            err.flush()
        except Exception:  # noqa: BLE001
            pass
        os._exit(EXIT_OK)

    signal.signal(signal.SIGTERM, _term)

    # fd 2 已指向 /dev/null，未捕获异常 / 线程异常 / 原生崩溃的默认输出都会消失。
    # 这里只经 `_log` 写异常类名与线程代号，绝不写 str(e) / traceback 文本（S7：
    # 异常消息可能含正文）；faulthandler 只输出帧位置（文件/行/函数名）。
    def _excepthook(exc_type, _exc, _tb):
        _log(err, "uncaught_exception", err=exc_type.__name__)

    def _thread_excepthook(args):
        _log(err, "thread_exception", err=args.exc_type.__name__,
             thread=getattr(args.thread, "name", None) or "")
        # 工作线程被 BaseException（如库内 sys.exit）杀死后进程仍活着，
        # _asr_q.join() 会永远卡住；立即以内部错误码退出，让 gateway 按退出码判崩溃。
        if getattr(args.thread, "name", None) in ("node-asr", "node-tx"):
            try:
                err.flush()
            except Exception:  # noqa: BLE001
                pass
            os._exit(EXIT_INTERNAL)

    sys.excepthook = _excepthook
    threading.excepthook = _thread_excepthook
    faulthandler.enable(file=err)

    try:
        from realtime_subtitle.node.engines import WhisperEngine
        from realtime_subtitle.node.segmenter import SileroVad

        asr, vad = WhisperEngine(), SileroVad()
    except Exception as e:  # noqa: BLE001
        _log(err, "engine_load_failed", err=type(e).__name__)
        sys.exit(EXIT_ENGINE)

    worker = Worker(sys.stdin.buffer, out, asr, vad, err=err)
    sys.exit(worker.run())


if __name__ == "__main__":
    main()
