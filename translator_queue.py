"""
Whisper + LLM 句子队列翻译模块
使用句子队列管理，避免重复翻译，只翻译完整句子
"""
import warnings
import logging
import sys
import os
import time
import requests
import re
import numpy as np

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
from difflib import SequenceMatcher
import config

# 过滤所有警告信息
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)

class WhisperQueueTranslator:
    """Whisper + 句子队列翻译器"""
    
    def __init__(self):
        """初始化翻译器"""
        print("🔄 正在加载 Faster-Whisper 模型...")
        print(f"   模型: {config.WHISPER_MODEL}")
        print(f"   计算类型: {config.WHISPER_COMPUTE_TYPE}")
        
        start_time = time.time()
        
        try:
            # 加载 Faster-Whisper 模型
            self.model = WhisperModel(
                config.WHISPER_MODEL,
                device=config.WHISPER_DEVICE,
                compute_type=config.WHISPER_COMPUTE_TYPE
            )
            
            # 句子缓冲队列
            self.sentence_queue = []  # 保存最近10条完整句子
            self.max_queue_size = 10
            
            # 最后一句的稳定性检测
            self.last_sentence = None  # 上次的最后一句
            self.last_sentence_count = 0  # 最后一句连续出现次数
            self.last_audio_time = time.time()  # 最后一次处理音频的墙钟时间（flush用）
            self.last_capture_end = None  # 上一段音频在采集端结束的时刻（算真实间隔用）
            
            # 音频上下文缓冲（滑动窗口）
            self.audio_context_buffer = []
            
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
            print(f"✅ 句子队列翻译方案已启用（队列大小: {self.max_queue_size}）")
            
            # 显示设备信息
            print(f"   设备: {config.WHISPER_DEVICE.upper()}")
            
        except Exception as e:
            print(f"❌ 模型加载失败: {e}")
            raise
    
    def _is_hallucination(self, text):
        """判断识别片段是否为Whisper幻觉（静音时凭空生成的电视字幕惯用语）"""
        lowered = text.lower()
        return any(pattern in lowered for pattern in config.HALLUCINATION_BLACKLIST)

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

    def clear_context(self):
        """清空识别/翻译上下文（切换源语言时调用，跑在翻译线程池里保证串行）"""
        self.audio_context_buffer = []
        self.sentence_queue = []
        self.last_sentence = None
        self.last_sentence_count = 0
        print("🧹 已清空识别与翻译上下文")

    def _normalize_text(self, text):
        """标准化文本（用于相似度对比）"""
        text = text.lower().strip()
        # 去除所有标点符号
        text = re.sub(r'[^\w\s]', '', text)
        # 去除多余空格
        text = re.sub(r'\s+', ' ', text)
        return text
    
    def _calculate_similarity(self, text1, text2):
        """计算两个文本的相似度"""
        return SequenceMatcher(None, text1, text2).ratio()
    
    def _split_sentences(self, text):
        """提取所有完整的分句（按逗号、句号等分割）"""
        # 按逗号、句号、问号、感叹号分割，保留分隔符
        parts = re.split(r'([,.!?]+)', text)
        
        # 重新组合句子（包含标点符号）
        sentences = []
        for i in range(0, len(parts)-1, 2):
            sentence_text = parts[i].strip()
            if i+1 < len(parts):
                sentence_text += parts[i+1]  # 添加标点符号
            if sentence_text.strip():
                sentences.append(sentence_text.strip())
        
        # 返回所有完整分句（不包括最后没有标点的部分）
        return sentences
    
    def _find_similar_sentence(self, text, threshold=0.70):
        """在队列中查找相似句子"""
        normalized = self._normalize_text(text)
        
        for item in self.sentence_queue:
            item_normalized = item['normalized_text']
            
            # 策略1：标准相似度检测
            if self._calculate_similarity(normalized, item_normalized) >= threshold:
                return item
            
            # 策略2：子串包含检测（处理截断情况）
            # 如果当前句子是历史句子的子串，或历史句子是当前句子的子串
            if normalized in item_normalized or item_normalized in normalized:
                # 额外检查：至少有30%的重叠（处理长句截断）
                overlap_ratio = min(len(normalized), len(item_normalized)) / max(len(normalized), len(item_normalized))
                if overlap_ratio >= 0.3:
                    return item
        
        return None
    
    def _add_sentence_to_queue(self, text):
        """添加新句子到队列"""
        item = {
            'text': text,
            'normalized_text': self._normalize_text(text),
            'translation': None,
            'is_translated': False,
            'timestamp': time.time()
        }
        self.sentence_queue.append(item)
        if len(self.sentence_queue) > self.max_queue_size:
            self.sentence_queue.pop(0)
        return item
    
    def _format_bilingual(self, source_text, translation):
        """格式化为德中双语字幕"""
        # 翻译失败时translation就是德语原文，双语模式下避免同一句显示两遍
        if getattr(config, "SHOW_BILINGUAL", False) and translation != source_text:
            return f"{source_text}\n{translation}"
        return translation

    def _translate_single_sentence(self, sentence, german_context):
        """翻译单个句子（源语言 -> 中文）"""
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

            prompt = f"""/no_think 你是专业的{lang_name}实时字幕翻译助手。请把{lang_name}句子翻译成自然流畅的简体中文。

【要求】
1. 结合上下文理解句意，不要机械直译
2. 保留说话语气和口语感
3. 专有名词保持一致
4. 只输出中文翻译，不要解释，不要输出{lang_name}原文
{glossary_block}
【{lang_name}上下文】
***
{german_context if german_context else "（无上下文）"}
***

【当前{lang_name}句子】
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
                # 实时字幕等不起30秒：单线程翻译池被一次慢请求堵住时，
                # 音频队列(约5秒容量)必然溢出丢音频，超时后显示德语原文往前走
                timeout=10
            )
            
            if response.status_code == 200:
                result = response.json()
                translation = result.get('response', '').strip()
                
                # 清理输出
                translation = re.sub(r'<think>.*?</think>', '', translation, flags=re.DOTALL)
                translation = translation.strip()
                
                return translation
            else:
                print(f"   ⚠️  Ollama 返回错误 (HTTP {response.status_code})，显示德语原文")
                return sentence

        except Exception as e:
            print(f"   ⚠️  翻译失败: {e}，显示德语原文")
            return sentence
    
    def translate(self, audio_data, capture_time=None):
        """翻译音频

        Args:
            audio_data: numpy float32 音频
            capture_time: 采集端提交这段音频的时刻（段尾时间戳）。
                用它算两段音频之间的真实间隔——之前用两次translate()调用
                的间隔当"静音时长"，结果把Whisper自己0.3-1.5秒的处理耗时
                也算成了静音，连续说话时也会误判"说完了"
        """
        try:
            start_time = time.time()
            self.last_audio_time = start_time  # flush_pending靠它判断"多久没新音频"

            # 真实音频间隔 = 当前段开始时刻 - 上一段结束时刻
            if capture_time is None:
                capture_time = start_time
            segment_start = capture_time - len(audio_data) / config.SAMPLE_RATE
            real_gap = (segment_start - self.last_capture_end) if self.last_capture_end else 0.0
            self.last_capture_end = capture_time

            # 长时间没声音（暂停/直播安静）后恢复：暂停前的音频上下文已经过时，
            # 不清掉会把旧内容拼进新音频里重新识别一遍
            if real_gap > 5.0 and self.audio_context_buffer:
                if config.SHOW_PERFORMANCE:
                    print(f"   🧹 音频中断{real_gap:.1f}秒，清空音频上下文")
                self.audio_context_buffer = []

            # 添加当前音频到上下文缓冲区
            self.audio_context_buffer.append(audio_data)
            if len(self.audio_context_buffer) > config.AUDIO_CONTEXT_WINDOW:
                self.audio_context_buffer.pop(0)
            
            # 拼接音频上下文
            combined_audio = np.concatenate(self.audio_context_buffer)
            if config.SHOW_PERFORMANCE:
                audio_duration = len(combined_audio) / config.SAMPLE_RATE
                print(f"   🎵 音频窗口: {len(self.audio_context_buffer)}个片段 ({audio_duration:.1f}秒)")
            
            # Whisper识别
            segments, info = self.model.transcribe(
                combined_audio,
                language=config.SOURCE_LANGUAGE,
                task=config.WHISPER_TASK,
                beam_size=config.WHISPER_BEAM_SIZE,
                temperature=config.WHISPER_TEMPERATURE,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=None,  # 移除initial_prompt，避免被当作识别结果
            )
            
            # 收集识别结果（顺手丢掉幻觉片段——静音时Whisper会背出
            # "Untertitelung des ZDF"之类的电视字幕版权惯用语）
            full_text = ""
            for segment in segments:
                text = segment.text.strip()
                if not text:
                    continue
                if self._is_hallucination(text):
                    if config.SHOW_PERFORMANCE:
                        print(f"   🚫 丢弃幻觉片段: {text[:40]}")
                    continue
                full_text += text + " "
            full_text = full_text.strip()
            
            if config.SHOW_PERFORMANCE:
                print(f"   📝 {full_text[:60]}{'...' if len(full_text) > 60 else ''}")
            
            if not full_text:
                return ""
            
            # 步骤1：分割句子（丢弃最后一句）
            sentences = self._split_sentences(full_text)
            
            if config.SHOW_PERFORMANCE:
                print(f"   📋 分割: {len(sentences)}句")
            
            if not sentences:
                return ""
            
            # 步骤2：处理每个句子
            # 检测最后一句的稳定性
            current_last_sentence = sentences[-1] if sentences else None
            last_sentence_stable = False
            
            if current_last_sentence:
                # 检查最后一句是否已经翻译过（使用相似度匹配）
                last_sentence_already_translated = False
                similar_last = self._find_similar_sentence(current_last_sentence)
                if similar_last and similar_last['is_translated']:
                    last_sentence_already_translated = True
                
                # 如果还未翻译，判断是否应该翻译
                if not last_sentence_already_translated:
                    if current_last_sentence == self.last_sentence:
                        self.last_sentence_count += 1
                        # 条件1：连续出现2次
                        if self.last_sentence_count >= 2:
                            last_sentence_stable = True
                            if config.SHOW_PERFORMANCE:
                                print(f"   ✅ 最后一句稳定（连续2次）")
                    else:
                        self.last_sentence = current_last_sentence
                        self.last_sentence_count = 1

                    # 说明：之前这里还有"两次调用间隔>0.5秒就算稳定"的条件，
                    # 但那个间隔混入了识别耗时，连续说话时也频繁误触发，
                    # 导致半截句被提前翻译。真正说完了的尾句由flush_pending()兜底。
                else:
                    # 已翻译过，不再处理
                    if config.SHOW_PERFORMANCE:
                        print(f"   ⏭️  最后一句已翻译，跳过")
            
            # 过滤策略：
            # - 如果有3句或更多：跳过第一句，翻译中间句子，最后一句根据稳定性决定
            # - 如果有2句：跳过第一句，最后一句根据稳定性决定
            # - 如果只有1句：根据稳定性决定
            
            if len(sentences) >= 3:
                # 跳过第一句，中间的句子全部翻译
                sentences_to_translate = sentences[1:-1]
                # 如果最后一句稳定，也翻译
                if last_sentence_stable:
                    sentences_to_translate.append(sentences[-1])

            elif len(sentences) == 2:
                # 跳过第一句，最后一句根据稳定性
                if last_sentence_stable:
                    sentences_to_translate = [sentences[1]]
                else:
                    sentences_to_translate = []

            else:
                # 只有1句，根据稳定性
                if last_sentence_stable:
                    sentences_to_translate = sentences
                else:
                    sentences_to_translate = []
                    if config.SHOW_PERFORMANCE:
                        print(f"   ⏭️  只有1句且未稳定，不翻译")
            
            # 检查队列中是否有累积的未翻译短句子
            untranslated_short_sentences = [
                item for item in self.sentence_queue 
                if not item['is_translated'] and len(item['text'].split()) <= 20
            ]
            
            new_translations = []
            
            # 如果累积了3个或以上未翻译的短句子，强制翻译它们
            if len(untranslated_short_sentences) >= 3:
                if config.SHOW_PERFORMANCE:
                    print(f"   📦 累积了{len(untranslated_short_sentences)}个短句子，合并翻译")
                
                # 合并所有短句子
                combined_sentence = " ".join([item['text'] for item in untranslated_short_sentences])
                german_context = " ".join([item['text'] for item in self.sentence_queue])
                
                if config.SHOW_PERFORMANCE:
                    print(f"   🔤 翻译(合并): {combined_sentence[:50]}...")
                
                translation = self._translate_single_sentence(combined_sentence, german_context)
                
                if translation:
                    display_text = self._format_bilingual(combined_sentence, translation)
                    self._save_transcript(combined_sentence, translation)
                    # 标记所有短句子为已翻译（使用相同的翻译）
                    for item in untranslated_short_sentences:
                        item['is_translated'] = True
                        item['translation'] = translation

                    # 添加翻译结果
                    new_translations.append(display_text)
            
            for sentence in sentences_to_translate:
                # 检查句子长度（单词数）
                word_count = len(sentence.split())
                if word_count <= 20:
                    if config.SHOW_PERFORMANCE:
                        print(f"   ⏭️  句子太短({word_count}词)，跳过: {sentence[:30]}...")
                    # 加入队列但不翻译，等待下次和其他句子一起翻译
                    similar_item = self._find_similar_sentence(sentence)
                    if not similar_item:
                        self._add_sentence_to_queue(sentence)
                    continue
                
                # 步骤3：查找相似句子
                similar_item = self._find_similar_sentence(sentence)

                if similar_item:
                    # 找到相似句子
                    if similar_item['is_translated']:
                        # 已翻译，跳过
                        continue
                    # 未翻译，使用这个item
                else:
                    # 没有相似句子，添加到队列
                    similar_item = self._add_sentence_to_queue(sentence)
                
                # 步骤4：翻译句子（使用队列中的德语原文作为上下文）
                german_context = " ".join([item['text'] for item in self.sentence_queue])
                
                if config.SHOW_PERFORMANCE:
                    print(f"   🔤 翻译({word_count}词): {sentence}")
                
                translation = self._translate_single_sentence(sentence, german_context)
                
                if translation:
                    display_text = self._format_bilingual(sentence, translation)
                    self._save_transcript(sentence, translation)
                    # 标记为已翻译并保存
                    similar_item['is_translated'] = True
                    similar_item['translation'] = translation
                    new_translations.append(display_text)
            
            # 步骤5：合并所有新翻译
            result = " ".join(new_translations) if new_translations else ""
            
            # 限制长度
            if result and len(result) > config.MAX_SUBTITLE_LENGTH:
                result = result[:config.MAX_SUBTITLE_LENGTH] + "..."
            
            if config.SHOW_PERFORMANCE:
                elapsed = time.time() - start_time
                print(f"   ⏱️  {elapsed:.2f}秒 | 新增{len(new_translations)}句 | 队列{len(self.sentence_queue)}/{self.max_queue_size}")
            
            return result if result else ""
            
        except Exception as e:
            print(f"❌ 翻译错误: {e}")
            import traceback
            traceback.print_exc()
            return ""
    
    def flush_pending(self, idle_seconds=2.0):
        """把积压的未翻译句子强制翻译掉（尾句兜底）

        短句平时要攒够3个才合并翻译、末句要"连续出现2次"才算稳定——
        一段话说完之后没有新音频，translate()就不会再被调用，
        最后一两句会永远卡在队列里不显示。主程序定时调用这里兜底：
        距离上一次收到音频超过idle_seconds才动手，说话中途不会插手。
        和translate()跑在同一个单线程池里，天然串行，不需要加锁。
        """
        if time.time() - self.last_audio_time < idle_seconds:
            return ""

        pending = [item for item in self.sentence_queue if not item['is_translated']]

        # 末句可能因为"未稳定"从来没进过队列，这里一并捞回来
        if self.last_sentence:
            similar = self._find_similar_sentence(self.last_sentence)
            if similar is None:
                pending.append(self._add_sentence_to_queue(self.last_sentence))
            elif not similar['is_translated'] and similar not in pending:
                pending.append(similar)
            self.last_sentence = None
            self.last_sentence_count = 0

        if not pending:
            return ""

        combined = " ".join(item['text'] for item in pending)
        german_context = " ".join(item['text'] for item in self.sentence_queue)

        if config.SHOW_PERFORMANCE:
            print(f"   🧹 收尾翻译{len(pending)}句: {combined[:50]}{'...' if len(combined) > 50 else ''}")

        translation = self._translate_single_sentence(combined, german_context)
        if not translation:
            return ""

        for item in pending:
            item['is_translated'] = True
            item['translation'] = translation

        self._save_transcript(combined, translation)
        return self._format_bilingual(combined, translation)

    def __del__(self):
        """清理资源"""
        try:
            if hasattr(self, 'model'):
                del self.model
            if hasattr(self, 'ollama_session'):
                self.ollama_session.close()
        except:
            pass

