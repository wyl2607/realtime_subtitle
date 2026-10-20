"""节点识别子进程：stdin 收音频与控制消息，stdout 发事件（RFC P3）。

    python -m realtime_subtitle.node.worker

线路格式（仅本机进程间，gateway <-> worker）：

  上行（stdin）两种消息交错：
    * 控制消息：一行一个 JSON（`hello` / `flush` / `drain` / `end`），以 `{` 开头。
      多会话时带整数 `sid`；没有 `sid` 的 hello/flush/drain 走旧的单会话路径（sid 0）。
    * 音频帧两种：
        - 旧：4 字节大端长度 + s16le PCM（首字节 0x00），长度 ≤ 64KB，进当前 sid 0；
        - 新：0x01 + 4 字节大端 sid + 4 字节大端长度 + PCM。
  首字节 0x00 / 0x01 / `{`（0x7B）以外一律协议错误。未知 sid 的音频或控制只丢弃
  该帧（不退出）：一个坏 sid 不能把别的会话一起打死。`end` 在 hello 之前出现也忽略。
  下行（stdout）：一行一个 JSON 事件，格式同 P2。只有 hello 带了显式 sid 时，
  事件才多一个 `sid`（gateway 转发给客户端之前会剥掉）。

退出码（P3 要求 gateway 能据此记录错误码）：
  0 正常（stdin 关闭 / 收到 SIGTERM）  2 协议错误  3 引擎加载失败  4 内部错误

会话：一个 worker 进程同时服务多个会话。每个 sid 有自己的分段器、VAD、语言对、
翻译器和句子 id。识别与翻译仍各只有一个线程（MLX 必须留在 asr 这一条线程上），
队列按会话轮转取活，避免一个会话的积压饿死另一个。`end` 或同 sid 的新 hello
只清该 sid；没有 sid 的新 hello 仍按旧行为收口并替换 sid 0。

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
import re
import signal
import sys
import threading
import time
from collections import deque
from pathlib import Path

import numpy as np

from realtime_subtitle.node.engines import AsrEngine, Translator, select_translator
from realtime_subtitle.node.info import (
    ASR_STATE_FILENAME,
    DEFAULT_TRANSLATOR,
    STATE_DIR,
)
from realtime_subtitle.node.segmenter import Segment, Segmenter

# 节点段长上限比 Segmenter 默认（15s）短。2026-10-11 网页视频实测：连续人声/背景乐里 Silero 找不到
# 0.6s 静音，几乎每段都顶到 13.5–14.6s 才切，精修结果 15s 一批整屏替换，用户觉得「卡」。
# 8s 加段尾 3s 找能量低点，实际 5–8s。云端分段不改：Groq 每次请求按至少 10s 计费（CLAUDE.md 第 49 条）。
NODE_MAX_SEGMENT_S = 8.0

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


class _Sess:
    """一个 sid 的分段 / 语言 / 草稿 / 翻译器。识别队列里的项只认 gen，不认对象身份。"""

    __slots__ = (
        "sid", "explicit", "gen", "src", "dst", "seg",
        "translator", "tr_pair", "next_id", "audio_s", "asr_s", "prev_src",
    )

    def __init__(self, sid: int, explicit: bool, gen: int, src: str, dst: str, seg: Segmenter) -> None:
        self.sid = sid
        self.explicit = explicit
        self.gen = gen
        self.src = src
        self.dst = dst
        self.seg = seg
        self.translator: Translator | None = None
        self.tr_pair: tuple[str, str] | None = None
        self.next_id = 1
        self.audio_s = 0.0
        self.asr_s = 0.0
        # 同会话上一句原文，给翻译当上下文（只在本会话内，不跨 sid）
        self.prev_src = ""


class FairQueue:
    """按 sid 分桶、轮转取活的有界队列。

    `qsize` 只数还排着队的项（不含正在处理的）；`unfinished_tasks` 含在飞的。
    丢弃只丢该 sid 队列里最旧的，并立刻把 unfinished 减掉，`join` 才不会卡住。
    一个 sid 的 `join_sid` 不等待别的 sid。
    """

    def __init__(self) -> None:
        self._cv = threading.Condition()
        self._qs: dict[int, deque] = {}
        self._order: deque[int] = deque()
        self._pending: dict[int, int] = {}
        self._inflight: dict[int, int] = {}
        self.unfinished_tasks = 0

    def qsize(self) -> int:
        with self._cv:
            return sum(len(q) for q in self._qs.values())

    def put_bounded(self, sid: int, item, cap: int) -> int:
        dropped = 0
        with self._cv:
            q = self._qs.setdefault(sid, deque())
            while len(q) >= cap:
                q.popleft()
                dropped += 1
                self.unfinished_tasks -= 1
                self._pending[sid] = self._pending.get(sid, 0) - 1
                if not q:
                    self._discard_order(sid)
            empty = not q
            q.append(item)
            if empty:
                self._order.append(sid)
            self.unfinished_tasks += 1
            self._pending[sid] = self._pending.get(sid, 0) + 1
            self._cv.notify()
        return dropped

    def get(self):
        with self._cv:
            while True:
                while not self._order:
                    self._cv.wait()
                sid = self._order.popleft()
                q = self._qs.get(sid)
                if not q:
                    continue
                item = q.popleft()
                if q:
                    self._order.append(sid)
                self._inflight[sid] = self._inflight.get(sid, 0) + 1
                return sid, item

    def task_done(self, sid: int | None = None) -> None:
        with self._cv:
            self.unfinished_tasks -= 1
            if sid is not None:
                self._pending[sid] = self._pending.get(sid, 0) - 1
                left = self._inflight.get(sid, 0) - 1
                if left <= 0:
                    self._inflight.pop(sid, None)
                else:
                    self._inflight[sid] = left
            self._cv.notify_all()

    def drop_session(self, sid: int) -> int:
        """丢掉还排着队的项（在飞的留给 task_done）。返回丢掉的个数。"""
        with self._cv:
            q = self._qs.pop(sid, None)
            self._discard_order(sid)
            n = len(q) if q else 0
            self.unfinished_tasks -= n
            self._pending[sid] = self._inflight.get(sid, 0)
            self._cv.notify_all()
            return n

    def join(self) -> None:
        with self._cv:
            while self.unfinished_tasks > 0:
                self._cv.wait()

    def join_sid(self, sid: int) -> None:
        with self._cv:
            while self._pending.get(sid, 0) > 0:
                self._cv.wait()

    def _discard_order(self, sid: int) -> None:
        try:
            self._order.remove(sid)
        except ValueError:
            pass


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
    def __init__(self, inp, out, asr: AsrEngine, vad=None,
                 translator_factory=None, err=None, state_dir=None, vad_factory=None):
        self._in = inp
        self._out = out
        self._err = err if err is not None else sys.stderr
        self._asr = asr
        if vad_factory is not None:
            self._vad_factory = vad_factory
        elif vad is not None:
            # 旧测试传入同一个无状态 VAD：单会话复用即可。多会话要各拿一份有状态的。
            self._vad_factory = lambda: vad
        else:
            raise TypeError("vad is required")
        self._translator_factory = translator_factory or select_translator
        self._loaded = False
        # 测试注入点；None 表示用 info.STATE_DIR（运行时以它为准，读取时才解析）
        self._state_dir = Path(state_dir) if state_dir is not None else None

        self._emit_lock = threading.Lock()
        self._state_lock = threading.Lock()
        self._sessions: dict[int, _Sess] = {}
        self._by_gen: dict[int, int] = {}
        self._next_gen = 0
        self._asr_q = FairQueue()
        self._tx_q = FairQueue()
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

    def _status(self, code: str, text: str, sess: _Sess | None = None, **fields) -> None:
        event = {"ev": "status", "code": code, "text": text}
        event.update(fields)
        if sess is not None and sess.explicit:
            event["sid"] = sess.sid
        self._emit(event)

    def log(self, event: str, **fields) -> None:
        _log(self._err, event, **fields)

    # ------------------------------------------------------------ 输入

    def _read_exact(self, n: int) -> bytes:
        buf = self._in.read(n)
        if buf is None or len(buf) != n:
            raise ProtocolError("truncated_message")
        return buf

    def _read_message(self):
        """返回 ("ctl", dict, None) / ("pcm", bytes, sid|None) / None（EOF）。

        没有 sid 的旧音频帧用 None，调用方把它送到 sid 0。
        """
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
            return "ctl", msg, None
        if first == b"\x01":
            sid = int.from_bytes(self._read_exact(4), "big")
            n = int.from_bytes(self._read_exact(4), "big")
            if n > MAX_FRAME:
                raise ProtocolError("frame_too_large")
            if n % 2:
                raise ProtocolError("frame_not_s16le")
            return "pcm", self._read_exact(n) if n else b"", sid
        if first == b"\x00":
            n = int.from_bytes(first + self._read_exact(3), "big")
            if n > MAX_FRAME:
                raise ProtocolError("frame_too_large")
            if n % 2:
                raise ProtocolError("frame_not_s16le")
            return "pcm", self._read_exact(n) if n else b"", None
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
            for sess in list(self._sessions.values()):
                if sess.translator is not None:
                    sess.translator.close()
                    sess.translator = None

    def _loop(self) -> int:
        while True:
            msg = self._read_message()
            if msg is None:
                self.log("stdin_closed")
                self._finish_session()
                return EXIT_OK
            kind, body, frame_sid = msg
            if kind == "pcm":
                self._on_audio(body, frame_sid)
                continue
            t = body.get("type")
            if t == "hello":
                code = self._on_hello(body)
                if code is not None:
                    return code
            elif t == "end":
                self._on_end(body)
            elif t in ("flush", "drain"):
                sess = self._control_session(body)
                if sess is None:
                    if not self._sessions:
                        raise ProtocolError("control_before_hello")
                    self.log("ignored_unknown_sid")
                    continue
                self._enqueue(sess, sess.seg.flush())
                if t == "drain":
                    self._drain(sess)
            else:
                raise ProtocolError("unknown_control")

    # ------------------------------------------------------------ 会话

    def _hello_sid(self, msg: dict) -> tuple[int, bool]:
        if "sid" not in msg:
            return 0, False
        sid = msg["sid"]
        if isinstance(sid, bool) or not isinstance(sid, int) or sid < 0 or sid > 0xFFFFFFFF:
            raise ProtocolError("bad_hello")
        return sid, True

    def _on_hello(self, msg: dict) -> int | None:
        if (msg.get("v") != 2 or msg.get("sample_rate") != SAMPLE_RATE
                or msg.get("format") != "s16le"
                or not isinstance(msg.get("src"), str)
                or not isinstance(msg.get("dst"), str)
                or not _LANG.fullmatch(msg["src"]) or not _LANG.fullmatch(msg["dst"])):
            raise ProtocolError("bad_hello")
        src, dst = msg["src"], msg["dst"]
        sid, explicit = self._hello_sid(msg)

        # 同 sid 的上一段在这里结束。先收口 rtf（用旧翻译器的名字），再决定
        # 留不留翻译器。坏 hello 在上面已经拒绝，不算会话边界。
        # 显式 sid 只动这一个会话；没有 sid 的旧 hello 也只动 sid 0。
        prev = self._sessions.get(sid)
        carried: Translator | None = None
        if prev is not None:
            if prev.translator is not None and prev.tr_pair == (src, dst):
                carried = prev.translator
            self._retire_session(sid, close_translator=carried is None)

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

        sess = self._open_session(sid, explicit, src, dst)
        tr_note = self._bind_translator(sess, src, dst, carried)
        self._warm_translator(sess.translator)
        self._start_threads()

        engine = dict(self._asr.info())
        engine["translator"] = sess.translator.name if sess.translator else "none"
        ready = {"ev": "ready", "cold": cold, "load_s": round(load_s, 3), "engine": engine}
        if explicit:
            ready["sid"] = sid
        self._emit(ready)
        self.log("ready", cold=cold, load_s=load_s)
        if tr_note:
            self._status(*tr_note, sess=sess)
        return None

    def _bind_translator(self, sess: _Sess, src: str, dst: str, carried: Translator | None):
        if carried is not None:
            sess.translator = carried
            sess.tr_pair = (src, dst)
            return None
        try:
            sess.translator = self._translator_factory(src, dst)
            sess.tr_pair = (src, dst)
        except Exception as e:  # noqa: BLE001
            # 构造失败不记语言对：同语言对的下一次 hello 要重试并再报告
            sess.translator = None
            sess.tr_pair = None
            self.log("translator_unavailable", err=type(e).__name__)
            return ("translator_unavailable", "翻译不可用，只输出原文")
        return None

    def _warm_translator(self, translator: Translator | None) -> None:
        """后台预热翻译模型：节点空闲 3 分钟后模型已卸载，第一句要等 9–15 秒（2026-10-12 实测）。
        不阻塞 hello→ready；失败只记日志，正式翻译时照常再加载。"""
        warm = getattr(translator, "warm", None)
        if warm is None:
            return

        def _run() -> None:
            t0 = time.perf_counter()
            try:
                warm()
            except Exception as e:  # noqa: BLE001
                self.log("translator_warm_failed", err=type(e).__name__)
                return
            self.log("translator_warm", warm_s=time.perf_counter() - t0)

        threading.Thread(target=_run, name="tx-warm", daemon=True).start()

    def _open_session(self, sid: int, explicit: bool, src: str, dst: str) -> _Sess:
        vad = self._vad_factory()
        if hasattr(vad, "reset"):
            vad.reset()
        with self._state_lock:
            self._next_gen += 1
            gen = self._next_gen
            sess = _Sess(sid, explicit, gen, src, dst, Segmenter(vad, max_s=NODE_MAX_SEGMENT_S))
            self._sessions[sid] = sess
            self._by_gen[gen] = sid
        return sess

    def _control_session(self, msg: dict) -> _Sess | None:
        if "sid" not in msg:
            return self._sessions.get(0)
        sid = msg["sid"]
        if isinstance(sid, bool) or not isinstance(sid, int):
            return None
        return self._sessions.get(sid)

    def _on_end(self, msg: dict) -> None:
        # end 在任何 hello 之前、或 sid 已经不在：忽略。协议错误会杀掉其它会话。
        if "sid" not in msg:
            sid: object = 0
        else:
            sid = msg["sid"]
        if isinstance(sid, bool) or not isinstance(sid, int) or sid not in self._sessions:
            return
        self._retire_session(sid, close_translator=True)

    # ------------------------------------------------------------ rtf 持久化

    def _finish_session(self, lock_timeout: float | None = None) -> None:
        """stdin EOF / SIGTERM：把还活着的每个会话的 rtf 滑动并入 asr_state.json。

        单个 sid 的结束走 `_retire_session`（hello 替换、end）。这里不拆会话对象：
        SIGTERM 之后进程马上退出，来不及再被别的路径重复写。

        lock_timeout 仅供 SIGTERM：handler 跑在主线程，可能打断持锁中的主线程，
        普通 `with` 会死锁。拿不到锁就整批放弃，不写一半。没有会话时不记
        rtf_skipped（和「还没 hello 就 EOF」的旧行为一致）。
        """
        if lock_timeout is not None and not self._state_lock.acquire(timeout=lock_timeout):
            self.log("rtf_skipped", reason="lock_busy_on_term")
            return
        if lock_timeout is None:
            self._state_lock.acquire()
        try:
            snap = []
            for sess in self._sessions.values():
                snap.append((sess.audio_s, sess.asr_s, sess.translator))
                sess.audio_s = 0.0
                sess.asr_s = 0.0
        finally:
            self._state_lock.release()
        for audio_s, asr_s, translator in snap:
            self._commit_rtf(audio_s, asr_s, translator)

    def _retire_session(self, sid: int, *, close_translator: bool) -> None:
        """收口一个 sid：写 rtf、从映射里摘掉、丢掉还排着队的活。在飞的识别靠 gen 对不上丢弃。"""
        self._state_lock.acquire()
        try:
            sess = self._sessions.get(sid)
            if sess is None:
                return
            audio_s, asr_s = sess.audio_s, sess.asr_s
            sess.audio_s = 0.0
            sess.asr_s = 0.0
            translator = sess.translator
            self._sessions.pop(sid, None)
            self._by_gen.pop(sess.gen, None)
        finally:
            self._state_lock.release()
        self._asr_q.drop_session(sid)
        self._tx_q.drop_session(sid)
        self._commit_rtf(audio_s, asr_s, translator)
        if close_translator and translator is not None:
            translator.close()
            sess.translator = None

    def _commit_rtf(self, audio_s: float, asr_s: float, translator: Translator | None) -> None:
        if audio_s < RTF_MIN_AUDIO_S:
            self.log("rtf_skipped", reason="short_session", audio_s=audio_s)
            return
        try:
            self._update_rtf_file(asr_s / audio_s, audio_s, translator)
        except Exception as e:  # noqa: BLE001 - 只记类名
            self.log("rtf_write_failed", err=type(e).__name__)

    def _update_rtf_file(self, session_rtf: float, audio_s: float, translator: Translator | None = None) -> None:
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
        # translator 以本会话真实在用的为准，不再沿用旧值：安装自检时语言包未装会写
        # ollama:<model>，之后装好语言包 worker 实际走 apple，沿用旧值会让 /v1/info 永久报错。
        # 本会话翻译器不可用（None）时才退回旧的合法值，再没有就用默认（不写 "none"，不在 P1 枚举内）。
        # 注意：沿用的旧值可能来自别的语言对（P1 是节点级单值，契约冻结，不按语言对区分），
        # 路由可能因此高估该节点的翻译能力。
        tr = old.get("translator")
        if translator is not None:
            state["translator"] = translator.name
        else:
            state["translator"] = tr if isinstance(tr, str) and tr else DEFAULT_TRANSLATOR

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

    def _on_audio(self, data: bytes, frame_sid: int | None = None) -> None:
        if frame_sid is None:
            if not self._sessions:
                raise ProtocolError("audio_before_hello")
            sess = self._sessions.get(0)
        else:
            if not self._sessions:
                raise ProtocolError("audio_before_hello")
            sess = self._sessions.get(frame_sid)
        if sess is None:
            self.log("ignored_unknown_sid", sid=0 if frame_sid is None else frame_sid)
            return
        pcm = np.frombuffer(data, dtype="<i2")
        self._enqueue(sess, sess.seg.feed(pcm))

    def _put_bounded(self, q: FairQueue, sid: int, item, cap: int, what: str, sess: _Sess) -> None:
        """入队；该 sid 超过上限就丢它最旧的（unfinished 立刻减，join 才不会卡住）并告警。"""
        dropped = q.put_bounded(sid, item, cap)
        if dropped:
            self.log("backlog_dropped", queue=what, dropped=dropped, cap=cap)
            self._status("backlog_dropped", "处理积压，已丢弃最旧内容", sess=sess)

    def _enqueue(self, sess: _Sess, segments: list[Segment]) -> None:
        gen, sid = sess.gen, sess.sid
        # 只给诊断用：段尾静音等待按样本轴算（客户端实时送音频，1s 样本≈1s 墙钟），
        # 排队时长用 perf_counter。segmenter 本身刻意不读墙钟，所以补在这里。
        received_at = time.perf_counter()
        received_seconds = sess.seg.samples_received / SAMPLE_RATE
        for s in segments:
            s.close_wait_s = max(received_seconds - s.a1, 0.0)
            s.enqueued_at = received_at
            self._put_bounded(self._asr_q, sid, (gen, s), MAX_ASR_BACKLOG, "asr", sess)

    def _drain(self, sess: _Sess) -> None:
        # 只等这一个 sid：asr 先放空（它会往 tx 队列里放东西），再等 tx。别的会话不用等。
        sid, gen = sess.sid, sess.gen
        self._asr_q.join_sid(sid)
        self._tx_q.join_sid(sid)
        with self._state_lock:
            cur = self._sessions.get(sid)
            if cur is None or cur.gen != gen:
                return
            explicit = cur.explicit
        event = {"ev": "drained"}
        if explicit:
            event["sid"] = sid
        self._emit(event)
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
            sid, (gen, seg) = self._asr_q.get()
            try:
                with self._state_lock:
                    sess = self._sessions.get(sid)
                    live = sess is not None and sess.gen == gen
                    src = sess.src if live else ""
                if live:
                    self._recognize(gen, seg, src)
            except Exception as e:  # noqa: BLE001
                self.log("asr_error", err=type(e).__name__)
                with self._state_lock:
                    sess = self._sessions.get(sid)
                    current = sess if sess is not None and sess.gen == gen else None
                self._status("asr_error", "识别失败，已跳过一段", sess=current)
            finally:
                self._asr_q.task_done(sid)

    def _recognize(self, gen: int, seg: Segment, src: str) -> None:
        t0 = time.perf_counter()
        queue_s = 0.0 if seg.enqueued_at is None else max(t0 - seg.enqueued_at, 0.0)
        utts = self._asr.transcribe(seg.audio, src)
        asr_s = time.perf_counter() - t0
        # 夹到不含垫的 [a0, a1]：垫只为多喂一点上下文，不是语音的一部分；
        # 夹到含垫边界会让强切相邻两段的 final 时间重叠（P5 按时间替换会吃掉邻行）
        lo, hi = seg.a0, seg.a1
        with self._state_lock:
            # 识别期间如果这个 sid 被换代或结束，这批结果丢掉，不能挂到新会话上
            sid = self._by_gen.get(gen)
            sess = self._sessions.get(sid) if sid is not None else None
            if sess is None or sess.gen != gen:
                return
            sess.audio_s += max(seg.a1 - seg.a0, 0.0)
            sess.asr_s += asr_s
            explicit = sess.explicit
            session_sid = sess.sid
            emitted = []
            for u in utts:
                a0 = min(max(seg.audio_t0 + u.start, lo), hi)
                a1 = min(max(seg.audio_t0 + u.end, a0), hi)
                sentence_id = sess.next_id
                sess.next_id += 1
                emitted.append((sentence_id, a0, a1, u.text))
        for sentence_id, a0, a1, text in emitted:
            event = {"ev": "final", "id": sentence_id, "a0": round(a0, 3),
                     "a1": round(a1, 3), "text": text}
            if explicit:
                event["sid"] = session_sid
            self._emit(event)
            self._put_bounded(self._tx_q, session_sid, (gen, sentence_id, text), MAX_TX_BACKLOG, "tx", sess)
        self.log("segment", seg_s=seg.a1 - seg.a0, asr_s=asr_s, wait_s=seg.close_wait_s,
                 queue_s=queue_s, sentences=len(utts))

    def _tx_loop(self) -> None:
        while True:
            qsid, (gen, sentence_id, text) = self._tx_q.get()
            try:
                with self._state_lock:
                    sess = self._sessions.get(qsid)
                    if sess is None or sess.gen != gen or sess.translator is None:
                        continue
                    tr, src, dst = sess.translator, sess.src, sess.dst
                    context, sess.prev_src = sess.prev_src, text
                t0 = time.perf_counter()
                translate_ctx = getattr(tr, "translate_ctx", None)
                if translate_ctx is not None:
                    out = translate_ctx(text, src, dst, context)
                else:
                    out = tr.translate(text, src, dst)
                # 「查代数 + 发事件」必须在同一把锁里：翻译期间 hello/end 可能已收掉这个 sid
                with self._state_lock:
                    sess = self._sessions.get(qsid)
                    if sess is None or sess.gen != gen:
                        continue
                    if out:
                        event = {"ev": "translation", "id": sentence_id, "text": out}
                        if sess.explicit:
                            event["sid"] = qsid
                        self._emit(event)
                    else:
                        self._status("translate_failed", "一句翻译失败，已跳过", sess=sess, id=sentence_id)
                if out:
                    self.log("translated", id=sentence_id, tx_s=time.perf_counter() - t0)
                else:
                    self.log("translate_failed", id=sentence_id)
            except Exception as e:  # noqa: BLE001
                self.log("translate_error", id=sentence_id, err=type(e).__name__)
                with self._state_lock:
                    sess = self._sessions.get(qsid)
                    if sess is not None and sess.gen == gen:
                        self._status("translate_failed", "一句翻译失败，已跳过", sess=sess, id=sentence_id)
            finally:
                self._tx_q.task_done(qsid)


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

    holder: list = []  # Worker 在 main 末尾才建；handler 经此访问，建好前为空

    def _term(_signum, _frame):
        # gateway 保温到期只发 SIGTERM（不 drain、不关 stdin），EOF 路径走不到，
        # 所以在这里尽力收口当前会话的 rtf；只用已累计数据，不等在飞识别，
        # 限时拿锁（0.5s）远小于 gateway 的 3s 宽限，任何异常吞掉照常退出。
        if holder:
            try:
                holder[0]._finish_session(lock_timeout=0.5)
            except BaseException as e:  # noqa: BLE001
                try:
                    _log(err, "rtf_write_failed", err=type(e).__name__)
                except BaseException:  # noqa: BLE001
                    pass
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

        asr = WhisperEngine()
        SileroVad()  # 模型加载失败走退出码 3；每个会话再各自 new（VAD 的 h/c 不能共享）
    except Exception as e:  # noqa: BLE001
        _log(err, "engine_load_failed", err=type(e).__name__)
        sys.exit(EXIT_ENGINE)

    worker = Worker(sys.stdin.buffer, out, asr, vad_factory=SileroVad, err=err)
    holder.append(worker)
    sys.exit(worker.run())


if __name__ == "__main__":
    main()
