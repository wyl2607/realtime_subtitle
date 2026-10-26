"""
主程序入口
整合所有模块，协调工作流程
"""
import functools
import warnings
import logging
import sys
import os
import platform
import time
from realtime_subtitle.paths import SINGLETON_LOCK_FILE, repo_path

# ☠️ 和 realtime_subtitle.capture.audio_capture.STOP_FLAG_FILE 必须是同一个值
# （.ps1 与 Python 的跨进程约定，见 paths.py）。不直接 import 那个模块，是因为
# 它会把 sounddevice（~60ms）拉进启动路径；tests/test_startup_imports.py 对账
# 两者相等，防的是以后有人只改一边。
STOP_FLAG_FILE = repo_path(".stop")

# 控制台可能默认使用非UTF-8编码（如cp1252），会导致emoji/中文print崩溃
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# 单实例守卫：双开会有两路音频采集互抢+热键注册冲突+两个悬浮窗叠着。
# 必须放在重量级 import（torch/模型加载）之前——第二个实例秒退，
# 不浪费几秒加载时间和显存。句柄存模块级变量，进程活着就一直持有。
#
# ☠️ 测试进程 import 本模块会被这段直接 sys.exit（字幕开着时症状是"pytest
# 静默退出"）。以前靠在 import 前 monkeypatch ctypes.windll.kernel32 绕开，
# 那既脆（换 ctypes 调用方式就失效）又要写进 CLAUDE.md 让人记住；现在改成
# 环境变量开关，tests/ 里设一次即可，也不会真去占那个 mutex。
_single_instance_lock = None


def _acquire_macos_singleton_lock(lock_file=SINGLETON_LOCK_FILE):
    import fcntl

    lock = open(lock_file, "w", encoding="utf-8")
    try:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("⚠️  实时字幕已经在运行了，不再启动第二个实例")
        lock.close()
        sys.exit(0)
    return lock


if sys.platform == "win32" and not os.environ.get("REALTIME_SUBTITLE_NO_SINGLETON"):
    import ctypes
    # ☠️ 错误码必须走 use_last_error=True 的独立句柄 + ctypes.get_last_error()。
    # CPython 文档明确写着**不能**直接调 windll.kernel32.GetLastError()：ctypes
    # 会在你那次调用和这次取值之间发起别的 Windows API 调用，错误码可能已被
    # 覆盖。它一直"能用"是运气；一旦读到 0，守卫失效 = 双开，而双开的症状
    # （两路 WASAPI 互抢、热键注册冲突、两个悬浮窗叠着）极难归因到这里。
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.CreateMutexW.argtypes = (ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p)
    _kernel32.CreateMutexW.restype = ctypes.c_void_p  # HANDLE 是 64 位，别让它被截成 int
    _single_instance_mutex = _kernel32.CreateMutexW(
        None, False, "realtime_subtitle_single_instance")
    if ctypes.get_last_error() == 183:  # ERROR_ALREADY_EXISTS
        print("⚠️  实时字幕已经在运行了，不再启动第二个实例")
        # 启动脚本是Hidden窗口+日志重定向，print用户看不见——弹个会自动
        # 消失的提示框（MessageBoxTimeoutW：user32未文档化但十几年稳定存在）
        try:
            ctypes.windll.user32.MessageBoxTimeoutW(
                None, "实时字幕已经在运行了，没有启动第二个。",
                "实时字幕", 0x40 | 0x1000, 0, 8000)  # INFO | SYSTEMMODAL, 8秒自动关
        except Exception:
            pass
        sys.exit(0)
elif sys.platform == "darwin" and not os.environ.get("REALTIME_SUBTITLE_NO_SINGLETON"):
    _single_instance_lock = _acquire_macos_singleton_lock()

# 在导入其他模块前先禁用所有警告和日志
warnings.filterwarnings("ignore")
os.environ['PYTHONWARNINGS'] = 'ignore'

# 压掉三方库的日志噪音，但保留ERROR——之前全禁到CRITICAL，
# faster-whisper的真实报错也被吞掉了，排障时什么都看不到
logging.basicConfig(level=logging.ERROR)
_LOG = logging.getLogger(__name__)
# 节点 connect 外层兜底超时 = ready 截止上限（loading 续期最长 60s）+ 这点余量。
# 余量覆盖 websockets 建连（默认 open_timeout 10s）和收尾；外层不能比 connect 自己先到期。
NODE_CONNECT_MARGIN_SECONDS = 15.0

# ⚠️ torch 必须在任何 Qt 导入【之前】——这条在 PyQt5 时代是硬伤：先 PyQt5
# 后 torch = `OSError WinError 1114`（c10.dll 初始化例程失败），100% 复现。
#
# ☠️ **迁到 PyQt6 之后它不再复现了，但这一行仍然别删、别挪。**
# 2026-09-20 迁移第 3 步实测（同机、torch 2.14.0+cpu，两个方向各起一个干净
# 进程）：PyQt5 先导 → 照样 WinError 1114；PyQt6 先导 → 两个方向都干净通过。
# 保留的理由是它一分钱不花：ctranslate2 本来就会无条件 import torch，这里
# 只是把那次 import 提前到一个确定的位置。拿一行零成本的保险，去换一个
# 只在"本机 + 这个 PyQt6/Qt/torch 版本组合"上验过一次的结论，不划算。
# 真要删，先在别的机器上把两个方向都复现一遍。见 CLAUDE.md 第 4 节第 1 条。
# 只在 Windows 上做：macOS 走 MLX，ctranslate2 不会去拉 torch，"零成本"的前提
# 不成立——无条件 import 实测白占 ~180MB 常驻内存，而 Mac 上内存正是降档的触发源
if sys.platform == "win32":
    import torch  # noqa: F401
# ☠️ 识别/翻译和音频采集这两个模块故意**不在这里 import**（2026-10-21 实测）：
# translator_queue 会拉进 numpy/requests（~97ms），audio_capture 会拉进
# sounddevice（~62ms）——都不参与窗口构造，却让窗口晚出现约 0.16 秒。
# 现在它们在 _load_models 的后台线程里导入，窗口构造不再为它们付钱。
# （2026-10-21 另试过在窗口构造期间预取 MLX 权重：A/B 三轮里就绪时间没有
#  稳定收益、窗口反而慢 ~70ms——加载和窗口构造抢同一块 CPU/内存，已放弃。）
from realtime_subtitle.ui.subtitle_window import SubtitleWindow
from realtime_subtitle.ui.settings_window import MODE_ICONS as _MODE_ICON
from realtime_subtitle.resources.memory import TierGovernor, read_memory_snapshot
from PyQt6.QtCore import QObject, QTimer, pyqtSignal
import realtime_subtitle.config as config
# 纯常量模块，没有任何 import，放这里不影响上面那条 torch/PyQt6 的顺序约束
from realtime_subtitle.version import version_string
# 同样是纯 stdlib（pathlib），不影响 DLL 顺序
from realtime_subtitle.migrate_legacy import migrate_legacy_runtime_files


def _auto_tiers_from_start(start_tier, tiers, max_tier=None, mlx_backend=True):
    if not mlx_backend:
        # faster-whisper 不能运行中改精度：只留 16 位的项（只降翻译）
        usable = [tier for tier in tiers if tier[0] == 16]
    elif start_tier[0] != 16:
        # 预量化仓库（如 --lean 的 turbo-q4）：权重本身就是低位，原地量化是空操作、
        # "升回 fp16"重载的也还是它——识别这一维固定，只降翻译
        models = list(dict.fromkeys(model for _, model in tiers))
        usable = [(start_tier[0], model) for model in models]
    else:
        usable = list(tiers)
    if start_tier not in usable:
        return []
    cap = max_tier or start_tier
    if cap not in usable:
        return []
    return list(usable)[usable.index(cap):]


def _tier_name(tier):
    bits, model = tier
    if model is None:
        return f"fp{bits}" if bits == 16 else f"{bits}bit"
    return f"fp{bits}+{model}" if bits == 16 else f"{bits}bit+{model}"


def _startup_whisper_bits():
    repo = str(getattr(config, "WHISPER_MLX_REPO", "")).lower()
    if "q4" in repo or "4bit" in repo or "4-bit" in repo:
        return 4
    if "q8" in repo or "8bit" in repo or "8-bit" in repo:
        return 8
    return 16


class _GuiCaller(QObject):
    """把后台线程的 UI 改动排回主线程。

    Qt6 里从工作线程直接改控件，或从工作线程用 QTimer.singleShot，都不安全。
    信号在主线程创建，emit 会排队到主线程再执行。
    """

    call = pyqtSignal(object)

    def __init__(self):
        super().__init__()
        self.call.connect(self._run)

    def _run(self, fn):
        fn()


def _pcm_int16(audio):
    """采集回调是 float32。云端分段和节点都要 s16le，这里只做这一次转换。"""
    import numpy as np

    arr = np.asarray(audio)
    if arr.size == 0:
        return np.zeros(0, dtype=np.int16)
    if arr.dtype == np.int16:
        return np.ascontiguousarray(arr).reshape(-1)
    clipped = np.clip(np.asarray(arr, dtype=np.float32), -1.0, 1.0)
    return np.ascontiguousarray((clipped * 32767.0).astype(np.int16)).reshape(-1)


def _coerce_revision(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _reinsert_pair_by_t0(window, key, source):
    """远端句替换后按 t0 插回 sentence_pairs，保持时间递增。

    add_pair 只会追加。远端结果往往比后出现的本机句更早，留在末尾会把旧句顶到底行。
    """
    pairs = window.sentence_pairs
    found = None
    for index in range(len(pairs) - 1, -1, -1):
        item = pairs[index]
        if len(item) >= 7 and item[6] == key:
            found = index
            break
    if found is None:
        return
    item = pairs.pop(found)
    item = (item[0], item[1], item[2], item[3], item[4], source, item[6])
    t0 = item[3]
    insert_at = len(pairs)
    if t0 is not None:
        for index, existing in enumerate(pairs):
            existing_t0 = existing[3] if len(existing) > 3 else None
            if existing_t0 is None:
                existing_t0 = float("-inf")
            try:
                later = existing_t0 > t0
            except TypeError:
                later = False
            if later:
                insert_at = index
                break
    pairs.insert(insert_at, item)
    window._render()


class RemoteBridge:
    """本机句和远端句的汇合点。

    两个开关都关时不要构造它。云端对象只在云端开关开着时建；
    NodeClient 只在真正要连节点时建，避免云端档还去打开节点套接字。
    """

    def __init__(
        self, *, translator, router, routing, cloud, segmenter, ledger,
        cloud_on, node_on, ui_show, ui_drop, ui_update_dst, ui_status,
        prepare=None, node_client_factory=None, cloud_key_path=None,
    ):
        import concurrent.futures

        self.translator = translator
        self.router = router
        self.routing = routing
        self.cloud = cloud
        self.segmenter = segmenter
        self.ledger = ledger
        self.cloud_on = bool(cloud_on)
        self.node_on = bool(node_on)
        self._ui_show = ui_show
        self._ui_drop = ui_drop
        self._ui_update_dst = ui_update_dst
        self._ui_status = ui_status
        self._prepare = prepare
        self._node_client_factory = node_client_factory
        self._closed = False
        self._generation = 0
        self._saw_cloud = False
        self._announced = None
        self._node_client = None
        # 每建一个节点客户端递增，作为这次连接的序号。节点句 id 是每会话从头
        # 编号的，四张节点状态表的键必须带上它，否则重连后同号句会串到上一
        # 会话的行上。不是最新一次发起的连接，它的事件（迟到的 closed/translation）
        # 一律丢弃；最新这次哪怕还在 connect 里（loading 早于 ready 就会到）也照收。
        self._node_conn_seq = 0
        # 正在等节点 ready 的节点 id。只有这段时间状态行显示加载中后缀。
        self._node_loading_id = None
        self._loop = None
        self._node_thread = None
        self._node_backoff_until = 0.0
        self._skipped_nodes = set()
        self._offline_until = {}
        self._node_dst_keys = {}
        self._early_node_dst = {}
        self._node_src_by_id = {}
        self._node_translate_failed = set()
        self._node_translate_failed_notified = set()
        self._fed_samples = 0
        self._language_revision = 0
        # 冷却时长只存在 routing_rules.cooldown_seconds，到点与否由桥自己记。
        self._routing_checked_at = None
        self._cooldown_deadline = None
        self._cooldown_sig = None
        self._cloud_key_path = cloud_key_path
        # key 判定和路由变更的专用锁。不能复用 _lock：那把锁只护行存储，
        # 而这里要把「读指纹 → 决定是否 key_modified / http_status」做成原子。
        self._key_lock = __import__("threading").Lock()
        self._cloud_key_digest, _ = self._read_cloud_key_digest()
        # 分段器的 a0 从这次开桥算起。加上开桥时本机识别已经走过的样本，
        # 远端句才能和本机句用同一套 t0/t1。中途才打开时，收件箱里还没
        # 识别的音频可能对不齐，这是已知限制。
        clock = int(getattr(translator, "_sample_clock_samples", 0) or 0)
        self._origin_seconds = clock / float(config.SAMPLE_RATE)
        from realtime_subtitle.remote.line_store import LineStore
        self.line_store = LineStore()
        self._lock = __import__("threading").Lock()
        # 单线程：云端转写、额度记账、节点缺译文时的本机翻译串行。
        # _translate_single_sentence 本来跑在翻译 worker 上，这里再开一条
        # 线程会和它抢同一个 Ollama session。收成一条，至少云端/节点之间不并发。
        self._pool = concurrent.futures.ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="remote-asr")
        self._announce_route(notify=False)

    def close(self):
        self._closed = True
        self._generation += 1
        if self.cloud is not None:
            try:
                self.cloud.cancel()
            except Exception as exc:
                print(f"⚠️  取消云端请求失败: {type(exc).__name__}")
        self._close_node()
        pool = self._pool
        self._pool = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    def flush(self):
        """等已经提交的云端/翻译任务跑完。测试用来同步，不关线程池。"""
        pool = self._pool
        if pool is not None:
            pool.submit(lambda: None).result()
        loop = self._loop
        if loop is not None:
            import asyncio
            asyncio.run_coroutine_threadsafe(asyncio.sleep(0), loop).result(timeout=2)

    def set_prefs(self, cloud_on, node_on):
        """只改开关。关云端要作废在飞结果；组合变了由 app 拆掉重建。"""
        cloud_on = bool(cloud_on)
        node_on = bool(node_on)
        if self.cloud_on and not cloud_on:
            self.routing.apply({"type": "switch_off"})
            self._generation += 1
            if self.cloud is not None:
                self.cloud.cancel()
            self.cloud_on = False
        elif cloud_on and not self.cloud_on:
            self.routing.apply({"type": "switch_on"})
            self.cloud_on = True
        if self.node_on and not node_on:
            self._close_node()
            self.node_on = False
        elif node_on and not self.node_on:
            self.node_on = True
        self._sync_provider()
        self._announce_route()

    def on_language_applied(self, src=None, tgt=None, revision=0):
        """语言对真正生效的边界：作废此前发出的远端请求/节点会话。"""
        del src, tgt
        self._language_revision = _coerce_revision(revision)
        self._generation += 1
        self._early_node_dst.clear()
        self._node_src_by_id.clear()
        self._node_dst_keys.clear()
        self._node_translate_failed.clear()
        if self.cloud is not None:
            try:
                self.cloud.cancel()
            except Exception as exc:
                print(f"⚠️  取消云端请求失败: {type(exc).__name__}")
        self._close_node()

    def _stale_remote_generation(self, generation, where):
        if generation == self._generation and not self._closed:
            return False
        _LOG.debug(
            "stale remote %s discarded: generation=%s current=%s closed=%s",
            where,
            generation,
            self._generation,
            self._closed,
        )
        return True

    def route_label(self):
        from realtime_subtitle.ui import design_tokens

        self._sync_provider()
        state = str(getattr(self.routing, "cloud_state", "") or "")
        # key 无效是按住不放的状态，优先于层选择：这时 tier 已经退回节点/本机，
        # 光看 tier 会把它显示成普通混合档，用户不知道要换 key。
        if state == "disabled_invalid_key":
            return design_tokens.TEXT_STATUS_CLOUD_KEY_INVALID
        tier = "local"
        select = getattr(self.router, "select_tier", None)
        if select is not None:
            try:
                tier = select()
            except Exception:
                tier = "local"
        if tier == "cloud":
            label = design_tokens.TEXT_STATUS_CLOUD
        elif tier == "node":
            name = getattr(self.router, "current_node_id", None)
            if not name:
                try:
                    cands = self.router.filter_candidates()
                except Exception:
                    cands = []
                if cands:
                    name = cands[0].config.id
            if self._node_loading_id:
                # 冷加载期间用「节点名 正在加载模型…」；本机识别照常先上屏。
                label = f"{self._node_loading_id}{design_tokens.TEXT_NODE_LOADING_SUFFIX}"
            else:
                label = design_tokens.TEXT_STATUS_HYBRID_PREFIX + (name or "节点")
        else:
            label = design_tokens.TEXT_STATUS_LOCAL
        if state == "cooldown_rate_limited":
            label += design_tokens.TEXT_STATUS_CLOUD_COOLDOWN_SUFFIX
        return label

    def on_local_pair(self, src, tgt, t0, t1, key, revision):
        revision = _coerce_revision(revision)
        self._language_revision = revision
        with self._lock:
            self.line_store.add_local(key, t0, t1, src or "", tgt)
            kept = any(line.key == key for line in self.line_store.lines)
        # 已经被远端盖住的本机句不再上屏。没盖住的仍走原来的本地来源。
        if kept:
            self._ui_show(src, tgt, t0, t1, key, "local", revision)

    def feed_audio(self, audio):
        if self._closed:
            return
        pcm = _pcm_int16(audio)
        if pcm.size == 0:
            return
        rate = float(config.SAMPLE_RATE)
        start = self._origin_seconds + (self._fed_samples / rate)
        self._fed_samples += int(pcm.shape[0])
        self._track_silence(pcm, pcm.shape[0] / rate)
        self._recover_routing()
        self._sync_provider()
        tier = "local"
        select = getattr(self.router, "select_tier", None)
        if select is not None:
            tier = select()
        if tier == "cloud":
            self._saw_cloud = True
        # 开关开着就一直喂，分段器的样本钟才不会在降档时空转落后。
        # 非云端档吐出来的段直接丢掉，不上传。
        if self.cloud_on and self.segmenter is not None:
            import numpy as np
            segments = self.segmenter.feed(pcm)
            if tier == "cloud" and self._pool is not None:
                for a0, a1, seg in segments:
                    copied = np.array(seg, dtype=np.int16, copy=True)
                    self._pool.submit(self._transcribe_one, float(a0), float(a1), copied, self._generation)
        if not self.node_on or tier == "cloud":
            return
        # 从云端退到节点要等一段静音，免得一句话被切两半。本机识别一直在跑。
        # 节点本来就是首选时不等待，直接连。
        if self._node_client is None:
            self._remember_for_replay(pcm, start)
            if self._saw_cloud and not self._silence_ready():
                return
            self._ensure_node()
            return
        self._send_node_bytes(pcm.tobytes(), start)

    def _transcribe_one(self, a0, a1, pcm, generation):
        if self._stale_remote_generation(generation, "cloud_start"):
            return
        try:
            # 分段器的 a0 从开桥算起，交给客户端时加上原点，返回的 start/end 已是绝对时间。
            transcript = self.cloud.transcribe(
                pcm, a0=self._origin_seconds + a0, language=config.SOURCE_LANGUAGE)
        except Exception as exc:
            self._on_cloud_exception(exc, generation)
            return
        if self._stale_remote_generation(generation, "cloud_result"):
            return
        try:
            self.routing.apply({"type": "segment_success"})
            if self.ledger is not None:
                self.ledger.note(
                    transcript.duration_s,
                    remaining_requests=transcript.remaining_requests,
                    reset_requests_s=transcript.reset_requests_s,
                    key_fp=transcript.key_fingerprint,
                    sent_at=transcript.sent_at,
                )
        except Exception as exc:
            print(f"⚠️  云端记账失败: {type(exc).__name__}")
        self._sync_provider()
        segments = list(getattr(transcript, "segments", ()) or ())
        if not segments and getattr(transcript, "text", ""):
            segments = [_LooseSegment(self._origin_seconds + a0, self._origin_seconds + a1, transcript.text)]
        for seg in segments:
            if self._stale_remote_generation(generation, "cloud_segment"):
                return
            text = (getattr(seg, "text", "") or "").strip()
            if not text:
                continue
            dst = self._translate(text)
            if self._stale_remote_generation(generation, "cloud_translation"):
                return
            self._publish_remote("cloud", getattr(seg, "start", None), getattr(seg, "end", None), text, dst)
        self._announce_route()

    def _on_cloud_exception(self, exc, generation):
        from realtime_subtitle.remote.cloud_client import CloudCancelled, CloudError
        if isinstance(exc, CloudCancelled) or self._stale_remote_generation(generation, "cloud_exception"):
            return
        if isinstance(exc, CloudError):
            status = exc.status
            reached = status is not None
            routed = status in (401, 403, 429) or (reached and 500 <= int(status) <= 599)
            previous_state = str(getattr(self.routing, "cloud_state", "") or "")
            status_applied = False
            stale_key = False
            if status in (401, 403):
                # 指纹比较和「打无效」必须是同一把锁里的一个决定。否则旧 key 的
                # 401 可能在音频线程换 key 之后才落盘，把新 key 永久打成
                # disabled_invalid_key 且之后再也不恢复。
                with self._key_lock:
                    if self._stale_remote_generation(generation, "cloud_exception"):
                        return
                    digest, fingerprint = self._read_cloud_key_digest()
                    if (
                        digest is not None
                        and getattr(exc, "key_fingerprint", None) is not None
                        and exc.key_fingerprint != fingerprint
                    ):
                        # _recover_routing 尚未到下一秒检查点时，401/403 仍可能先到。
                        # 它属于旧 key，不能让 routing_rules 锁死新 key。
                        stale_key = True
                        self._apply_cloud_key_modified_locked(digest)
                    else:
                        self.routing.apply({
                            "type": "http_status",
                            "status": int(status),
                            "retry_after": exc.retry_after,
                        })
                        status_applied = True
            if stale_key:
                self._sync_provider()
                self._announce_route()
                return
            try:
                if not status_applied:
                    if routed:
                        self.routing.apply({
                            "type": "http_status",
                            "status": int(status),
                            "retry_after": exc.retry_after,
                        })
                    else:
                        self.routing.apply({"type": "segment_failure"})
                if reached and self.ledger is not None:
                    self.ledger.note(
                        exc.duration_s,
                        remaining_requests=exc.remaining_requests,
                        reset_requests_s=exc.reset_requests_s,
                        key_fp=exc.key_fingerprint,
                        sent_at=exc.sent_at,
                    )
                if status == 429 and previous_state != "cooldown_rate_limited":
                    # 只在进入冷却时打一行；和 rslite 同一格式，不含 key 和正文。
                    seconds = getattr(self.routing, "cooldown_seconds", None) or 60
                    print(f"rslite.cloud cooldown reason=429 retry_after={int(seconds)}")
            except Exception as note_exc:
                print(f"⚠️  云端失败记账出错: {type(note_exc).__name__}")
        else:
            # 不打印异常文本：requests 的消息里可能夹着请求头。
            print(f"⚠️  云端识别失败: {type(exc).__name__}")
            try:
                self.routing.apply({"type": "segment_failure"})
            except Exception:
                pass
        self._sync_provider()
        self._announce_route()

    def _read_cloud_key_digest(self):
        """返回 key 内容的完整摘要和可公开的短指纹；读不到 key 时不改变路由。"""
        if not self.cloud_on or self.cloud is None or self._cloud_key_path is None:
            return None, None
        from realtime_subtitle.remote.cloud_client import CloudError, read_groq_key
        try:
            key = read_groq_key(self._cloud_key_path)
        except (CloudError, OSError, UnicodeError):
            # key 文件可能被写到一半或不是 UTF-8。读不出来就不改变路由，
            # 也不能让 feed_audio / 建桥因此抛异常。
            return None, None
        if key is None:
            return None, None
        import hashlib
        digest = hashlib.sha256(key.encode("utf-8")).digest()
        return digest, digest.hex()[:12]

    def _apply_cloud_key_modified(self, digest):
        """换 key：只作废云端自己的在飞请求，再让 routing_rules 恢复云端。

        ☠️ 不推进 self._generation：它同时是节点会话的代次（NodeClient 构造时
        固化 session_generation），推进它会让混合档下节点字幕被整批当成 stale
        丢掉。云端自己的 generation 由 cloud.cancel() 推进；排队中的段之后执行
        时会重新读 key 文件，自然用上新 key。
        """
        with self._key_lock:
            changed = self._apply_cloud_key_modified_locked(digest)
        if not changed:
            return
        self._sync_provider()
        self._announce_route()

    def _apply_cloud_key_modified_locked(self, digest):
        """换 key 的实际动作。调用方必须已持有 self._key_lock。"""
        if not self.cloud_on or self.cloud is None:
            return False
        if digest == self._cloud_key_digest:
            return False
        self._cloud_key_digest = digest
        try:
            self.cloud.cancel()
        except Exception as exc:
            print(f"⚠️  取消云端请求失败: {type(exc).__name__}")
        self.routing.apply({"type": "key_modified"})
        return True

    def _translate(self, text):
        """云端句、以及节点还没给译文的句子，走本机现有的单句翻译。"""
        if not text or self.translator is None:
            return ""
        try:
            translated = self.translator._translate_single_sentence(text, "")
        except Exception as exc:
            print(f"⚠️  远端句子翻译失败: {type(exc).__name__}")
            return ""
        return translated or ""

    def _publish_remote(self, node_id, t0, t1, src, dst):
        with self._lock:
            replaced = self.line_store.apply_remote(node_id, t0, t1, src, dst)
            remote_key = None
            for line in reversed(self.line_store.lines):
                if line.source == "remote" and line.src == src and line.t0 == t0:
                    remote_key = line.key
                    break
        if replaced:
            self._ui_drop(replaced)
        self._ui_show(src, dst or "", t0, t1, remote_key, "remote", self._language_revision)
        return remote_key

    def _recover_routing(self):
        """音频驱动的冷却恢复。至少隔 1 秒看一次，避免每个音频块都打状态机。

        限流和退避的时长在 routing.cooldown_seconds（apply 会把它清掉，所以只在
        看到新值时记一次 deadline）。额度冷却没有秒数，靠 ledger.usage() 在
        降到 80% 以下时自己派发 quota_update。
        """
        import time
        now = time.monotonic()
        last = self._routing_checked_at
        if last is not None and (now - last) < 1.0:
            return
        self._routing_checked_at = now
        routing = self.routing
        if routing is None:
            return
        if self.cloud_on and self.cloud is not None:
            digest, _fingerprint = self._read_cloud_key_digest()
            if digest is not None:
                with self._key_lock:
                    if self._cloud_key_digest is None:
                        # 开桥时没有 key、之后才补上：只记指纹，不算一次换 key。
                        self._cloud_key_digest = digest
                        changed = False
                    else:
                        changed = self._apply_cloud_key_modified_locked(digest)
                if changed:
                    self._sync_provider()
                    self._announce_route()
        state = str(getattr(routing, "cloud_state", "") or "")
        seconds = getattr(routing, "cooldown_seconds", None)
        if state == "cooldown_quota" or not state.startswith("cooldown_"):
            self._cooldown_deadline = None
            self._cooldown_sig = None
        elif isinstance(seconds, (int, float)) and not isinstance(seconds, bool):
            signature = (state, int(seconds))
            if signature != self._cooldown_sig:
                self._cooldown_sig = signature
                self._cooldown_deadline = now + float(seconds)
        deadline = self._cooldown_deadline
        if (
            deadline is not None
            and state.startswith("cooldown_")
            and state != "cooldown_quota"
            and now >= deadline
        ):
            routing.apply({"type": "cooldown_expired"})
            self._cooldown_deadline = None
            self._cooldown_sig = None
            # 冷却结束要把状态行从「… · 云端限流冷却」恢复成原文案。
            self._announce_route()
        if self.ledger is not None:
            self.ledger.usage()

    def _sync_provider(self):
        stub = getattr(self.router, "cloud_provider", None)
        if stub is not None and self.routing is not None:
            stub.has_key = bool(self.routing.has_key)
            stub.cloud_enabled = bool(self.routing.cloud_enabled)
            stub.cloud_state = self.routing.cloud_state
        if hasattr(self.router, "node_enabled"):
            self.router.node_enabled = bool(self.node_on)

    def _announce_route(self, notify=True):
        label = self.route_label()
        if label == self._announced:
            return
        self._announced = label
        print(f"路由：{label}")
        if notify and self._ui_status is not None:
            self._ui_status(f"路由：{label}")

    def _track_silence(self, pcm, duration):
        if not hasattr(self.router, "silence_run"):
            return
        from realtime_subtitle.remote.router import update_silence_run
        self.router.silence_run = update_silence_run(
            float(self.router.silence_run or 0.0), pcm.tobytes(), duration)

    def _silence_ready(self):
        from realtime_subtitle.remote.router import can_migrate_at_silence
        return can_migrate_at_silence(float(getattr(self.router, "silence_run", 0.0) or 0.0))

    def _remember_for_replay(self, pcm, start):
        buf = getattr(self.router, "replay_buffer", None)
        if buf is None:
            return
        from realtime_subtitle.remote.router import TimedAudioChunk
        buf.append(TimedAudioChunk(
            bytes(pcm.tobytes()), float(start), pcm.shape[0] / float(config.SAMPLE_RATE)))

    def _ensure_node(self):
        import time
        if self._closed or not self.node_on or self._node_client is not None:
            return
        if time.time() < self._node_backoff_until:
            return
        prepare = self._prepare
        if prepare is None:
            from realtime_subtitle.remote.node_client import prepare as prepare
        from realtime_subtitle.remote.node_client import (
            READY_LOADING_MAX_SECONDS, NodeCallbacks, SelfNodeError)
        from realtime_subtitle.remote.router import NodeStateRecord, RouteCandidate
        configs = []
        try:
            configs = list(self.router.configs_skipping_self())
        except Exception:
            configs = list(getattr(self.router, "node_configs", []) or [])
        prepared = []
        now = time.time()
        for cfg in configs:
            if cfg.id in self._skipped_nodes or now < self._offline_until.get(cfg.id, 0.0):
                continue
            started = time.perf_counter()
            try:
                _pinned, info = prepare(cfg)
            except SelfNodeError:
                self._skipped_nodes.add(cfg.id)
                continue
            except Exception as exc:
                print(f"⚠️  节点探测失败 {cfg.id}: {type(exc).__name__}")
                until = time.time() + 30
                self._offline_until[cfg.id] = until
                if hasattr(self.router, "node_states"):
                    self.router.node_states[cfg.id] = NodeStateRecord(offline_until=until)
                continue
            prepared.append((cfg, info, (time.perf_counter() - started) * 1000.0))
        if not prepared:
            self._node_backoff_until = time.time() + 30
            return
        chosen = prepared[0]
        choose = getattr(self.router, "select_best_node", None)
        if choose is not None:
            candidates = [
                RouteCandidate(
                    id=cfg.id, expected_node_id=cfg.node_id, info=info, rtt_ms=rtt)
                for cfg, info, rtt in prepared
            ]
            try:
                best = choose(candidates, time.time())
            except Exception:
                best = None
            if best is not None:
                for item in prepared:
                    if item[0].id == best.id:
                        chosen = item
                        break
        cfg, info, _rtt = chosen
        self._node_conn_seq += 1
        conn = self._node_conn_seq
        callbacks = NodeCallbacks(
            on_final=functools.partial(self._on_node_final, conn=conn),
            on_translation=functools.partial(self._on_node_translation, conn=conn),
            on_status=functools.partial(self._on_node_status, conn=conn),
            on_status_event=functools.partial(self._on_node_status_event, conn=conn),
            on_loading=functools.partial(self._on_node_loading, conn=conn),
            on_translate_failed=functools.partial(self._on_node_translate_failed, conn=conn),
            on_closed=functools.partial(self._on_node_closed, conn=conn),
        )
        try:
            client = self._build_node_client(cfg, info, callbacks)
        except Exception as exc:
            print(f"⚠️  节点客户端创建失败 {cfg.id}: {type(exc).__name__}")
            self._node_backoff_until = time.time() + 30
            return
        generation = self._generation
        self._node_loading_id = cfg.id
        self._announce_route()
        try:
            self._start_node_loop()
            # 不能用 _run_on_loop 默认的 5s：节点冷启动要 15s 起、收到 loading 最长续到 60s，
            # 5s 就放弃会让桌面端永远连不上冷启动中的节点。connect 自己的截止才是唯一真相。
            self._run_on_loop(
                client.connect(),
                timeout=READY_LOADING_MAX_SECONDS + NODE_CONNECT_MARGIN_SECONDS)
        except Exception as exc:
            print(f"⚠️  节点连接失败 {cfg.id}: {type(exc).__name__}")
            self._abandon_client(client)
            self._close_node()
            self._node_backoff_until = time.time() + 30
            self._node_loading_id = None
            self._announce_route()
            return
        # 成功路径先不播报：这时 current_node_id 还是旧值（或为空，退回第一个候选），
        # 立刻播报会让状态行闪一下别的节点名。等下面换成这次连上的节点再播报。
        self._node_loading_id = None
        # 连接在音频线程上等待。返回时主线程可能已经 close()，或云端开关把代次加一。
        # 这时不能把新客户端留在桥上。
        if self._closed or self._generation != generation:
            self._abandon_client(client)
            self._announce_route()
            return
        if hasattr(self.router, "node_states"):
            self.router.node_states[cfg.id] = NodeStateRecord(info=info)
        frames = []
        buf = getattr(self.router, "replay_buffer", None)
        if buf is not None:
            from realtime_subtitle.remote.router import should_replay_buffered_audio
            # 这里只在「当前没有客户端」时进来，按首次接入回放缓冲。
            # 已经连着的时候换节点不走这个函数，所以不会把迁移也回放一遍。
            if should_replay_buffered_audio(False, False):
                frames = list(buf.frames)
            buf.clear()
        self._node_client = client
        self._prune_node_state(conn)
        if hasattr(self.router, "current_node_id"):
            self.router.current_node_id = cfg.id
        for frame in frames:
            self._send_node_bytes(frame.data, frame.start_seconds)
        self._announce_route()

    def _build_node_client(self, cfg, info, callbacks):
        if self._node_client_factory is not None:
            return self._node_client_factory(cfg, info, callbacks)
        from realtime_subtitle.remote.node_client import NodeClient, read_token_file
        token = read_token_file(cfg.token_file)
        return NodeClient(
            cfg,
            token,
            info,
            source_locale_id=str(getattr(config, "SOURCE_LANGUAGE", "de")),
            target_language_id=str(getattr(config, "TARGET_LANGUAGE", "zh")),
            session_generation=self._generation,
            callbacks=callbacks,
        )

    def _start_node_loop(self):
        import asyncio
        import threading
        loop = self._loop
        thread = self._node_thread
        if loop is not None and thread is not None and thread.is_alive() and not loop.is_closed():
            return
        self._stop_node_loop()
        loop = asyncio.new_event_loop()
        self._loop = loop

        def _run():
            asyncio.set_event_loop(loop)
            loop.run_forever()

        self._node_thread = threading.Thread(target=_run, name="remote-node", daemon=True)
        self._node_thread.start()

    def _stop_node_loop(self):
        import threading
        loop = self._loop
        thread = self._node_thread
        self._loop = None
        self._node_thread = None
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(loop.stop)
            except Exception:
                pass
        if thread is not None and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=2)
        if loop is not None and not loop.is_closed():
            try:
                loop.close()
            except Exception:
                pass

    def _abandon_client(self, client):
        """连接还没挂到 self._node_client 上就要作废。优先用现有 loop。"""
        if client is None:
            return
        loop = self._loop
        try:
            if loop is not None and not loop.is_closed():
                self._run_on_loop(client.close(), timeout=2)
                return
        except Exception as exc:
            print(f"⚠️  节点连接作废失败: {type(exc).__name__}")
        try:
            import asyncio
            asyncio.run(client.close())
        except Exception as exc:
            print(f"⚠️  节点连接作废失败: {type(exc).__name__}")

    def _run_on_loop(self, coro, timeout=5):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=timeout)

    def _send_node_bytes(self, data, start):
        client = self._node_client
        loop = self._loop
        if client is None or loop is None or self._closed or not data:
            return
        import asyncio
        asyncio.run_coroutine_threadsafe(client.send(data, start), loop)

    def _close_node(self):
        client = self._node_client
        self._node_client = None
        if client is not None and self._loop is not None and not self._loop.is_closed():
            try:
                self._run_on_loop(client.close(), timeout=2)
            except Exception as exc:
                print(f"⚠️  节点关闭失败: {type(exc).__name__}")
        self._stop_node_loop()

    def _node_conn_stale(self, conn):
        """conn=None 是测试直接调用时的旧签名；真实回调一定带序号。"""
        return conn is not None and conn != self._node_conn_seq

    def _prune_node_state(self, conn):
        """新连接挂上桥时清掉上一会话的残留，按键里的连接序号隔离。"""
        for table in (self._node_src_by_id, self._early_node_dst, self._node_dst_keys):
            for key in [key for key in list(table) if key[1] != conn]:
                table.pop(key, None)
        # 原地删，不整体重新赋值：节点线程可能正往里 add 新连接的标记，换成新集合会把它丢掉。
        for key in [key for key in list(self._node_translate_failed) if key[1] != conn]:
            self._node_translate_failed.discard(key)

    def _on_node_final(self, node_id, generation, final_id, body, t0, t1, conn=None):
        text = (body or "").strip()
        if not text or self._node_conn_stale(conn) or self._stale_remote_generation(generation, "node_final"):
            return
        node_key = (node_id, conn, final_id)
        self._node_src_by_id[node_key] = text

        def work():
            if self._stale_remote_generation(generation, "node_final_work"):
                return
            # 节点译文通常晚于 final。先用本机翻译顶上，译文到了再覆盖。
            # 已经先到的译文直接用，不再打一次 Ollama。
            early = self._early_node_dst.pop(node_key, None)
            dst = early if early and node_key not in self._node_translate_failed else self._translate(text)
            if self._stale_remote_generation(generation, "node_final_translate"):
                return
            remote_key = self._publish_remote(node_id, t0, t1, text, dst)
            self._node_dst_keys[node_key] = remote_key

        self._pool.submit(work)

    def _on_node_translation(self, node_id, generation, tx_id, body, conn=None):
        if self._node_conn_stale(conn):
            return
        if self._stale_remote_generation(generation, "node_translation"):
            return
        node_key = (node_id, conn, tx_id)
        if node_key in self._node_translate_failed or not (body or ""):
            return

        def work():
            if self._stale_remote_generation(generation, "node_translation_work"):
                return
            key = self._node_dst_keys.get(node_key)
            if key is None:
                self._early_node_dst[node_key] = body or ""
                return
            with self._lock:
                self.line_store.set_translation(key, body or "")
            if self._ui_update_dst is not None:
                self._ui_update_dst(key, body or "")

        self._pool.submit(work)

    def _notify_node_translate_failed_once(self, node_id, generation):
        marker = (node_id, generation)
        if marker in self._node_translate_failed_notified:
            return
        self._node_translate_failed_notified.add(marker)
        if self._ui_status is not None:
            self._ui_status(f"节点 {node_id}：翻译暂不可用，改用本机翻译")

    def _on_node_status_event(self, node_id, generation, event, conn=None):
        if self._node_conn_stale(conn):
            return
        if self._stale_remote_generation(generation, "node_status_event"):
            return
        code = event.get("code") if isinstance(event, dict) else None
        if code == "translate_failed":
            self._notify_node_translate_failed_once(node_id, generation)

    def _on_node_translate_failed(self, node_id, generation, tx_id, conn=None):
        if self._node_conn_stale(conn):
            return
        if self._stale_remote_generation(generation, "node_translate_failed"):
            return
        self._notify_node_translate_failed_once(node_id, generation)
        node_key = (node_id, conn, tx_id)
        self._node_translate_failed.add(node_key)

        def work():
            if self._stale_remote_generation(generation, "node_translate_failed_work"):
                return
            src = self._node_src_by_id.get(node_key, "")
            key = self._node_dst_keys.get(node_key)
            if not src or key is None:
                return
            dst = self._translate(src)
            if self._stale_remote_generation(generation, "node_translate_failed_retry") or not dst:
                return
            with self._lock:
                self.line_store.set_translation(key, dst)
            if self._ui_update_dst is not None:
                self._ui_update_dst(key, dst)

        self._pool.submit(work)

    def _on_node_status(self, text, conn=None):
        if self._node_conn_stale(conn):
            return
        if self._ui_status is not None and text:
            self._ui_status(text)

    def _on_node_loading(self, node_id, generation, conn=None):
        if self._node_conn_stale(conn):
            return
        if self._stale_remote_generation(generation, "node_loading"):
            return
        from realtime_subtitle.ui import design_tokens

        self._node_loading_id = node_id
        if self._ui_status is not None:
            # 每 5s 一条 loading：重新挂状态行，避免 5s 自动清空后在剩下的冷加载里看不见。
            self._ui_status(f"{node_id}{design_tokens.TEXT_NODE_LOADING_SUFFIX}")

    def _on_node_closed(self, node_id, generation, error, conn=None):
        if self._node_conn_stale(conn):
            return
        if self._stale_remote_generation(generation, "node_closed"):
            return
        if error is not None:
            print(f"⚠️  节点断开 {node_id}: {type(error).__name__}")
        self._node_client = None
        import time
        self._node_backoff_until = time.time() + 5


class _LooseSegment:
    def __init__(self, start, end, text):
        self.start = start
        self.end = end
        self.text = text


def open_remote_bridge(
    *, cloud_on, node_on, translator, ui_show, ui_drop, ui_update_dst=None,
    ui_status=None, cloud_client_factory=None, segmenter_factory=None,
    ledger_factory=None, router_factory=None, prepare=None,
    node_client_factory=None, node_entries=None, key_path=None, quota_path=None,
):
    """开关都关时直接返回，不 import、不调用任何远端工厂。"""
    if not cloud_on and not node_on:
        return None
    from realtime_subtitle.remote.router import CloudProviderStub, RemoteRouter, read_self_node_id
    from realtime_subtitle.remote.routing_rules import RoutingState
    from realtime_subtitle.ui.settings_window import config_dir, groq_key_file, load_node_entries

    if cloud_on:
        if cloud_client_factory is None:
            from realtime_subtitle.remote.cloud_client import CloudClient
            cloud_client_factory = CloudClient
        if segmenter_factory is None:
            from realtime_subtitle.remote.cloud_segmenter import CloudSegmenter
            segmenter_factory = CloudSegmenter
        if ledger_factory is None:
            from realtime_subtitle.remote.quota import QuotaLedger
            ledger_factory = QuotaLedger
    if router_factory is None:
        router_factory = RemoteRouter

    routing = RoutingState(
        has_key=bool(cloud_on),
        cloud_enabled=bool(cloud_on),
        cloud_state="available" if cloud_on else "off",
        node_enabled=bool(node_on),
    )
    cloud = None
    segmenter = None
    ledger = None
    cloud_key_path = None
    if cloud_on:
        cloud_key_path = key_path or groq_key_file()
        cloud = cloud_client_factory(key_path=cloud_key_path)
        segmenter = segmenter_factory()
        qpath = quota_path or (config_dir() / "quota.json")
        qpath.parent.mkdir(parents=True, exist_ok=True)
        ledger = ledger_factory(qpath, routing=routing)
    entries = []
    self_node_id = None
    if node_on:
        if node_entries is None:
            entries = load_node_entries()
        else:
            entries = list(node_entries)
        self_node_id = read_self_node_id()
    from realtime_subtitle.remote.node_client import NodeConfig
    configs = []
    for entry in entries:
        if isinstance(entry, NodeConfig):
            configs.append(entry)
        elif isinstance(entry, dict):
            configs.append(NodeConfig.from_dict(entry))
    router = router_factory(
        cloud_provider=CloudProviderStub(
            has_key=bool(cloud_on),
            cloud_enabled=bool(cloud_on),
            cloud_state=routing.cloud_state,
        ),
        node_configs=configs,
        self_node_id=self_node_id,
    )
    if hasattr(router, "node_enabled"):
        # RemoteRouter 默认把节点当成开。产品默认是关，这里按面板覆盖。
        router.node_enabled = bool(node_on)
    return RemoteBridge(
        translator=translator,
        router=router,
        routing=routing,
        cloud=cloud,
        segmenter=segmenter,
        ledger=ledger,
        cloud_on=cloud_on,
        node_on=node_on,
        ui_show=ui_show,
        ui_drop=ui_drop,
        ui_update_dst=ui_update_dst,
        ui_status=ui_status,
        prepare=prepare,
        node_client_factory=node_client_factory,
        cloud_key_path=cloud_key_path,
    )


class SubtitleApp:
    """实时字幕应用主类"""
    
    def __init__(self):
        """初始化应用：只建悬浮窗（几秒内可见），模型加载推到后台线程。

        之前是 翻译器(加载模型10-25秒)→窗口→音频 全串行，双击启动后半分钟
        屏幕上什么都没有，用户以为没启动。现在窗口先出现并显示加载进度，
        重活在 _load_models 里做完再接线。"""
        self._print_header()

        # ☠️ 必须在 SubtitleWindow 之前：窗口 __init__ 就会读 window_state.json，
        # 晚一步就读到空的、把用户调好的布局重置成默认值。见 migrate_legacy 的注释
        migrate_legacy_runtime_files()

        print("🔧 正在初始化组件...")
        self.running = False
        self.translator = None
        self.audio_capture = None
        self.remote = None

        # 这台机器的"正常档位"翻译模型：必须在任何模式切换之前拍快照
        # （此时 config_local 的显存分档已经生效）。非性能模式统一切回它
        self._baseline_ollama_model = config.OLLAMA_MODEL
        self._baseline_whisper_bits = _startup_whisper_bits()
        self._memory_tier_by_name = {}
        self._memory_tier_stop = None
        self._memory_tier_thread = None
        self._memory_tier_error_printed = False

        # 初始化字幕窗口（必须在主线程；QApplication 也在这里面建）
        try:
            self.subtitle_window = SubtitleWindow()
        except Exception as e:
            print(f"❌ 字幕窗口初始化失败: {e}")
            sys.exit(1)

        # 模式记账：窗口构造时已经从持久化状态恢复了面板高亮，这里必须读那个
        # 值而不是无条件置 None——否则重启后第一次按 Ctrl+Alt+G，"之前的模式"
        # 会被记成兜底的「直播」而不是屏幕上实际高亮的那个
        self._current_mode = self.subtitle_window.settings_window._active_preset
        self._mode_before_perf = None
        # 面板四个按钮 / 手动拨滑块 → 都回到 _apply_mode 这唯一入口
        self.subtitle_window.settings_window.on_mode_change = self._apply_mode
        self.subtitle_window.settings_window.on_language_change = self._request_language_pair
        # 远端开关不是模式。显示靠 refresh 读盘；变更只通知这里，不走 _apply_mode。
        self._gui_caller = _GuiCaller()
        self.subtitle_window.settings_window.on_remote_change = self._on_remote_prefs_changed
        self.subtitle_window.settings_window.route_status_source = self._route_status_text
        self.subtitle_window.signals.status.connect(self._on_status_refresh_panel)
        self.subtitle_window.settings_window.refresh_from_config()
        if sys.platform == "darwin":
            self._setup_hotkey()

    def _auto_tier_enabled(self):
        enabled = getattr(config, "AUTO_TIER_ENABLED", None)
        if enabled is None:
            return platform.system() == "Darwin"
        return bool(enabled)

    def _build_memory_governor(self):
        if not self._auto_tier_enabled():
            return None
        from realtime_subtitle.asr.backends import selected_whisper_backend
        mlx_backend = selected_whisper_backend() == "mlx"
        apple_translation = self._apple_translation_active()
        if apple_translation:
            # Apple Translation 不驻留 Ollama 翻译模型，内存压力只剩 Whisper 精度。
            # 仍从 AUTO_TIERS 取 bits，是为了沿用同一套排序/上限配置，不另开旋钮。
            seen_bits = []
            for bits, _model in getattr(config, "AUTO_TIERS", []):
                if bits not in seen_bits:
                    seen_bits.append(bits)
            tier_source = [(bits, None) for bits in seen_bits]
            start_tier = (self._baseline_whisper_bits if mlx_backend else 16, None)
            max_tier = ((getattr(config, "AUTO_TIER_MAX", None) or (None, None))[0], None) \
                if getattr(config, "AUTO_TIER_MAX", None) else None
            tiers = _auto_tiers_from_start(
                start_tier,
                tier_source,
                max_tier,
                mlx_backend=mlx_backend,
            )
        else:
            start_tier = (
                self._baseline_whisper_bits if mlx_backend else 16,
                self._baseline_ollama_model,
            )
            tiers = _auto_tiers_from_start(
                start_tier,
                list(getattr(config, "AUTO_TIERS", [])),
                getattr(config, "AUTO_TIER_MAX", None),
                mlx_backend=mlx_backend,
            )
        if not tiers:
            print(f"🧠 启动档位 {start_tier} 不在自动分档列表，自动降档未启用")
            return None
        gb = 1024 ** 3
        self._memory_tier_by_name = {_tier_name(tier): tier for tier in tiers}
        costs = {
            _tier_name((bits, model)): int((
                getattr(config, "WHISPER_BITS_COST_GB", {}).get(bits, 0)
                + (0 if model is None else getattr(config, "OLLAMA_MODEL_COST_GB", {}).get(model, 0))
            ) * gb)
            for bits, model in tiers
        }
        return TierGovernor(
            list(self._memory_tier_by_name),
            _tier_name(start_tier if start_tier in tiers else tiers[0]),
            tier_cost_bytes=costs,
            headroom_bytes=int(getattr(config, "AUTO_TIER_HEADROOM_GB", 1.5) * gb),
        )

    def _apple_translation_active(self):
        translator = getattr(self, "translator", None)
        if getattr(translator, "_translate_backend", None) == "apple":
            return True
        return (getattr(config, "TRANSLATE_BACKEND", "auto") or "auto").strip().lower() == "apple"

    def _load_models(self):
        """后台线程：加载 Whisper/Ollama + 音频采集，完成后接线并启动。

        窗口的 status 提示 5 秒自动清除，加载要十几秒，所以循环重发；
        这里所有对窗口的调用都走 Qt 信号（show_status 等），线程安全。"""
        import threading
        done = threading.Event()

        def _ticker():
            msg = "⏳ 正在加载识别/翻译模型（约10-30秒）…"
            self.subtitle_window.show_status(msg)
            while not done.wait(4):
                if self._closing:
                    return  # 退出中别再往正在拆的窗口发信号
                self.subtitle_window.show_status(msg)

        threading.Thread(target=_ticker, daemon=True, name="LoadTicker").start()
        # 用户在加载的十几秒里随时可能点❌/跑停止脚本（stop()在主线程执行）。
        # 屏障策略：每个"会启动新活动"的步骤前都重查 _closing；translator 的
        # 线程池是非daemon线程，凡是建出来了就必须 shutdown，否则进程退不掉
        # （残余毫秒级窗口由停止脚本的超时强杀+Ollama卸载兜底，不追求零竞态）
        translator = None
        try:
            if self._closing:
                from realtime_subtitle.translate.translator_queue import release_startup_warm
                release_startup_warm()
                return
            # 延迟导入：见模块顶部说明（窗口出现前不付 numpy/requests 的 import）。
            from realtime_subtitle.translate.translator_queue import WhisperQueueTranslator
            # 翻译器（Whisper 模型 + Ollama 预热，启动耗时大头）
            translator = WhisperQueueTranslator()
            if self._closing:
                translator.shutdown()
                return

            # 接线：识别提交的德语立即上屏（live行），翻译完成变句对
            # 两个回调都走Qt信号，从任何线程调都安全
            translator.on_display = self.subtitle_window.update_live
            translator.on_pair = self.subtitle_window.add_pair
            translator.on_draft = self.subtitle_window.update_draft
            translator.on_status = self.subtitle_window.show_status
            translator.on_language_applied = self._on_language_applied
            translator.on_translate_failed = self.subtitle_window.on_translate_failed
            self.subtitle_window.on_retry_translation = (
                lambda k: self.translator.retry_translation(k) if self.translator else False
            )
            self.translator = translator  # 此后 stop() 会负责 shutdown 它
            # 模型还没好时用户拨的开关只落在文件里。这里按文件决定要不要接远端。
            self._attach_remote()
            # 点词查词：窗口点击→translator查Ollama→回调线程安全地弹结果
            # on_partial：流式生成中整行地先上屏，不用干等整段
            self.subtitle_window.on_lookup = (
                lambda word, ctx, request_id=None: self.translator.lookup_word(
                    word, ctx, self.subtitle_window.show_lookup_result,
                    on_partial=self.subtitle_window.show_lookup_partial,
                    request_id=request_id))
            # 🤖 背景总结 / 点词深度解释：走独立的 _analysis_executor，
            # 既不占翻译队列，也不挡查词
            self.subtitle_window.on_ai_analysis = (
                lambda text, cb: self.translator.analyze_background(text, cb))
            self.subtitle_window.on_deep_explain = (
                lambda sentence, cb: self.translator.deep_explain(sentence, cb))

            # 音频捕获（设备名/设备切换提示直接上悬浮窗）。构造要开WASAPI流，
            # 有几百毫秒窗口，之后再查一次 _closing 才真正启动采集/热键
            from realtime_subtitle.capture.audio_capture import AudioCapture

            audio_capture = AudioCapture(
                callback=self.on_audio_received,
                on_status=self.subtitle_window.show_status)
            if self._closing:
                audio_capture.stop()
                return
            self.audio_capture = audio_capture

            print("✅ 所有组件初始化完成")
            self.running = True
            self.audio_capture.start()
            # 模型加载完才开始盯内存：加载 Whisper + 预热翻译模型时压力本来就会
            # 冲高，算进去的话一启动就被降档（M2 e2e 实测：启动即 4b→2b，之后
            # 要 5 分钟以上才升回，这段时间中文明显更差）
            self._start_memory_tier_loop()
            self._print_usage()
            if sys.platform != "darwin":
                self._setup_hotkey()
        except Exception as e:
            print(f"❌ 初始化失败: {e}")
            import traceback
            traceback.print_exc()
            # 失败也要把已建的线程池/模型收掉：不收的话 Whisper 白占显存、
            # 非daemon线程拖住进程退出（shutdown 重入无害，stop()再调也没事）
            if translator is not None:
                try:
                    translator.shutdown()
                except Exception:
                    pass
                self.translator = None
                self.subtitle_window.on_lookup = None
                self.subtitle_window.on_ai_analysis = None
                self.subtitle_window.on_deep_explain = None
            # status 5秒自清，失败信息要用live行持久显示（不会有识别来覆盖它）
            self.subtitle_window.update_live(
                f"❌ 启动失败: {e}", "详见 subtitle.err.log / subtitle.log，可发给AI排查；点❌退出")
            return
        finally:
            done.set()
        self.subtitle_window.show_status("✅ 字幕已就绪，播放德语音视频即可")
    
    def _print_header(self):
        """打印启动标题"""
        print("\n" + "=" * 60)
        # 版本号读 version.py（单一真相源）——以前这里写死 "v2.0"，
        # 和 git tag 对不上，用户报 bug 只能贴 commit hash
        print(" " * 15 + f"🎬 实时字幕软件 {version_string()}")
        print(" " * 12 + "基于 Whisper + Ollama")
        print("=" * 60)
        print()
    
    def on_audio_received(self, audio_data, capture_time):
        """
        音频接收回调函数
        将翻译任务提交到线程池，避免阻塞音频处理线程

        Args:
            audio_data: numpy array, float32, shape=(n_samples,)
            capture_time: 采集端提交该段音频的时刻（翻译端算真实音频间隔用）
        """
        if not self.running:
            return

        # 每次处理音频块
        if config.SHOW_PERFORMANCE:
            audio_duration = len(audio_data) / config.SAMPLE_RATE
            print(f"\n📦 处理音频块: {audio_duration:.2f}秒")

        # 音频进translator收件箱：识别线程忙时块在收件箱里攒着，
        # 醒来一口气全塞进缓冲只识别一遍——GPU被游戏抢走时字幕只滞后不丢词
        self.translator.enqueue_audio(audio_data, capture_time)
        # 本机识别始终在跑。远端是额外的一路，两个开关都关时 remote 是 None，这里什么都不做。
        remote = self.remote
        if remote is not None:
            try:
                remote.feed_audio(audio_data)
            except Exception as exc:
                print(f"⚠️  远端音频投递失败: {type(exc).__name__}")

    def _on_remote_prefs_changed(self):
        # 翻译器还没建好时，开关已经写进文件，等 _load_models 再接。
        if self.translator is None:
            return
        self._attach_remote()

    def _attach_remote(self):
        from realtime_subtitle.ui.settings_window import cloud_active, node_active

        cloud_on = cloud_active()
        node_on = node_active()
        if not cloud_on and not node_on:
            self._close_remote()
            if self.translator is not None:
                self.translator.on_pair = self.subtitle_window.add_pair
            self._refresh_panel()
            return
        # 开关组合一变就拆掉重建：关掉的那一路不能把客户端继续留在内存里。
        self._close_remote()
        try:
            self.remote = open_remote_bridge(
                cloud_on=cloud_on,
                node_on=node_on,
                translator=self.translator,
                ui_show=self._ui_show_line,
                ui_drop=self._ui_drop_keys,
                ui_update_dst=self._ui_update_dst,
                ui_status=self.subtitle_window.show_status,
            )
            if self.translator is not None and self.remote is not None:
                self.translator.on_pair = self._on_local_pair
        except Exception as exc:
            # 远端接不上不能把字幕一起拖死：本机识别照常。这里不是路由回退策略。
            print(f"⚠️  远端识别没有接上，继续只用本机: {type(exc).__name__}")
            self.remote = None
            if self.translator is not None:
                self.translator.on_pair = self.subtitle_window.add_pair
        self._refresh_panel()

    def _close_remote(self):
        # __new__ 跳过 __init__ 的测试也会进 stop()，那时还没有 remote。
        remote = getattr(self, "remote", None)
        self.remote = None
        if remote is None:
            return
        try:
            remote.close()
        except Exception as exc:
            print(f"⚠️  关闭远端失败: {type(exc).__name__}")

    def _on_local_pair(self, src, tgt, t0, t1, key, revision):
        remote = self.remote
        if remote is None:
            self.subtitle_window.add_pair(src, tgt, t0, t1, key, revision)
            return
        remote.on_local_pair(src, tgt, t0, t1, key, revision)

    def _on_language_applied(self, src, tgt, revision=0):
        self.subtitle_window.notify_language_applied(src, tgt, revision)
        remote = getattr(self, "remote", None)
        if remote is not None:
            try:
                remote.on_language_applied(src, tgt, revision)
            except Exception as exc:
                print(f"⚠️  远端语言代切换失败: {type(exc).__name__}")

    def _ui_invoke(self, fn):
        caller = getattr(self, "_gui_caller", None)
        if caller is None:
            fn()
            return
        caller.call.emit(fn)

    def _refresh_panel(self):
        self._ui_invoke(self.subtitle_window.settings_window.refresh_from_config)

    def _on_status_refresh_panel(self, _text):
        # 悬浮窗那条状态 5 秒会淡掉。面板上的「当前路由」靠这次刷新留下来。
        if getattr(self, "_closing", False):
            return
        window = getattr(self, "subtitle_window", None)
        if window is None:
            return
        window.settings_window.refresh_from_config()

    def _route_status_text(self):
        remote = self.remote
        if remote is not None:
            try:
                return remote.route_label()
            except Exception:
                pass
        from realtime_subtitle.ui.settings_window import static_route_label
        try:
            return static_route_label()
        except Exception:
            from realtime_subtitle.ui import design_tokens
            return design_tokens.TEXT_STATUS_LOCAL

    def _ui_show_line(self, src, dst, t0, t1, key, source, revision=0):
        def _go():
            window = self.subtitle_window
            rev = _coerce_revision(revision)
            if source != "local":
                # 远端句没有自己的代数。用窗口当前代数和桥上见过的最新代数里更大的那个，
                # 语言切过去之后才不会被 _is_stale_language 当成 0 丢掉。
                rev = max(rev, _coerce_revision(getattr(window, "language_revision", 0)))
            window.add_pair(src or "", dst or "", t0, t1, key, rev)
            if source == "local":
                return
            _reinsert_pair_by_t0(window, key, source)
        self._ui_invoke(_go)

    def _ui_drop_keys(self, keys):
        drop = set(keys or [])
        if not drop:
            return

        def _go():
            window = self.subtitle_window
            window.sentence_pairs = [
                item for item in window.sentence_pairs
                if not (len(item) >= 7 and item[6] in drop)
            ]
            # 远端句替换本机句：历史窗/电视窗/影院条也按同一句 id 去掉最初那条
            # 本机句，否则它们会保留旧内容、随后又多出一条远端句（同一内容两遍）。
            # 缺这些属性的测试替身用 getattr 兜底。
            for name in ("history_window", "tv_window", "cinema_bar"):
                view = getattr(window, name, None)
                drop_fn = getattr(view, "drop_keys", None)
                if drop_fn is not None:
                    drop_fn(drop)
            window._render()
        self._ui_invoke(_go)

    def _ui_update_dst(self, key, dst):
        self._ui_invoke(lambda: self.subtitle_window.update_dst(key, dst))

    def _toggle_pause(self):
        """暂停/继续（和 暂停继续字幕.bat 完全等价：切换.paused标记文件）"""
        from realtime_subtitle.capture.audio_capture import PAUSE_FLAG_FILE

        try:
            if os.path.exists(PAUSE_FLAG_FILE):
                os.remove(PAUSE_FLAG_FILE)
                self.subtitle_window.show_status("▶️ 已继续识别翻译")
                print("▶️ [热键] 继续识别与翻译")
            else:
                open(PAUSE_FLAG_FILE, "w").close()
                self.subtitle_window.show_status("⏸️ 字幕已暂停（Ctrl+Alt+P 继续）")
                print("⏸️ [热键] 已暂停识别与翻译")
        except OSError as e:
            print(f"⚠️  切换暂停状态失败: {e}")

    def _switch_language(self):
        """Ctrl+Alt+L：在 LANGUAGE_PAIRS 里循环下一个**语言对**。

        ☠️ 循环的是对不是单个源语言：只切源语言的话，切到中文会变成
        "中文→中文"（识别对了但 prompt 还在要求输出中文）。见 config 里
        LANGUAGE_PAIRS 的注释。
        """
        from realtime_subtitle.translate.translator_queue import (
            language_pairs, language_name,
        )
        pairs = language_pairs()
        try:
            idx = [s for s, _ in pairs].index(config.SOURCE_LANGUAGE)
        except ValueError:
            idx = -1  # 当前语言不在列表里（手改过config），切到列表第一个
        new_lang, new_target = pairs[(idx + 1) % len(pairs)]
        name = language_name(new_lang)
        tname = language_name(new_target)
        # "清上下文+改语言"作为一个任务在识别线程内串行执行。之前是这里
        # 先改config再排清理任务——窗口期内会拿新语言参数识别缓冲里的
        # 旧语言音频，蹦出乱词
        self.translator.request_switch_language(new_lang, source="manual")
        self.subtitle_window.show_status(f"🌐 切换中: {name} → {tname}…")
        print(f"🌐 [热键] 请求切换语言对: {name} → {tname}")

    def _request_language_pair(self, src, tgt=None):
        """⚙️ 面板点语言对：复用 request_switch_language，源和目标一起切。

        tgt 是面板按钮上那一对的目标语言。必须显式传下去——配了 zh→en 和
        zh→de 两条时，只传源语言的话后台只能查到第一条，第二个按钮永远切不上。

        ☠️ 这里不许有"等于 config.SOURCE_LANGUAGE 就直接 return"的快捷路径。
        config 只在后台真正应用之后才变，而应用要等 ASR 的批边界：
        「德语 → 点英语 → 再点德语」里第二次点击那一刻 config 还是 de，
        于是它被当成"没变化"扔掉，最后停在英语——用户最后一次选择失效。
        所有点击一律提交给后台，由它在消费时比较实际语言对（见
        _apply_pending_lang_switch：无变化时只刷 UI，不清音频）。
        """
        from realtime_subtitle.translate.translator_queue import (
            language_name, target_for,
        )
        target = tgt or target_for(src)
        if self.translator is None:
            config.SOURCE_LANGUAGE = src
            config.TARGET_LANGUAGE = target
            self.subtitle_window.settings_window.refresh_from_config()
            return
        name = language_name(src)
        tname = language_name(target)
        self.translator.request_switch_language(src, source="manual", target=target)
        self.subtitle_window.show_status(f"🌐 切换中: {name} → {tname}…")

    def _apply_mode(self, name):
        """唯一的"应用模式"入口（⚙️面板按钮和 Ctrl+Alt+G 都走这里）。

        纯 setattr 赋值，不碰任何 Qt widget，所以热键线程和主线程都能直接调
        （widget 的同步由下面那句 notify_mode_applied 走信号 marshal 到主线程）。
        模式里的值全是各循环每轮现读 config 的，改属性即热生效：提交节奏下一块
        生效、beam 下一次识别生效、语域和模型下一次请求生效。

        name=None 表示"用户手动拨了滑块，现在是自定义"——只更新记账和指示器，
        一个 config 值都不动。未知模式名返回 False 且不改任何东西。
        """
        if name is None:
            self._current_mode = None
            self.subtitle_window.notify_mode_applied(None)
            return True
        presets = getattr(config, "PRESETS", {}) or {}
        if name not in presets:
            print(f"⚠️ 未知模式: {name}")
            return False
        for key, val in presets[name].items():
            setattr(config, key, val)
        # 「性能」换轻量翻译模型；其它模式切回启动时的基线（不是写死的值——
        # 不同机器 config_local 里的 OLLAMA_MODEL 不一样，写死会覆盖显存分档）
        target_model = (getattr(config, "GAME_MODE_OLLAMA_MODEL", None) if name == "性能"
                        else self._baseline_ollama_model)
        self._switch_ollama_model(target_model)
        self._current_mode = name
        self.subtitle_window.notify_mode_applied(name)
        self.subtitle_window.show_status(f"{_MODE_ICON.get(name, '⚙️')} 已切换到「{name}」模式")
        print(f"🎬 [模式] {name}: 节奏{config.CHUNK_SUBMIT_SECONDS}s "
              f"beam{config.WHISPER_BEAM_SIZE} "
              f"草稿{'开' if config.DRAFT_TRANSLATION else '关'} "
              f"语域{config.TRANSLATION_STYLE} 模型{config.OLLAMA_MODEL}")
        return True

    def _switch_ollama_model(self, target_model):
        if target_model and target_model != config.OLLAMA_MODEL:
            old_model = config.OLLAMA_MODEL
            config.OLLAMA_MODEL = target_model
            # ☠️ 模型还在后台加载时（窗口先显示的那十几秒）translator 是 None，
            # 这时只改 config 就够了：翻译器构造后自然用新名字，退出时的
            # _unload_our_models 也会把两个模型名都覆盖到，不会漏显存
            if self.translator is not None:
                self.translator.request_warm_model(old_model=old_model, new_model=target_model)

    def _on_memory_tier(self, name):
        bits, model = self._memory_tier_by_name.get(name, (None, None))
        if bits is None:
            print(f"⚠️  未知内存档位: {name}")
            return
        old_bits = self._baseline_whisper_bits
        old_model = self._baseline_ollama_model
        self._baseline_whisper_bits = bits
        if model is not None:
            self._baseline_ollama_model = model
        if model is not None and self._current_mode != "性能":
            self._switch_ollama_model(model)
        if bits != old_bits and self.translator is not None:
            self.translator.request_whisper_bits(bits)
        # 内存监控在模型加载完成后才启动；这里保留 None 分支不补队列，避免
        # 加载期虚构一条从未被 TierGovernor 观测到的精度切换。
        parts = []
        if bits != old_bits:
            desc = "fp16" if bits == 16 else f"{bits}bit"
            if bits < old_bits:
                suffix = "（准确度不变）" if bits == 8 else ""
                parts.append(f"识别精度降到 {desc}{suffix}")
            else:
                parts.append(f"识别精度恢复 {desc}")
        all_tiers = list(getattr(config, "AUTO_TIERS", []))
        old_tier_for_index = (old_bits, None) if model is None else (old_bits, old_model)
        new_tier_for_index = (bits, model)
        tight = bits < old_bits
        try:
            old_index = all_tiers.index(old_tier_for_index)
            new_index = all_tiers.index(new_tier_for_index)
        except ValueError:
            old_index = new_index = 0
        else:
            tight = new_index > old_index
        if model is not None and model != old_model:
            parts.append(f"翻译模型{'降到' if new_index > old_index else '恢复'} {model}")
        if bits >= old_bits and (model is None or model == old_model):
            parts.append(f"恢复 {_tier_name((bits, model))}")
        msg = f"🧠 内存{'紧张' if tight else '充足'}：" + "，".join(parts)
        self.subtitle_window.show_status(msg)
        print(msg)

    def _start_memory_tier_loop(self):
        governor = self._build_memory_governor()
        if governor is None:
            return
        import threading
        self._memory_tier_stop = threading.Event()

        def _loop():
            poll = float(getattr(config, "AUTO_TIER_POLL_SEC", 2.0))
            while not self._memory_tier_stop.wait(poll):
                try:
                    model = governor.observe(read_memory_snapshot(), time.monotonic())
                    if model is not None:
                        self._on_memory_tier(model)
                except Exception as e:
                    if not self._memory_tier_error_printed:
                        self._memory_tier_error_printed = True
                        print(f"⚠️  内存自动分档监控失败，已暂停日志刷屏: {e}")

        self._memory_tier_thread = threading.Thread(target=_loop, daemon=True, name="MemoryTierLoop")
        self._memory_tier_thread.start()

    def _toggle_perf_hotkey(self):
        """Ctrl+Alt+G：跳到「性能」模式；已经在性能模式则跳回进入前那个模式
        （没记录过就回默认「直播」）。"""
        if self._current_mode == "性能":
            target = self._mode_before_perf or "直播"
        else:
            self._mode_before_perf = self._current_mode or "直播"
            target = "性能"
        self._apply_mode(target)

    def _setup_hotkey(self):
        """全局快捷键：Windows 原生 RegisterHotKey。

        ☠️ 之前用 keyboard 库的低级键盘钩子：keyboard.send 注入的测试事件
        能触发，但用户的物理键盘（德语QWERTZ布局系统）按了完全没反应——
        keyboard 库对非美式布局的组合键匹配不可靠是它的老毛病。
        RegisterHotKey 按虚拟键码在系统层注册，与键盘布局无关，游戏里
        也有效（仅管理员权限窗口在前台时无效——Windows UIPI 限制）。
        注册和消息循环必须在同一个线程（热键投递到注册线程的消息队列）。
        回调跑在热键线程里：文件操作+Qt信号都线程安全。
        """
        if sys.platform not in ("win32", "darwin"):
            print("当前平台暂不支持全局快捷键，请用窗口按钮/⚙️面板")
            return
        callbacks = {
            "Ctrl+Alt+P": self._toggle_pause,
            "Ctrl+Alt+L": self._switch_language,
            "Ctrl+Alt+M": self.subtitle_window.toggle_click_through,
            "Ctrl+Alt+G": self._toggle_perf_hotkey,
            "Ctrl+Alt+C": self.subtitle_window.toggle_cinema,
        }
        if sys.platform == "darwin":
            # Carbon 热键必须在主线程注册，所以比 Windows 早（构造期就注册）；
            # 模型加载完（running）之前按键一律忽略，和 Windows"加载完才注册"等价——
            # 否则加载期间按 Ctrl+Alt+L 会撞上还没建好的 translator
            from realtime_subtitle.ui import macos_hotkeys
            gated = {label: (lambda f=f: f() if self.running else None)
                     for label, f in callbacks.items()}
            registered = macos_hotkeys.register(gated)
            if registered:
                print(f"⌨️  全局快捷键已注册(系统级): {', '.join(registered)}")
            return
        import threading
        import ctypes
        from ctypes import wintypes

        MOD_ALT, MOD_CONTROL, MOD_NOREPEAT = 0x1, 0x2, 0x4000
        WM_HOTKEY = 0x0312
        handlers = {
            1: ("Ctrl+Alt+P", ord('P'), callbacks["Ctrl+Alt+P"]),
            2: ("Ctrl+Alt+L", ord('L'), callbacks["Ctrl+Alt+L"]),
            3: ("Ctrl+Alt+M", ord('M'), callbacks["Ctrl+Alt+M"]),
            4: ("Ctrl+Alt+G", ord('G'), callbacks["Ctrl+Alt+G"]),
            5: ("Ctrl+Alt+C", ord('C'), callbacks["Ctrl+Alt+C"]),
        }

        def hotkey_loop():
            user32 = ctypes.windll.user32
            # 记下线程id，stop()里PostThreadMessage(WM_QUIT)让循环优雅退出
            self._hotkey_tid = ctypes.windll.kernel32.GetCurrentThreadId()
            registered = []
            for hid, (label, vk, _) in handlers.items():
                if user32.RegisterHotKey(None, hid, MOD_CONTROL | MOD_ALT | MOD_NOREPEAT, vk):
                    registered.append(hid)
                else:
                    print(f"⚠️  快捷键 {label} 注册失败（可能被其它程序占用）")
            if not registered:
                return
            print(f"⌨️  全局快捷键已注册(系统级): "
                  f"{', '.join(handlers[h][0] for h in registered)}")
            msg = wintypes.MSG()
            while True:
                r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if r == 0:
                    break  # WM_QUIT（stop()里PostThreadMessage发来）
                if r == -1:
                    print("⚠️  热键消息循环出错，退出（GetMessage返回-1）")
                    break  # 出错返回-1：不能当"有消息"继续转，会死循环
                if msg.message == WM_HOTKEY and msg.wParam in handlers:
                    try:
                        handlers[msg.wParam][2]()
                    except Exception as e:
                        print(f"⚠️  快捷键处理错误: {e}")
            for hid in registered:
                user32.UnregisterHotKey(None, hid)

        self._hotkey_tid = None
        threading.Thread(target=hotkey_loop, name="HotkeyLoop", daemon=True).start()

    def _flush_check(self):
        """定时兜底：一段话说完后没有新音频，识别不会再被触发，
        队列里攒着的未翻译尾句由这里冲出去（忙时不插队，判断在translator里）"""
        if not self.running:
            return
        self.translator.request_flush()

    def _stop_flag_check(self):
        """停止脚本写 .stop 文件后，在此优雅退出（关线程池/模型，再退 Qt）。

        不看 self.running：模型还在后台加载时（running 还没置 True）
        停止脚本也应该能把程序关掉，而不是干等 3 秒被强杀"""
        if self._closing:
            return
        if not os.path.exists(STOP_FLAG_FILE):
            return
        try:
            os.remove(STOP_FLAG_FILE)
        except OSError:
            pass
        print("\n⏹️  收到停止请求，正在优雅退出...")
        # quit 会触发 aboutToQuit → stop()；随后事件循环结束
        self.subtitle_window.app.quit()

    def start(self):
        """启动应用：主线程立刻进 Qt 事件循环（窗口马上可见），
        模型/音频在后台线程加载完自行启动（_load_models）"""
        print("\n🚀 正在启动应用...")
        self._closing = False
        # 启动前清掉残留停止标记，避免立刻又退
        try:
            if os.path.exists(STOP_FLAG_FILE):
                os.remove(STOP_FLAG_FILE)
        except OSError:
            pass

        # 后台加载模型（daemon：用户加载期间退出时不挡进程结束）
        import threading
        threading.Thread(target=self._load_models, daemon=True, name="ModelLoader").start()

        # 尾句兜底定时器（跑在Qt主线程）。0.5秒一次：除了收尾 flush，它还负责
        # 放行"被扣留等下文"的句尾（config.SENTENCE_HOLD_SEC），粒度太粗会让
        # 一段话的最后一句多等半秒。忙的时候 request_flush 只是一次 early-return
        self._flush_timer = QTimer()
        self._flush_timer.timeout.connect(self._flush_check)
        self._flush_timer.start(500)

        # 停止标记轮询（0.5s：停止.bat 体感更快）
        self._stop_timer = QTimer()
        self._stop_timer.timeout.connect(self._stop_flag_check)
        self._stop_timer.start(500)

        # 运行UI事件循环（阻塞，直到窗口关闭）
        try:
            # 连接退出信号
            self.subtitle_window.app.aboutToQuit.connect(self.stop)
            self.subtitle_window.run()
        except KeyboardInterrupt:
            print("\n⚠️  收到中断信号")
        finally:
            self.stop()
    
    def stop(self):
        """停止应用（模型加载中途退出时 translator/audio 可能还是 None）"""
        if getattr(self, '_closing', False):
            return
        self._closing = True

        print("\n⏹️  正在停止应用...")
        self.running = False

        # 停掉定时器，避免向关闭中的线程池提交任务
        if hasattr(self, '_flush_timer'):
            self._flush_timer.stop()
        if hasattr(self, '_stop_timer'):
            self._stop_timer.stop()
        if getattr(self, '_memory_tier_stop', None) is not None:
            self._memory_tier_stop.set()
        if getattr(self, '_memory_tier_thread', None) is not None:
            self._memory_tier_thread.join(timeout=1)

        # 让热键线程退出消息循环并注销热键（WM_QUIT = 0x0012）
        if sys.platform == "win32" and getattr(self, '_hotkey_tid', None):
            import ctypes
            ctypes.windll.user32.PostThreadMessageW(self._hotkey_tid, 0x0012, 0, 0)
        elif sys.platform == "darwin":
            from realtime_subtitle.ui import macos_hotkeys
            macos_hotkeys.unregister_all()

        # 先停止音频捕获，避免向已关闭的线程池提交新任务
        if self.audio_capture is not None:
            try:
                self.audio_capture.stop()
            except Exception as e:
                print(f"⚠️  停止音频捕获时出错: {e}")

        # 远端翻译还在用 translator，必须先取消在飞请求再关识别线程。
        self._close_remote()

        # 优雅地关闭识别/翻译线程（translator内部先ASR后翻译）
        if self.translator is not None:
            print("   - 正在关闭识别与翻译线程...")
            self.translator.shutdown()
        else:
            # 加载中途退出：WhisperQueueTranslator() 还没返回，self.translator
            # 仍是 None，但后台预热可能已经把模型留在 Ollama 里。
            from realtime_subtitle.translate.translator_queue import release_startup_warm
            print("   - 正在结束加载中的翻译模型预热...")
            release_startup_warm()

        # 清掉停止标记（若仍在）
        try:
            if os.path.exists(STOP_FLAG_FILE):
                os.remove(STOP_FLAG_FILE)
        except OSError:
            pass

        # 清掉 pid 文件：stop脚本会删，但❌按钮/.stop标记/Ctrl+C退出不经过
        # stop脚本——残留的旧PID被系统回收复用给别的进程后，start脚本会误判
        # "已经在运行中"拒绝启动（2026-07-17 实测撞上）。注意 pid 文件记的是
        # venv 启动器存根的 PID（我们的父进程），不是 os.getpid()，所以只能
        # 按路径删、不做内容比对
        try:
            pid_file = repo_path("subtitle.pid")
            if os.path.exists(pid_file):
                os.remove(pid_file)
        except OSError:
            pass

        print("👋 应用已关闭，再见！")
    
    def _print_usage(self):
        """打印使用说明"""
        print("\n" + "=" * 60)
        print("✅ 应用已成功启动！")
        print("=" * 60)
        print("\n💡 使用提示：")
        print("   1. 字幕窗口会显示在屏幕底部（可拖动）")
        print("   2. 播放任何视频/音频，系统会自动捕获并翻译")
        print("   3. 支持YouTube、本地视频、音乐播放器等")
        print("   4. 点击 ➖ 按钮可最小化字幕")
        print("   5. 点击 ⚙️ 按钮可调节参数")
        print("   6. 点击 ❌ 按钮可退出程序")
        print("   7. 或按 Ctrl+C 中断程序")
        print("\n⚙️  当前配置：")
        from realtime_subtitle.asr.backends import describe_whisper_backend
        print(f"   - 识别: {describe_whisper_backend()}")
        print(f"   - 处理模式: local agreement 增量识别 (每{config.CHUNK_SUBMIT_SECONDS}秒一块, 缓冲上限{config.BUFFER_TRIM_SEC:.0f}秒)")
        print(f"   - 收尾静音: {config.IDLE_FLUSH_SEC}秒")
        print(f"   - 翻译: Qwen + Whisper (Ollama {config.OLLAMA_MODEL})")
        print(f"   - 源语言: {config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)}")
        print(f"   - 路由: {self._route_status_text()}")
        from realtime_subtitle.translate.translator_queue import log_language_startup
        log_language_startup()
        print(f"   - 翻译语域: {getattr(config, 'TRANSLATION_STYLE', '（默认）')}")
        print(f"   - 快捷键: Ctrl+Alt+P 暂停/继续, Ctrl+Alt+L 切换源语言,")
        print(f"             Ctrl+Alt+M 鼠标穿透, Ctrl+Alt+G 跳「⚡性能」模式,")
        print(f"             Ctrl+Alt+C 影院字幕条（全屏看剧）")
        print("\n" + "=" * 60)
        print("🎉 开始享受实时字幕吧！")
        print("=" * 60 + "\n")

def main():
    """主函数"""
    try:
        app = SubtitleApp()
        app.start()
    except KeyboardInterrupt:
        print("\n👋 用户中断，程序退出")
    except Exception as e:
        print(f"\n❌ 程序异常: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)

if __name__ == "__main__":
    main()
