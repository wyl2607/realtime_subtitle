"""
Whisper + LLM 句子队列翻译模块
使用句子队列管理，避免重复翻译，只翻译完整句子
"""
import warnings
import logging
import time
import torch
import requests
import re
import numpy as np
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
            self.last_audio_time = time.time()  # 最后一次音频时间
            
            # 音频上下文缓冲（滑动窗口）
            self.audio_context_buffer = []
            
            # 创建复用的HTTP会话（用于Ollama）
            self.ollama_session = requests.Session()
            
            elapsed = time.time() - start_time
            print(f"✅ Whisper 模型加载完成！({elapsed:.1f}秒)")
            print(f"✅ 句子队列翻译方案已启用（队列大小: {self.max_queue_size}）")
            
            # 显示设备信息
            print(f"   设备: {config.WHISPER_DEVICE.upper()}")
            if config.WHISPER_DEVICE == "cuda" and torch.cuda.is_available():
                gpu_name = torch.cuda.get_device_name(0)
                print(f"   GPU: {gpu_name}")
            
        except Exception as e:
            print(f"❌ 模型加载失败: {e}")
            raise
    
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
    
    def _translate_single_sentence(self, sentence, english_context):
        """翻译单个句子"""
        try:
            # 构建prompt（明确LLM职责）
            prompt = f"""/no_think 你是一个专业的实时字幕翻译助手，请完成以下翻译任务：

【核心要求】
⚠️ 必须结合上下文理解当前句子的真实含义，不要进行孤立的字面翻译
⚠️ 根据上下文判断词语的实际意思（例如：在分手场景中，"I'm done" 应译为"我受够了"而非"我完成了"）

【如何使用上下文】
1. 先阅读英文上下文，理解当前的情境、场景、话题
2. 判断当前句子在这个情境中的真实意图和情感色彩
3. 选择符合情境的中文表达，而不是直译
4. 注意人物代词（he/she/they）的指代关系
5. 注意时态和语气的连贯性
6. 注意专有名词的一致性（如人名、地名）

【翻译要求】
1. 必须参考上下文来确定词语的准确含义
2. 翻译要准确、流畅、符合中文表达习惯
3. 保持与上下文的情境一致性和语义连贯性
4. 仅输出中文翻译结果，不要有任何解释或多余内容

【示例】
上下文："I can't do this anymore. We're not working out."
待翻译："I'm done."
分析：这是分手场景，"done"表示受够了、结束关系
正确翻译：我受够了。
错误翻译：我完成了。❌

【英文上下文】
***
{english_context if english_context else "（无上下文）"}
***

【当前需要翻译的句子】
***
{sentence}
***

请先在心中理解上下文的情境，然后翻译当前句子。
输出：
【中文翻译结果】
"""
            
            response = self.ollama_session.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "options": {
                        "temperature": 0.3,
                        "top_p": 0.9,
                        "num_predict": 512,
                        "num_ctx": 2048,
                        "num_gpu": 50,
                    }
                },
                timeout=30
            )
            
            if response.status_code == 200:
                result = response.json()
                translation = result.get('response', '').strip()
                
                # 清理输出
                translation = re.sub(r'<think>.*?</think>', '', translation, flags=re.DOTALL)
                translation = translation.strip()
                
                return translation
            else:
                return sentence
                
        except Exception as e:
            if config.SHOW_PERFORMANCE:
                print(f"   ⚠️  翻译失败: {e}")
            return sentence
    
    def translate(self, audio_data):
        """翻译音频"""
        try:
            if config.SHOW_PERFORMANCE:
                start_time = time.time()
            
            # 更新最后一次音频时间
            current_time = time.time()
            silence_duration = current_time - self.last_audio_time
            self.last_audio_time = current_time
            
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
                language=config.WHISPER_LANGUAGE,
                task=config.WHISPER_TASK,
                beam_size=config.WHISPER_BEAM_SIZE,
                temperature=config.WHISPER_TEMPERATURE,
                vad_filter=False,
                condition_on_previous_text=False,
                initial_prompt=None,  # 移除initial_prompt，避免被当作识别结果
            )
            
            # 收集识别结果
            full_text = ""
            for segment in segments:
                text = segment.text.strip()
                if text:
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
                    
                    # 条件2：静音时间超过0.5秒
                    if silence_duration > 0.5:
                        last_sentence_stable = True
                        if config.SHOW_PERFORMANCE:
                            print(f"   ✅ 最后一句稳定（静音{silence_duration:.1f}秒）")
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
                
                # 简化调试输出
                pass
                        
            elif len(sentences) == 2:
                # 跳过第一句，最后一句根据稳定性
                if last_sentence_stable:
                    sentences_to_translate = [sentences[1]]
                else:
                    sentences_to_translate = []
                
                # 简化调试输出
                pass
                        
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
                english_context = " ".join([item['text'] for item in self.sentence_queue])
                
                if config.SHOW_PERFORMANCE:
                    print(f"   🔤 翻译(合并): {combined_sentence[:50]}...")
                
                translation = self._translate_single_sentence(combined_sentence, english_context)
                
                if translation:
                    # 标记所有短句子为已翻译（使用相同的翻译）
                    for item in untranslated_short_sentences:
                        item['is_translated'] = True
                        item['translation'] = translation
                    
                    # 添加翻译结果
                    new_translations.append(translation)
            
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
                
                # 简化调试输出
                pass
                
                if similar_item:
                    # 找到相似句子
                    if similar_item['is_translated']:
                        # 已翻译，跳过
                        continue
                    # 未翻译，使用这个item
                else:
                    # 没有相似句子，添加到队列
                    similar_item = self._add_sentence_to_queue(sentence)
                
                # 步骤4：翻译句子（使用队列中的英文原文作为上下文）
                # 获取最近的英文上下文（最多10条）
                english_context = " ".join([item['text'] for item in self.sentence_queue])
                
                if config.SHOW_PERFORMANCE:
                    print(f"   🔤 翻译({word_count}词): {sentence}")
                
                translation = self._translate_single_sentence(sentence, english_context)
                
                if translation:
                    # 标记为已翻译并保存
                    similar_item['is_translated'] = True
                    similar_item['translation'] = translation
                    new_translations.append(translation)
            
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
    
    def __del__(self):
        """清理资源"""
        try:
            if hasattr(self, 'model'):
                del self.model
            if hasattr(self, 'ollama_session'):
                self.ollama_session.close()
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except:
            pass

