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
import sys
import os
import time
import re
import queue
from threading import Lock
from concurrent.futures import ThreadPoolExecutor
from collections import deque

import requests

# torch本身在这个项目里没用（faster-whisper走ctranslate2），但venv里的
# ctranslate2版本会在内部无条件import torch。必须在下面往PATH里注入
# nvidia cublas目录【之前】加载torch——注入后再首次加载torch出现过
# c10.dll初始化失败(WinError 1114)，先加载则一直稳定
import torch

# ctranslate2 在 Windows 上通过 LoadLibraryA("cublas64_12.dll") 按名加载，
# 只认 PATH，不认 os.add_dll_directory 注册的路径，所以要直接塞进 PATH
if sys.platform == "win32":
    try:
        import nvidia.cublas
        os.environ["PATH"] = os.path.join(list(nvidia.cublas.__path__)[0], "bin") + os.pathsep + os.environ["PATH"]
    except ImportError:
        pass

from faster_whisper import WhisperModel
from streaming_asr import OnlineASRProcessor
import config

# 过滤所有警告信息
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)

# 句子结束符：只认 .!?（旧管线按逗号切句是碎句/上下文错乱的来源之一）
_SENTENCE_END = re.compile(r'(.*?[.!?…]["»«\']?)(\s|$)', re.DOTALL)


class WhisperQueueTranslator:
    """local agreement 增量识别 + 异步 Ollama 翻译"""

    def __init__(self):
        """初始化翻译器"""
        print("🔄 正在加载 Faster-Whisper 模型...")
        print(f"   模型: {config.WHISPER_MODEL}")
        print(f"   计算类型: {config.WHISPER_COMPUTE_TYPE}")

        start_time = time.time()

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
            self._tx_executor = ThreadPoolExecutor(max_workers=1)

            # 最近已翻译的德语句子，作为翻译上下文
            self.context_history = deque(maxlen=6)

            # UI回调，由main.py接线（都必须线程安全——SubtitleWindow用Qt信号保证）
            self.on_display = None  # (committed_live, unstable) -> None
            self.on_pair = None     # (german, chinese) -> None
            self.on_draft = None    # (chinese_draft) -> None 残句的草稿中文

            # 草稿翻译节流状态（残句还没凑成完整句子时先出一版灰色中文）
            self._draft_last_time = 0.0
            self._draft_last_text = ""

            # 创建复用的HTTP会话（用于Ollama）
            self.ollama_session = requests.Session()

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
        """清空识别/翻译上下文（切换源语言时调用，跑在ASR线程池里保证与translate串行）"""
        self.processor.init()
        self.pending_text = ""
        self._last_unstable = ""
        with self._tx_lock:
            self._tx_queue.clear()
        self.context_history.clear()
        print("🧹 已清空识别与翻译上下文")
        self._emit_display()

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
        with self._tx_lock:
            self._tx_queue.extend(sentences)
        try:
            self._tx_executor.submit(self._translation_worker)
        except RuntimeError:
            pass  # 程序正在退出

    def _translation_worker(self):
        """把队列里积压的句子合并成一次Ollama请求"""
        with self._tx_lock:
            if not self._tx_queue:
                return
            batch = list(self._tx_queue)
            self._tx_queue.clear()
            self._tx_inflight = batch

        german = " ".join(batch)
        context = " ".join(self.context_history)
        try:
            if config.SHOW_PERFORMANCE:
                t0 = time.time()
            translation = self._translate_single_sentence(german, context)
            if config.SHOW_PERFORMANCE:
                print(f"   🔤 翻译{len(batch)}句 {time.time() - t0:.1f}秒: {german[:50]}{'...' if len(german) > 50 else ''}")
        finally:
            with self._tx_lock:
                self._tx_inflight = []

        self.context_history.extend(batch)
        if translation and translation != german:
            self._save_transcript(german, translation)
        if self.on_pair:
            self.on_pair(german, translation)
        self._draft_last_text = ""  # 正式句对上屏了，残句草稿从头再来
        self._emit_display()

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
        with self._tx_lock:
            if self._tx_queue or self._tx_inflight:
                return  # 正式翻译在忙，草稿让路
        self._draft_last_time = time.time()
        self._draft_last_text = text
        try:
            self._tx_executor.submit(self._draft_worker, text)
        except RuntimeError:
            pass  # 程序正在退出

    def _draft_worker(self, snapshot):
        """在翻译线程里跑草稿（和正式翻译同一个单线程池，天然串行）"""
        with self._tx_lock:
            if self._tx_queue:
                return  # 等草稿排到时已经来了正式句子，草稿没意义了
        translation = self._translate_single_sentence(snapshot, " ".join(self.context_history))
        # 翻译失败会原样返回德语，那就不值得展示
        if not translation or translation == snapshot:
            return
        # 草稿期间残句可能已经变了：还在以snapshot开头（只是变长）就照常展示，
        # 完全变了（已成句送翻译/被清空）就丢弃
        if self.pending_text.startswith(snapshot):
            self.on_draft(translation)

    def _translate_single_sentence(self, sentence, german_context):
        """翻译一段对白（源语言 -> 中文）。失败时返回原文。"""
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
                    "stream": False,
                    "think": False,
                    "options": {
                        "temperature": 0.3,
                        "top_p": 0.9,
                        "num_predict": 512,
                        "num_ctx": 4096,  # prompt多了术语表，2048偏紧
                        "num_gpu": 50,
                    }
                },
                # 翻译在独立worker里跑，超时不会堵识别，但等太久句对显示会滞后
                timeout=10
            )

            if response.status_code == 200:
                result = response.json()
                translation = result.get('response', '').strip()
                translation = re.sub(r'<think>.*?</think>', '', translation, flags=re.DOTALL)
                return translation.strip()
            else:
                print(f"   ⚠️  Ollama 返回错误 (HTTP {response.status_code})，显示德语原文")
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
            except Exception as e:
                print(f"❌ 识别错误: {e}")
                import traceback
                traceback.print_exc()

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

        if config.SHOW_PERFORMANCE:
            elapsed = time.time() - start_time
            shown = committed if committed else "(无新提交)"
            merged = f"(合并{len(items)}块)" if len(items) > 1 else ""
            print(f"   ⏱️  {elapsed:.2f}秒{merged} | 缓冲{self.processor.buffer_seconds():.1f}秒 | ✅ {shown[:60]}")
            if unstable:
                print(f"   ⏳ 未稳定: {unstable[:60]}")

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
        self._asr_executor.shutdown(wait=True, cancel_futures=False)
        self._tx_executor.shutdown(wait=True, cancel_futures=False)

    def __del__(self):
        """清理资源"""
        try:
            if hasattr(self, 'model'):
                del self.model
            if hasattr(self, 'ollama_session'):
                self.ollama_session.close()
        except:
            pass
