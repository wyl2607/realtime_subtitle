"""
Local Agreement 增量识别（移植自 ufal/whisper_streaming, MIT License）

核心思想：音频缓冲持续增长，每次把整个缓冲交给 Whisper 识别；
只有连续两次识别结果一致的词前缀才"提交"(commit)——提交过的词不会再变，
从根上消灭旧管线"滑动窗口每0.5秒整窗重识别 + 文本相似度去重"带来的重复碎片。

与参考实现的差异：
- 只保留 faster-whisper 路径（本项目唯一后端），砍掉多后端抽象层。
- ts_words() 里除了跳过 no_speech_prob>0.9 的段，还应用
  config.HALLUCINATION_BLACKLIST（原来在 translator_queue 里做，段级信息在这才有）。
- 识别参数从 config 读（语言可被 Ctrl+Alt+L 热切换，所以每次调用现读）。
- vad_filter=True：faster-whisper 内置 Silero VAD（onnx 自带无新依赖），
  压制剧集里背景音乐/音效段——能量阈值对音乐无能为力，这个有。
"""
import numpy as np
import config


class HypothesisBuffer:
    """两次连续识别结果的最长公共词前缀才提交（local agreement-2）

    词条格式统一为 (start_sec, end_sec, "word")，时间是全局时间轴
    （buffer_time_offset 已加上）。
    """

    def __init__(self):
        self.commited_in_buffer = []  # 已提交、且对应音频还在缓冲里的词
        self.buffer = []  # 上一次识别的未提交尾部
        self.new = []  # 本次识别的新词
        self.last_commited_time = 0
        self.last_commited_word = None

    def insert(self, new, offset):
        """插入本次识别结果。只保留大致位于已提交时间之后的词；
        如果新词头部和已提交尾部逐词重复（Whisper 常见），做 1-5 gram 去重。"""
        new = [(a + offset, b + offset, t) for a, b, t in new]
        self.new = [(a, b, t) for a, b, t in new if a > self.last_commited_time - 0.1]

        if len(self.new) >= 1:
            a, b, t = self.new[0]
            if abs(a - self.last_commited_time) < 1:
                if self.commited_in_buffer:
                    cn = len(self.commited_in_buffer)
                    nn = len(self.new)
                    for i in range(1, min(min(cn, nn), 5) + 1):
                        c = " ".join([self.commited_in_buffer[-j][2] for j in range(1, i + 1)][::-1])
                        tail = " ".join(self.new[j - 1][2] for j in range(1, i + 1))
                        if c == tail:
                            for _ in range(i):
                                self.new.pop(0)
                            break

    def flush(self):
        """提交 = 上次未提交尾部和本次新词的最长公共前缀"""
        commit = []
        while self.new:
            na, nb, nt = self.new[0]
            if len(self.buffer) == 0:
                break
            if nt == self.buffer[0][2]:
                commit.append((na, nb, nt))
                self.last_commited_word = nt
                self.last_commited_time = nb
                self.buffer.pop(0)
                self.new.pop(0)
            else:
                break
        self.buffer = self.new
        self.new = []
        self.commited_in_buffer.extend(commit)
        return commit

    def pop_commited(self, time):
        """缓冲裁剪后，丢掉已滚出音频缓冲的已提交词"""
        while self.commited_in_buffer and self.commited_in_buffer[0][1] <= time:
            self.commited_in_buffer.pop(0)

    def complete(self):
        """当前未提交的尾部（悬浮窗灰色显示用）"""
        return self.buffer


class OnlineASRProcessor:
    """增长式音频缓冲 + local agreement 提交 + 已完成segment处裁剪"""

    SAMPLING_RATE = 16000

    def __init__(self, model):
        """model: 已加载的 faster_whisper.WhisperModel（translator_queue 持有）"""
        self.model = model
        self.init()

    def init(self, offset=None):
        """启动/语言切换/长静音后重置"""
        self.audio_buffer = np.array([], dtype=np.float32)
        self._audio_chunks = []  # 待合并块，避免每 0.5s np.append 整段拷贝
        self.transcript_buffer = HypothesisBuffer()
        self.buffer_time_offset = offset if offset is not None else 0
        self.transcript_buffer.last_commited_time = self.buffer_time_offset
        self.commited = []  # 全部已提交词（prompt 上下文用），只保留尾部若干

    def insert_audio_chunk(self, audio):
        """攒块；真正拼进 audio_buffer 在 process_iter/finish 里一次 concatenate。"""
        if audio is None or len(audio) == 0:
            return
        self._audio_chunks.append(np.asarray(audio, dtype=np.float32))

    def _flush_audio_chunks(self):
        """把 _audio_chunks 合并进 audio_buffer（每轮识别最多一次拷贝）"""
        if not self._audio_chunks:
            return
        if len(self.audio_buffer) == 0:
            self.audio_buffer = (
                self._audio_chunks[0]
                if len(self._audio_chunks) == 1
                else np.concatenate(self._audio_chunks)
            )
        else:
            self.audio_buffer = np.concatenate([self.audio_buffer] + self._audio_chunks)
        self._audio_chunks = []

    def _prompt(self):
        """已提交且滚出音频缓冲的文本尾部（≤200字符）作为 initial_prompt，
        给 Whisper 跨缓冲的上下文——这是识别准确度相对旧管线的另一个提升点"""
        k = len(self.commited)
        while k > 0 and self.commited[k - 1][1] > self.buffer_time_offset:
            k -= 1
        p = [t for _, _, t in self.commited[:k]]
        prompt = []
        l = 0
        while p and l < 200:
            x = p.pop(-1)
            l += len(x) + 1
            prompt.append(x)
        text = "".join(prompt[::-1])
        # Whisper会模仿prompt的文风：嘈杂语音识别出的无标点文本进了prompt，
        # 会诱导后续继续不打标点（恶性循环，句子切分全靠标点）。补个句号打断
        if text and text[-1] not in ".!?…":
            text += "."
        return text

    def _is_hallucination(self, text):
        lowered = text.lower()
        return any(pattern in lowered for pattern in config.HALLUCINATION_BLACKLIST)

    def _ts_words(self, segments):
        """segment 流 → [(start, end, word)]，段级过滤静音幻觉。
        注意 faster-whisper 的 word.word 自带前导空格，不能 strip（拼接时需要）"""
        out = []
        for segment in segments:
            if segment.no_speech_prob > 0.9:
                continue
            if self._is_hallucination(segment.text):
                if config.SHOW_PERFORMANCE:
                    print(f"   🚫 丢弃幻觉片段: {segment.text.strip()[:40]}")
                continue
            for word in segment.words:
                out.append((word.start, word.end, word.word))
        return out

    def process_iter(self):
        """识别当前整个音频缓冲，返回 (新提交的文本, 未稳定尾部文本)"""
        self._flush_audio_chunks()
        if len(self.audio_buffer) == 0:
            return "", ""
        prompt = self._prompt()
        segments, _info = self.model.transcribe(
            self.audio_buffer,
            language=config.SOURCE_LANGUAGE,
            task=config.WHISPER_TASK,
            initial_prompt=prompt,
            beam_size=config.WHISPER_BEAM_SIZE,
            word_timestamps=True,
            condition_on_previous_text=True,
            vad_filter=True,
        )
        res = list(segments)

        tsw = self._ts_words(res)
        self.transcript_buffer.insert(tsw, self.buffer_time_offset)
        committed = self.transcript_buffer.flush()
        self.commited.extend(committed)
        # commited 只用于 prompt 尾部200字符，别让它无限涨
        if len(self.commited) > 200:
            self.commited = self.commited[-200:]

        # 缓冲超长：优先在倒数第二个已完成 segment 结尾处裁剪
        if len(self.audio_buffer) / self.SAMPLING_RATE > config.BUFFER_TRIM_SEC:
            self._chunk_completed_segment(res)

        # segment裁剪对连续不停顿的语音经常不满足条件（直播实测缓冲涨到26秒、
        # 单次识别2.4秒、开始丢块）——兜底：直接在已提交词边界裁，
        # 保留最近 BUFFER_KEEP_SEC 秒音频作为识别上下文
        if len(self.audio_buffer) / self.SAMPLING_RATE > config.BUFFER_TRIM_SEC:
            self._chunk_at_committed_word()

        # 绝对硬上限：长段音乐/噪音（能量高但无语音）一个词都提交不出来，
        # 上面两种裁剪都无处可裁，缓冲会干涨（直播实测涨到59秒）。
        # 超过 TRIM+KEEP 还裁不动就直接丢最旧音频——这时丢的必然是音乐不是话
        cur_sec = len(self.audio_buffer) / self.SAMPLING_RATE
        if cur_sec > config.BUFFER_TRIM_SEC + config.BUFFER_KEEP_SEC:
            if config.SHOW_PERFORMANCE:
                print(f"   ✂️  缓冲{cur_sec:.0f}秒无可裁词（音乐/噪音），硬裁至{config.BUFFER_KEEP_SEC:.0f}秒")
            self._chunk_at(self.buffer_time_offset + cur_sec - config.BUFFER_KEEP_SEC)

        committed_text = "".join(t for _, _, t in committed).strip()
        unstable_text = "".join(t for _, _, t in self.transcript_buffer.complete()).strip()
        return committed_text, unstable_text

    def _chunk_completed_segment(self, res):
        if not self.commited:
            return
        ends = [s.end for s in res]
        t = self.commited[-1][1]
        if len(ends) > 1:
            e = ends[-2] + self.buffer_time_offset
            while len(ends) > 2 and e > t:
                ends.pop(-1)
                e = ends[-2] + self.buffer_time_offset
            if e <= t:
                self._chunk_at(e)

    def _chunk_at_committed_word(self):
        """兜底裁剪：在"距缓冲末尾至少 BUFFER_KEEP_SEC 秒"的最新已提交词结尾处裁。
        已提交词之后才是未稳定区，在词边界裁不会切掉还没定稿的内容。"""
        if not self.commited:
            return
        latest_allowed = self.buffer_time_offset + len(self.audio_buffer) / self.SAMPLING_RATE - config.BUFFER_KEEP_SEC
        cut = None
        for _, end, _ in reversed(self.commited):
            if end <= self.buffer_time_offset:
                break  # 已经滚出缓冲的词，再往前没意义
            if end <= latest_allowed:
                cut = end
                break
        if cut is not None:
            if config.SHOW_PERFORMANCE:
                print(f"   ✂️  兜底裁剪至已提交词边界 (保留{config.BUFFER_KEEP_SEC:.0f}秒)")
            self._chunk_at(cut)

    def _chunk_at(self, time):
        self._flush_audio_chunks()
        self.transcript_buffer.pop_commited(time)
        cut_seconds = time - self.buffer_time_offset
        self.audio_buffer = self.audio_buffer[int(cut_seconds * self.SAMPLING_RATE):]
        self.buffer_time_offset = time

    def finish(self):
        """结束/长静音/暂停时冲出未提交尾部（不再等第二次一致确认）"""
        self._flush_audio_chunks()
        o = self.transcript_buffer.complete()
        self.transcript_buffer.buffer = []
        self.buffer_time_offset += len(self.audio_buffer) / self.SAMPLING_RATE
        self.audio_buffer = np.array([], dtype=np.float32)
        self._audio_chunks = []
        return "".join(t for _, _, t in o).strip()

    def buffer_seconds(self):
        """当前音频缓冲长度（秒），性能日志用（含尚未合并的 chunks）"""
        pending = sum(len(c) for c in self._audio_chunks)
        return (len(self.audio_buffer) + pending) / self.SAMPLING_RATE
