"""
Whisper 增量识别 + LLM 翻译模块（2026-07-06 重写）

识别：streaming_asr.OnlineASRProcessor（local agreement 词级提交，根治了旧管线
"滑动窗口整窗重识别+文本相似度去重"的重复碎片问题）。
翻译：独立单线程 executor 跑 Ollama——Ollama 一次1-10秒的请求不再堵住识别节奏，
这也是"德语先行显示"的前提：识别提交的德语立即上屏，中文随后跟上。

显示模型（通过回调驱动 UI，由 main.py 接线）：
- on_display(committed_live, unstable): live行 = 已提交未翻译的德语 + 灰色未稳定尾部
- on_pair(german, chinese): 一段德语翻译完成，变成历史句对
"""
import warnings
import logging
import json
import sys
import os
import time
import re
import queue
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque, OrderedDict

import requests

from streaming_asr import OnlineASRProcessor
import config

# 过滤所有警告信息
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)

# 句子结束符：只认 .!?（旧管线按逗号切句是碎句/上下文错乱的来源之一）
_SENTENCE_END = re.compile(r'(.*?[.!?…]["»«\']?)(\s|$)', re.DOTALL)

_PUNCT_STRIP = " \t.!?…,;:–—\"'«»„“”"


def _interjection_lookup(sentence):
    """≤3词的高频感叹词直接查词典，命中就不打Ollama。
    游戏/聊天场景实测21%的字幕是"Ja." "Was?" "Whoa!"这类，
    每条单独一次Ollama请求纯浪费GPU（还和Whisper抢卡）。"""
    if config.SOURCE_LANGUAGE != "de":
        return None  # 词典是德语的，其它源语言不启用
    key = sentence.strip(_PUNCT_STRIP).lower()
    if not key or len(key.split()) > 3:
        return None
    return getattr(config, "INTERJECTION_TRANSLATIONS", {}).get(key)


def _squash_repeats(sentences, keep_words=3, keep_sents=2):
    """压缩Whisper复读伪影（游戏噪音实测提交过"Get. Get. Get. Get. Get. Get."）：
    句内同词连续超过 keep_words 次收敛；一批里连续相同句子超过 keep_sents 条丢弃。
    真人口语的"ja, ja"重复不受影响（阈值3已经很宽）。"""
    out = []
    for s in sentences:
        words = s.split()
        squashed = []
        run = 0
        for w in words:
            if squashed and w.lower() == squashed[-1].lower():
                run += 1
                if run >= keep_words:
                    continue
            else:
                run = 0
            squashed.append(w)
        s = " ".join(squashed)
        if len(out) >= keep_sents and all(x == s for x in out[-keep_sents:]):
            continue
        out.append(s)
    return out

_WhisperModel = None  # set by _ensure_ml_deps()


def _ensure_ml_deps():
    """Load torch + faster-whisper only when the translator is constructed.

    Keeps lightweight imports (e.g. _SENTENCE_END for unit tests) free of
    torch/ctranslate2. On Windows, torch must load before PATH cublas injection
    and before ctranslate2 (via faster-whisper), or c10.dll can fail (WinError 1114).
    """
    global _WhisperModel
    if _WhisperModel is not None:
        return _WhisperModel

    # torch本身在这个项目里没用（faster-whisper走ctranslate2），但venv里的
    # ctranslate2版本会在内部无条件import torch。必须在下面往PATH里注入
    # nvidia cublas目录【之前】加载torch——注入后再首次加载torch出现过
    # c10.dll初始化失败(WinError 1114)，先加载则一直稳定
    import torch  # noqa: F401

    # ctranslate2 在 Windows 上通过 LoadLibraryA("cublas64_12.dll") 按名加载，
    # 只认 PATH，不认 os.add_dll_directory 注册的路径，所以要直接塞进 PATH
    if sys.platform == "win32":
        try:
            import nvidia.cublas
            os.environ["PATH"] = (
                os.path.join(list(nvidia.cublas.__path__)[0], "bin")
                + os.pathsep
                + os.environ["PATH"]
            )
        except ImportError:
            pass

    from faster_whisper import WhisperModel
    _WhisperModel = WhisperModel
    return _WhisperModel


class WhisperQueueTranslator:
    """local agreement 增量识别 + 异步 Ollama 翻译"""

    def __init__(self):
        """初始化翻译器"""
        print("🔄 正在加载 Faster-Whisper 模型...")
        print(f"   模型: {config.WHISPER_MODEL}")
        print(f"   计算类型: {config.WHISPER_COMPUTE_TYPE}")

        start_time = time.time()
        WhisperModel = _ensure_ml_deps()

        try:
            self.model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE
            )
            self.processor = OnlineASRProcessor(self.model)

            # committed 但还没凑成完整句子的德语残句
            self.pending_text = ""
            # 上次识别的未稳定尾部（重绘live行用）
            self._last_unstable = ""

            self.last_audio_time = time.time()  # flush_pending靠它判断"多久没新音频"
            self.last_capture_end = None  # 上一段音频在采集端结束的时刻（算真实间隔用）

            # ASR收件箱：采集线程只管往里放，识别线程每次醒来把攒下的块
            # 一口气全塞进缓冲、只识别一遍。GPU被游戏/共享抢走时字幕只是
            # 滞后几秒，不丢词；GPU恢复后自动追上（旧方案是每块一个任务，
            # 积压满8个就丢块——聊天场景实测丢过）
            self._asr_lock = Lock()
            self._audio_inbox = []      # [(audio, capture_time)]
            self._asr_scheduled = False
            self._asr_executor = ThreadPoolExecutor(max_workers=1)

            # 翻译队列：ASR线程往里放完整句子，翻译worker每次醒来把积压的全部
            # 合并成一次Ollama请求（自带积压治理，说话快时翻译永远追得上）
            self._tx_lock = Lock()
            self._tx_queue = []      # 已入队待翻译
            self._tx_inflight = []   # 正在翻译中
            self._tx_epoch = 0       # 切语言时+1：在飞的翻译完成时代数不符就丢弃
            self.closing = False     # shutdown置True：所有worker出口不再回调UI
            self._tx_executor = ThreadPoolExecutor(max_workers=1)
            # 点词查词单独一个worker：查词典不该排在字幕翻译后面等
            self._lookup_executor = ThreadPoolExecutor(max_workers=1)
            # 查词 LRU：重复点同一词零 Ollama 成本（精听高频）；
            # OrderedDict + move_to_end = 真 LRU，锁保护 UI 线程与 worker 并发
            self._lookup_cache = OrderedDict()  # (word_lower, lang) -> text
            self._lookup_cache_lock = Lock()
            self._LOOKUP_CACHE_MAX = int(getattr(config, "LOOKUP_CACHE_MAX", 200))

            # 最近已翻译的德语句子，作为翻译上下文
            self.context_history = deque(maxlen=6)

            # UI回调，由main.py接线（都必须线程安全——SubtitleWindow用Qt信号保证）
            self.on_display = None  # (committed_live, unstable) -> None
            self.on_pair = None     # (german, chinese) -> None
            self.on_draft = None    # (chinese_draft) -> None 残句的草稿中文
            self.on_status = None   # (text) -> None 状态提示（如Ollama挂了）
            self._ollama_down_notified = 0.0  # 上次提示"翻译服务未运行"的时间（节流）

            # 分钟级性能概况（SHOW_PERFORMANCE=False 后仅剩的观测手段）
            self._stats_lock = Lock()
            self._stats_t0 = time.time()
            self._stat_asr = []       # 每轮识别耗时
            self._stat_tx = []        # 每次Ollama翻译耗时
            self._stat_buf_max = 0.0  # 音频缓冲峰值（秒）
            self._stat_merge = 0      # 合并多块处理的轮数（GPU落后的信号）
            self._stat_draft = 0      # 草稿翻译次数
            self._stat_dict = 0       # 感叹词词典直译次数（省下的Ollama请求）

            # 草稿翻译节流状态（残句还没凑成完整句子时先出一版灰色中文）
            self._draft_last_time = 0.0
            self._draft_last_text = ""

            # 复用的HTTP会话（翻译/查词各一个：requests.Session跨线程
            # 并发使用不保证安全，翻译线程和查词线程各自独享）
            self.ollama_session = requests.Session()
            self.lookup_session = requests.Session()

            # 字幕记录（原文+译文+时间戳，每天一个文件）
            self._transcript_ok = bool(getattr(config, "SAVE_TRANSCRIPT", False))
            if self._transcript_ok:
                self._transcript_dir = os.path.join(
                    os.path.dirname(os.path.abspath(__file__)), config.TRANSCRIPT_DIR)
                try:
                    os.makedirs(self._transcript_dir, exist_ok=True)
                    print(f"📝 字幕记录已开启: {config.TRANSCRIPT_DIR}\\日期.txt")
                except OSError as e:
                    print(f"⚠️  字幕记录目录创建失败，记录功能关闭: {e}")
                    self._transcript_ok = False

            # 启动时检查Ollama是否可达（不可达只警告，不中断启动）
            try:
                self.ollama_session.get(f"{config.OLLAMA_BASE_URL}/api/version", timeout=2)
                print(f"✅ Ollama 连接正常 ({config.OLLAMA_MODEL})")
            except requests.RequestException:
                print(f"⚠️  无法连接 Ollama ({config.OLLAMA_BASE_URL})，字幕将只显示德语原文")
                print(f"   请确认 Ollama 已启动: ollama serve")

            elapsed = time.time() - start_time
            print(f"✅ Whisper 模型加载完成！({elapsed:.1f}秒)")
            print(f"✅ local agreement 增量识别已启用（缓冲上限 {config.BUFFER_TRIM_SEC:.0f}秒）")
            print(f"   设备: {config.WHISPER_DEVICE.upper()}")

        except Exception as e:
            print(f"❌ 模型加载失败: {e}")
            raise

    # ------------------------------------------------------------------
    # 字幕记录
    # ------------------------------------------------------------------
    def _save_transcript(self, source_text, translation):
        """把一条字幕追加到当天的记录文件（失败一次就关闭，不刷屏）"""
        if not self._transcript_ok:
            return
        try:
            path = os.path.join(self._transcript_dir, time.strftime("%Y-%m-%d") + ".txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {source_text}\n")
                f.write(f"           {translation}\n\n")
        except OSError as e:
            print(f"⚠️  字幕记录写入失败，记录功能关闭: {e}")
            self._transcript_ok = False

    # ------------------------------------------------------------------
    # 上下文管理
    # ------------------------------------------------------------------
    def clear_context(self):
        """清空识别/翻译上下文（切换源语言时调用，跑在ASR线程池里，与识别串行）。

        注意翻译worker在【另一个】线程池里可能正在飞：这里递增epoch代数，
        worker完成时发现代数变了就丢弃结果（不上屏、不写回上下文）——
        否则切语言瞬间，旧语言的句对还会蹦出来并污染新语言的翻译上下文。
        """
        self.processor.init()
        self.pending_text = ""
        self._last_unstable = ""
        self._draft_last_text = ""
        with self._tx_lock:
            self._tx_queue.clear()
            self._tx_epoch += 1  # 使在飞的翻译/草稿作废
            self.context_history.clear()
        print("🧹 已清空识别与翻译上下文")
        self._emit_display()

    def request_switch_language(self, new_lang):
        """切换源语言：清上下文和改SOURCE_LANGUAGE作为【一个任务】在ASR线程
        串行执行。之前是热键线程先改语言、清理任务排在后面——中间窗口期
        会拿新语言参数去识别缓冲里的旧语言音频，蹦出乱词。"""
        def task():
            self.clear_context()
            config.SOURCE_LANGUAGE = new_lang
            name = config.LANGUAGE_NAMES.get(new_lang, new_lang)
            print(f"🌐 源语言已切换为: {name}")
            if self.on_status:
                self.on_status(f"🌐 源语言已切换: {name}")
        try:
            self._asr_executor.submit(task)
        except RuntimeError:
            pass  # 程序正在退出

    # ------------------------------------------------------------------
    # 显示
    # ------------------------------------------------------------------
    def _live_text(self):
        """live行的白色部分 = 翻译中 + 待翻译 + 未成句残句"""
        with self._tx_lock:
            parts = list(self._tx_inflight) + list(self._tx_queue)
        if self.pending_text:
            parts.append(self.pending_text)
        return " ".join(parts)

    def _emit_display(self):
        if self.on_display:
            self.on_display(self._live_text(), self._last_unstable)

    # ------------------------------------------------------------------
    # 翻译（独立worker线程）
    # ------------------------------------------------------------------
    def _enqueue_sentences(self, sentences):
        """完整句子进翻译队列，唤醒worker"""
        if not sentences:
            return
        sentences = _squash_repeats(sentences)  # 压缩Whisper复读伪影
        with self._tx_lock:
            self._tx_queue.extend(sentences)
        try:
            self._tx_executor.submit(self._translation_worker)
        except RuntimeError:
            pass  # 程序正在退出

    def _translation_worker(self):
        """把队列里积压的句子合并成一次Ollama请求。

        batch有字符上限：说话快时积压句子无限合并会让单次请求越来越长
        （延迟和幻觉风险都涨），超限的留给下一轮（本轮末尾自我再调度）。
        至少取一句（单句本身可超过上限）；已有batch时若再塞会超限则停。
        """
        max_chars = getattr(config, "TRANSLATE_BATCH_MAX_CHARS", 300)
        with self._tx_lock:
            if not self._tx_queue:
                return
            epoch = self._tx_epoch  # 完成时代数变了（切了语言）就丢弃结果
            batch = []
            direct = []  # (德语, 词典中文)：队首的感叹词直接上屏不进Ollama
            total = 0
            while self._tx_queue:
                s = self._tx_queue[0]
                if not batch:
                    hit = _interjection_lookup(s)
                    if hit is not None:
                        # 只在batch还空时直发，保持上屏顺序不颠倒；
                        # batch里已有句子时感叹词跟着batch合并翻（不多花请求）
                        self._tx_queue.pop(0)
                        direct.append((s, hit))
                        continue
                if batch and total + len(s) > max_chars:
                    break
                self._tx_queue.pop(0)
                batch.append(s)
                total += len(s)
            self._tx_inflight = batch
            context = " ".join(self.context_history)

        if direct and not self.closing:
            with self._tx_lock:
                stale_direct = (epoch != self._tx_epoch)
            if not stale_direct:
                for g, zh in direct:
                    self._save_transcript(g, zh)
                    if self.on_pair:
                        self.on_pair(g, zh)
                with self._stats_lock:
                    self._stat_dict += len(direct)

        if not batch:
            return  # 本轮全是词典感叹词，不用打Ollama

        german = " ".join(batch)
        translation = None
        try:
            t0 = time.time()
            # 流式：翻译的中文逐段推到live区的草稿行，不等整句翻完
            translation = self._translate_single_sentence(
                german, context, on_partial=self._epoch_gated_draft(epoch))
            tx_elapsed = time.time() - t0
            with self._stats_lock:
                self._stat_tx.append(tx_elapsed)
            if config.SHOW_PERFORMANCE:
                print(f"   🔤 翻译{len(batch)}句 {tx_elapsed:.1f}秒: {german[:50]}{'...' if len(german) > 50 else ''}")
        finally:
            with self._tx_lock:
                self._tx_inflight = []

        with self._tx_lock:
            stale = (epoch != self._tx_epoch)
            if not stale and translation:
                self.context_history.extend(batch)
        if stale or self.closing or translation is None:
            return  # 期间切了语言/正在退出：旧语言结果不上屏不写回

        if translation != german:
            self._save_transcript(german, translation)
        if self.on_pair:
            # 翻译失败时 translation==german：只显示一遍德语，
            # 不要"德语\n德语"重复两行（Ollama挂掉时实测很难看）
            self.on_pair(german, "" if translation == german else translation)
        self._draft_last_text = ""  # 正式句对上屏了，残句草稿从头再来
        self._emit_display()

        # 本轮因batch上限留下的句子，自我再调度（不等下一次enqueue）
        with self._tx_lock:
            leftover = bool(self._tx_queue)
        if leftover:
            try:
                self._tx_executor.submit(self._translation_worker)
            except RuntimeError:
                pass

    def _maybe_draft(self):
        """残句草稿翻译：中文不用等"凑成完整句+正式翻译"，先出一版草稿。

        德语先行显示解决了"德语第一时刻上屏"，但中文要滞后一整句
        （长句可能5-15秒）。这里在翻译worker空闲时把当前残句先翻一版，
        UI以灰色斜体显示；正式句对完成后自动替换。
        只在正式翻译队列完全空闲时做，绝不和正式句对抢Ollama。"""
        if not getattr(config, "DRAFT_TRANSLATION", False) or not self.on_draft:
            return
        text = self.pending_text
        if len(text.split()) < getattr(config, "DRAFT_MIN_WORDS", 3):
            return
        if text == self._draft_last_text:
            return  # 残句没变，上一版草稿还有效
        if time.time() - self._draft_last_time < getattr(config, "DRAFT_MIN_INTERVAL", 1.5):
            return
        with self._asr_lock:
            if len(self._audio_inbox) >= 2:
                return  # 识别已在攒块（GPU被抢），草稿是奢侈品，先让路
        with self._tx_lock:
            if self._tx_queue or self._tx_inflight:
                return  # 正式翻译在忙，草稿让路
        self._draft_last_time = time.time()
        self._draft_last_text = text
        with self._stats_lock:
            self._stat_draft += 1
        try:
            self._tx_executor.submit(self._draft_worker, text)
        except RuntimeError:
            pass  # 程序正在退出

    def _epoch_gated_draft(self, epoch):
        """给流式partial套上代数/退出检查：切语言或退出后不再往屏幕推旧内容"""
        def emit(text):
            if self.closing or not self.on_draft:
                return
            with self._tx_lock:
                if epoch != self._tx_epoch:
                    return
            self.on_draft(text)
        return emit

    def _draft_worker(self, snapshot):
        """在翻译线程里跑草稿（和正式翻译同一个单线程池，天然串行）"""
        with self._tx_lock:
            if self._tx_queue:
                return  # 等草稿排到时已经来了正式句子，草稿没意义了
            epoch = self._tx_epoch
            context = " ".join(self.context_history)
        translation = self._translate_single_sentence(
            snapshot, context, on_partial=self._epoch_gated_draft(epoch))
        with self._tx_lock:
            if epoch != self._tx_epoch:
                return  # 期间切了语言，草稿作废
        if self.closing:
            return
        # 翻译失败会原样返回德语，那就不值得展示
        if not translation or translation == snapshot:
            return
        # 草稿期间残句可能已经变了：还在以snapshot开头（只是变长）就照常展示，
        # 完全变了（已成句送翻译/被清空）就丢弃
        if self.pending_text.startswith(snapshot):
            if config.SHOW_PERFORMANCE:
                print(f"   ✏️  草稿: {translation[:50]}{'...' if len(translation) > 50 else ''}")
            self.on_draft(translation)

    # ------------------------------------------------------------------
    # 点词查词（独立worker，不占字幕翻译的队列）
    # ------------------------------------------------------------------
    def _lookup_cache_get(self, cache_key):
        """真 LRU 读：命中则移到末尾。"""
        with self._lookup_cache_lock:
            text = self._lookup_cache.get(cache_key)
            if text is not None:
                self._lookup_cache.move_to_end(cache_key)
            return text

    def _lookup_cache_put(self, cache_key, text):
        """真 LRU 写：已存在则刷新位置；超额弹出最久未用。"""
        if not text:
            return
        with self._lookup_cache_lock:
            if cache_key in self._lookup_cache:
                self._lookup_cache.move_to_end(cache_key)
            self._lookup_cache[cache_key] = text
            while len(self._lookup_cache) > self._LOOKUP_CACHE_MAX:
                self._lookup_cache.popitem(last=False)

    def _serve_cached_lookup(self, word, cache_key, context, callback):
        """缓存命中就直接回调，返回True。缓存值是(词典文本, 当时的句境)：
        同一个词在【不同句子】里点，"本句中"那行是上一个句子的解释，
        会误导学习者——剥掉它再显示（原形/词性/释义与句境无关照常秒回）"""
        cached = self._lookup_cache_get(cache_key)
        if cached is None:
            return False
        text, cached_context = cached
        if context != cached_context:
            text = "\n".join(
                line for line in text.splitlines()
                if not line.strip().startswith("本句中")).strip()
        if config.SHOW_PERFORMANCE:
            print(f"   📖 查词缓存命中: {word}")
        callback(word, text)
        return True

    def lookup_word(self, word, context, callback):
        """查一个德语/英语单词的词典解释，完成后调 callback(word, text)。

        callback 必须线程安全（SubtitleWindow.show_lookup_result 走Qt信号）。
        缓存命中在调用线程同步返回，不进 executor、不打 Ollama。
        """
        cache_key = (word.lower(), config.SOURCE_LANGUAGE)
        if self._serve_cached_lookup(word, cache_key, context, callback):
            return
        try:
            self._lookup_executor.submit(self._lookup_worker, word, context, callback)
        except RuntimeError:
            pass  # 程序正在退出

    def _lookup_worker(self, word, context, callback):
        lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)
        cache_key = (word.lower(), config.SOURCE_LANGUAGE)
        # 双检：submit 前到 worker 之间可能已被别的点击填入缓存
        if self._serve_cached_lookup(word, cache_key, context, callback):
            return
        prompt = f"""/no_think 你是{lang_name}汉词典。简明解释{lang_name}单词"{word}"。
它出现在这句话里：{context}

严格按这个格式输出，不要多余内容：
原形: （动词给不定式、名词给单数带冠词，本身是原形就重复）
词性:
释义: 中文释义，最多2条，分号隔开
本句中: 一句话说明它在上面那句话里的意思
"""
        try:
            t0 = time.time()
            # 用查词专属session：和翻译线程共享一个requests.Session
            # 并发使用不保证线程安全
            response = self.lookup_session.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    # 不设的话 Ollama 默认5分钟无请求就卸载模型，
                    # 安静段/暂停后第一句要付~9秒冷加载
                    "keep_alive": "2h",
                    "options": {"temperature": 0.2, "num_predict": 220, "num_ctx": 2048},
                },
                timeout=15,
            )
            if self.closing:
                return  # 程序在退出，别再回调正在拆的UI
            if response.status_code == 200:
                text = response.json().get("response", "").strip()
                text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
                if config.SHOW_PERFORMANCE:
                    print(f"   📖 查词 {word} {time.time() - t0:.1f}秒")
                if text:
                    self._lookup_cache_put(cache_key, (text, context))
                callback(word, text or "（没查到）")
            else:
                callback(word, f"查询失败（HTTP {response.status_code}）")
        except Exception as e:
            if not self.closing:
                callback(word, f"查询失败: {e}")

    def _translate_single_sentence(self, sentence, german_context, on_partial=None):
        """翻译一段对白（源语言 -> 中文）。失败时返回原文。

        on_partial: 每~0.15秒把已生成的部分中文回调出去（流式上屏，
        中文首字延迟从"整句翻完"降到首token到达）。回调必须线程安全。
        """
        try:
            lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)

            # 术语表只注入当前句子/上下文里真出现的词条，prompt保持精简
            haystack = f"{german_context} {sentence}".lower()
            matched_terms = [
                f"{de} → {zh}" for de, zh in config.GLOSSARY.items()
                if de.lower() in haystack
            ]
            glossary_block = ""
            if matched_terms:
                glossary_block = "\n【术语表：以下人名/党派/术语必须照用这些译名】\n" + "\n".join(matched_terms[:12]) + "\n"

            prompt = f"""/no_think 你是{lang_name}影视剧的字幕翻译。请把{lang_name}对白翻译成自然的简体中文。

【要求】
1. 这是剧集对白：口语、俚语、粗话按中文对白的习惯翻，保持角色语气，不要书面腔
2. 结合上下文理解句意，不要机械直译；歌词就按歌词翻
3. 专有名词保持一致
4. 只输出中文翻译，不要解释，不要输出{lang_name}原文
{glossary_block}
【{lang_name}上下文（此前的对白）】
***
{german_context if german_context else "（无上下文）"}
***

【当前对白】
***
{sentence}
***

中文翻译：
"""

            response = self.ollama_session.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": True,  # 流式：中文逐段上屏，不等整句
                    "think": False,
                    "keep_alive": "2h",  # 默认5分钟卸载，安静段后第一句付~9秒冷加载
                    "options": {
                        "temperature": 0.3,
                        "top_p": 0.9,
                        "num_predict": 512,
                        "num_ctx": 4096,  # prompt多了术语表，2048偏紧
                        "num_gpu": 50,
                    }
                },
                stream=True,
                # 流式下timeout是"相邻数据块间隔"上限，不是总时长。
                # 别低于15：Ollama冷加载qwen3:8b实测9.2秒（首token前无数据），
                # 10秒会让服务重启后的头几句全部降级成德语
                timeout=15
            )

            try:
                if response.status_code == 200:
                    parts = []
                    last_emit = 0.0
                    for line in response.iter_lines():
                        if self.closing:
                            break  # 正在退出：别等整句生成完，finally会close连接
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                        except ValueError:
                            continue
                        parts.append(data.get("response", ""))
                        if data.get("done"):
                            break
                        if on_partial and time.time() - last_emit > 0.15:
                            partial = "".join(parts).strip()
                            if partial:
                                last_emit = time.time()
                                on_partial(partial)
                    translation = re.sub(r'<think>.*?</think>', '', "".join(parts), flags=re.DOTALL)
                    return translation.strip()
                else:
                    print(f"   ⚠️  Ollama 返回错误 (HTTP {response.status_code})，显示德语原文")
                    return sentence
            finally:
                # stream=True的连接不close不会归还连接池：done后break出来、
                # 半途超时、非200，都必须显式关，否则长session连接泄漏
                response.close()

        except requests.ConnectionError as e:
            # Ollama没在运行——屏幕上给用户明确提示（60秒节流），
            # 否则只是默默全德语，用户不知道发生了什么
            print(f"   ⚠️  翻译失败: {e}，显示德语原文")
            if self.on_status and time.time() - self._ollama_down_notified > 60:
                self._ollama_down_notified = time.time()
                self.on_status("⚠️ 翻译服务(Ollama)未运行，暂时只显示德语——请运行 启动字幕.bat 或 ollama serve")
            return sentence
        except Exception as e:
            print(f"   ⚠️  翻译失败: {e}，显示德语原文")
            return sentence

    # ------------------------------------------------------------------
    # 识别（ASR线程，单线程executor保证串行）
    # ------------------------------------------------------------------
    def _extract_sentences(self):
        """从pending_text里切出所有完整句子（只按.!?切，逗号不切），残句留下"""
        sentences = []
        rest = self.pending_text
        while True:
            m = _SENTENCE_END.match(rest)
            if not m:
                break
            sentences.append(m.group(1).strip())
            rest = rest[m.end():].lstrip()
        self.pending_text = rest
        return sentences

    def _append_committed(self, committed_text):
        if not committed_text:
            return
        if self.pending_text:
            self.pending_text += " " + committed_text
        else:
            self.pending_text = committed_text

    def enqueue_audio(self, audio_data, capture_time):
        """采集线程调用：音频进收件箱，识别线程没醒着就唤醒它（永不丢块）"""
        with self._asr_lock:
            self._audio_inbox.append((audio_data, capture_time))
            n = len(self._audio_inbox)
            if self._asr_scheduled:
                if n in (6, 12):  # GPU被抢时的提示，不丢数据
                    print(f"⚠️  GPU繁忙，字幕滞后约{n * config.CHUNK_SUBMIT_SECONDS:.0f}秒（攒了{n}块待识别，会自动追上）")
                return
            self._asr_scheduled = True
        try:
            self._asr_executor.submit(self._process_inbox)
        except RuntimeError:
            pass  # 程序正在退出

    def request_flush(self):
        """main的定时器调用：空闲收尾。识别忙着就不插队（它自己会消化）"""
        with self._asr_lock:
            if self._asr_scheduled or self._audio_inbox:
                return
        try:
            self._asr_executor.submit(self.flush_pending)
        except RuntimeError:
            pass

    def request_clear_context(self):
        """切换语言时调用：清上下文任务排进识别线程保证串行"""
        try:
            self._asr_executor.submit(self.clear_context)
        except RuntimeError:
            pass

    def request_warm_model(self, old_model=None, new_model=None):
        """游戏模式切翻译模型后调用：在翻译线程里卸掉旧模型、预热新模型。

        ☠️ 必须先卸旧模型：翻译请求带 keep_alive=2h，不显式卸载旧模型会
        赖满2小时——两个模型+游戏抢显存，Ollama把放不下的搬进系统内存
        （实测RAM冲到93%、llama-server吃11GB）。
        排进 _tx_executor 串行执行——加载期间到达的句子在它后面排队，
        等价于它们自己付加载费，但预热通常抢在第一句之前完成。

        ☠️ new_model 必须由调用方在改 config.OLLAMA_MODEL 的当下显式传入，
        不能靠 worker 执行时现读 config.OLLAMA_MODEL——热键快速连按时，
        提交和执行之间隔着排队延迟，config.OLLAMA_MODEL 可能已经被后续
        toggle 改到别的值，worker 读到的就不是这次切换真正要的目标模型，
        会导致该卸载的没卸载/该保留的被误卸载（压测复现过：连按6次后
        ollama ps 里9b和4b同时常驻）。"""
        try:
            self._tx_executor.submit(self._warm_model_worker, old_model, new_model)
        except RuntimeError:
            pass  # 程序正在退出

    def _warm_model_worker(self, old_model=None, new_model=None):
        model = new_model if new_model is not None else config.OLLAMA_MODEL
        if old_model and old_model != model:
            try:
                # keep_alive=0 = 立即卸载，先腾出显存再加载新模型
                self.ollama_session.post(
                    f"{config.OLLAMA_BASE_URL}/api/generate",
                    json={"model": old_model, "prompt": "", "keep_alive": 0},
                    timeout=30,
                ).close()
                print(f"🧹 已卸载旧翻译模型 {old_model}")
            except Exception as e:
                if not self.closing:
                    print(f"   ⚠️  卸载旧模型失败: {e}")
        try:
            t0 = time.time()
            # prompt留空：Ollama只加载模型不生成，是官方的预热用法
            self.ollama_session.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={"model": model, "prompt": "", "keep_alive": "2h"},
                timeout=60,  # 冷加载可能要十几秒
            ).close()
            print(f"🔥 翻译模型 {model} 预热完成 {time.time() - t0:.1f}秒")
        except Exception as e:
            if not self.closing:
                print(f"   ⚠️  模型预热失败（首句翻译会稍慢）: {e}")

    def _process_inbox(self):
        """识别线程主循环：把收件箱里攒下的块一次性处理完，空了才睡"""
        while True:
            with self._asr_lock:
                items = self._audio_inbox
                self._audio_inbox = []
                if not items:
                    self._asr_scheduled = False
                    return
            try:
                self._process_items(items)
                self._asr_error_streak = 0
            except Exception as e:
                print(f"❌ 识别错误: {e}")
                import traceback
                traceback.print_exc()
                # 单次异常可能是瞬时的（CUDA打嗝）；连续异常说明
                # HypothesisBuffer/音频缓冲已处于半更新的脏状态——
                # 重置识别器丢弃当前缓冲，比一直错乱下去强
                self._asr_error_streak = getattr(self, "_asr_error_streak", 0) + 1
                if self._asr_error_streak >= 2:
                    print("🧹 连续识别错误，重置识别器状态（丢弃当前音频缓冲）")
                    try:
                        self.processor.init()
                    except Exception:
                        pass
                    self._asr_error_streak = 0

    def _process_items(self, items):
        """把一批音频块塞进识别缓冲，整批只识别一遍"""
        start_time = time.time()
        self.last_audio_time = start_time

        for audio_data, capture_time in items:
            if capture_time is None:
                capture_time = start_time
            # 真实音频间隔 = 当前段开始时刻 - 上一段结束时刻
            segment_start = capture_time - len(audio_data) / config.SAMPLE_RATE
            real_gap = (segment_start - self.last_capture_end) if self.last_capture_end else 0.0
            self.last_capture_end = capture_time

            # 长时间没声音（暂停/静音）后恢复：旧缓冲已经过时，冲出尾部并重置
            if real_gap > 5.0 and self.processor.buffer_seconds() > 0:
                if config.SHOW_PERFORMANCE:
                    print(f"   🧹 音频中断{real_gap:.1f}秒，收尾并重置识别缓冲")
                self._append_committed(self.processor.finish())
                self.processor.init()
                # 中断前的内容不会再有下文，整段（含残句）都送翻译
                self._enqueue_sentences(self._extract_sentences())
                if self.pending_text:
                    self._enqueue_sentences([self.pending_text])
                    self.pending_text = ""

            self.processor.insert_audio_chunk(audio_data)

        committed, unstable = self.processor.process_iter()
        self._last_unstable = unstable

        self._append_committed(committed)
        self._enqueue_sentences(self._extract_sentences())

        # 聊天/嘈杂语音下Whisper经常整段不打标点，残句永远凑不成句 →
        # 超长就不等标点直接翻（实测聊天时德语堆在live行、中文迟迟不出）
        if len(self.pending_text.split()) > config.MAX_PENDING_WORDS:
            if config.SHOW_PERFORMANCE:
                print(f"   ✂️  残句超{config.MAX_PENDING_WORDS}词无标点，强制送翻译")
            self._enqueue_sentences([self.pending_text])
            self.pending_text = ""

        self._emit_display()
        self._maybe_draft()

        elapsed = time.time() - start_time
        self._stat_note_asr(elapsed, self.processor.buffer_seconds(), len(items))
        if config.SHOW_PERFORMANCE:
            shown = committed if committed else "(无新提交)"
            merged = f"(合并{len(items)}块)" if len(items) > 1 else ""
            print(f"   ⏱️  {elapsed:.2f}秒{merged} | 缓冲{self.processor.buffer_seconds():.1f}秒 | ✅ {shown[:60]}")
            if unstable:
                print(f"   ⏳ 未稳定: {unstable[:60]}")

    def _stat_note_asr(self, elapsed, buf_sec, n_items):
        """记一轮识别指标；到间隔就打一行概况（跑在ASR线程，无音频时不打）"""
        interval = getattr(config, "STATS_SUMMARY_INTERVAL", 60)
        if interval <= 0:
            return
        with self._stats_lock:
            self._stat_asr.append(elapsed)
            self._stat_buf_max = max(self._stat_buf_max, buf_sec)
            if n_items > 1:
                self._stat_merge += 1
            if time.time() - self._stats_t0 < interval:
                return
            asr, tx = sorted(self._stat_asr), sorted(self._stat_tx)
            merge, buf_max = self._stat_merge, self._stat_buf_max
            draft, dhit = self._stat_draft, self._stat_dict
            self._stat_asr, self._stat_tx = [], []
            self._stat_merge, self._stat_buf_max = 0, 0.0
            self._stat_draft = self._stat_dict = 0
            self._stats_t0 = time.time()

        def pct(a, q):
            return a[min(len(a) - 1, int(q * len(a)))] if a else 0.0

        line = (f"📈 概况: 识别{len(asr)}次 p50 {pct(asr, .5):.2f}s p90 {pct(asr, .9):.2f}s"
                f" | 缓冲峰值 {buf_max:.1f}s")
        if merge:
            line += f" | 合并{merge}轮"
        if tx:
            line += f" | 翻译{len(tx)}次 p50 {pct(tx, .5):.1f}s p90 {pct(tx, .9):.1f}s"
        if draft:
            line += f" | 草稿{draft}"
        if dhit:
            line += f" | 词典直译{dhit}"
        print(line)

    def flush_pending(self):
        """空闲兜底：一段话说完后没有新音频，未提交尾部/未成句残句会一直挂着。
        main的定时器每秒调这里：距上次音频超过IDLE_FLUSH_SEC才动手。
        和translate()跑在同一个单线程池里，天然串行。"""
        if time.time() - self.last_audio_time < config.IDLE_FLUSH_SEC:
            return

        tail = self.processor.finish()
        self._append_committed(tail)

        if not self.pending_text:
            # 没有要冲的内容；如果屏幕上还挂着灰色未稳定尾部，清掉重绘一次
            if self._last_unstable:
                self._last_unstable = ""
                self._emit_display()
            return

        sentences = self._extract_sentences()
        if self.pending_text:
            sentences.append(self.pending_text)
            self.pending_text = ""

        if sentences:
            if config.SHOW_PERFORMANCE:
                combined = " ".join(sentences)
                print(f"   🧹 收尾翻译: {combined[:50]}{'...' if len(combined) > 50 else ''}")
            self._enqueue_sentences(sentences)
        self._last_unstable = ""
        self._emit_display()

    def shutdown(self):
        """关闭识别/翻译线程（main.stop调用）。先ASR后翻译：
        ASR关完就不会再往翻译队列塞句子"""
        self.closing = True  # 在飞worker的出口检查：不再回调正在拆的UI
        # cancel_futures=True：队列里还没开跑的识别/翻译全部丢弃——结果没人看
        # （窗口在关、transcript也差不了几句），翻完再退纯属浪费退出时间。
        # 在飞的那一个任务照常等完：ASR最坏~2.5秒（GPU被抢时），流式翻译
        # 循环里查closing、一个数据块(~0.1秒)内就break出来
        self._asr_executor.shutdown(wait=True, cancel_futures=True)
        self._tx_executor.shutdown(wait=True, cancel_futures=True)
        self._lookup_executor.shutdown(wait=False, cancel_futures=True)
        try:
            self.ollama_session.close()
            self.lookup_session.close()
        except Exception:
            pass

    def __del__(self):
        """清理资源"""
        try:
            if hasattr(self, 'model'):
                del self.model
            if hasattr(self, 'ollama_session'):
                self.ollama_session.close()
        except Exception:
            pass
