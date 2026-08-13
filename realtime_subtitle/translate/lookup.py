"""点词查词 + 🤖 AI 分析（背景总结 / 深度解释）。

从 translator_queue.py 拆出来的（2026-08-12）。原因不是"那个文件行数多"，
而是**一个类里有 5 把锁和 4 个 executor**，谁能和谁并发、哪个锁不能套哪个锁
全靠注释维护。这一块是其中最独立的：它只用自己的两个 executor、自己的两个
requests.Session 和自己的缓存，和字幕主链路（识别→切句→翻译队列）唯一的
交集就是 `_lookup_inflight_n`（草稿翻译看它让路）。

☠️ 拆成 mixin 而不是独立对象，是为了**零调用点改动**——`WhisperQueueTranslator`
的方法名、`self.` 上的状态、测试里 `WhisperQueueTranslator._lookup_worker(t, ...)`
这种直接拿未绑定方法调的写法全都照旧。UI 那边的 `WindowChromeMixin` /
`LiveTextRenderMixin` 就是同一套做法，沿用它。

☠️ 本 mixin **不自己 __init__**，下列状态由 `WhisperQueueTranslator.__init__`
建好（和 UI 那两个 mixin 依赖 SubtitleWindow.__init__ 是同一个约定）：

    self.closing              退出中标志（所有 worker 的出口检查）
    self._ollama_hot          查词成功也算"模型在显存里"，会回写它
    self.lookup_session       查词专属 requests.Session
    self.analysis_session     AI 分析专属 requests.Session
    self._lookup_executor     查词 worker（max_workers=1）
    self._analysis_executor   AI 分析 worker（max_workers=1，和查词分开是为了
                              避免 30 秒超时的分析卡在 15 秒超时的查词前面）
    self._lookup_cache        OrderedDict，真 LRU
    self._lookup_cache_lock   保护上面那个
    self._LOOKUP_CACHE_MAX    容量
    self._lookup_seq          只在 UI 线程递增的点击序号
    self._lookup_inflight_n   在飞的用户请求计数（草稿看它让路）
    self._inflight_lock       保护上面那个

`shutdown()` 里对这两个 executor 的有界排干、以及 `_save_lookup_cache()` 的
调用时机，都仍然在 translator_queue 那边——顺序是有讲究的（CLAUDE.md 第 4 节
第 13 条：任何"退出前卸载 Ollama 模型"的路径都要先等在飞的请求落地）。
"""
import json
import os
import re
import time

from realtime_subtitle.paths import repo_path
import realtime_subtitle.config as config

# 单次流式响应累计的字符硬顶。
# ☠️ num_predict 只是**请求**参数，服务端愿不愿意听是另一回事：模型钻进复读
# 循环、或者 11434 端口被别的进程占了（Ollama 没起来时任何本地程序都能占），
# iter_lines() 就会一直把内容往 parts 里堆，内存跟着涨、翻译 worker 也永远
# 不返回。一条字幕的中文再长也就几百字，20000 是留足余量的保险丝。
_MAX_STREAM_CHARS = 20000


def lookup_language_for(word):
    """点了这个词，该按哪个语言去查词典。

    ☠️ 不能一律用 `config.SOURCE_LANGUAGE`。加了中→德之后，源语言是中文时
    **德语出现在译文行**——点它本来是想查德语单词，可 SOURCE_LANGUAGE 是 zh，
    prompt 就成了"你是中文汉词典。简明解释中文单词 Kameraqualität"，模型只能
    瞎编；缓存键也会记成 zh，把两种语言的同形词混进同一格。

    判据用字符集而不是"点在第几行"：UI 那边本来就只放行纯拉丁字母的词
    （subtitle_render._on_label_click），所以这里只需回答"当前语言对里哪个是
    拉丁语言"。源语言不是无空格书写系统时就返回它自己——德/英→中的老行为
    一字不变。
    """
    from realtime_subtitle.translate.translator_queue import (
        _no_space_language, current_target_language,
    )
    src = config.SOURCE_LANGUAGE
    if not _no_space_language(src):
        return src
    is_latin = bool(word) and all(ord(c) < 0x2E80 for c in word)
    return current_target_language() if is_latin else src


# ------------------------------------------------------------------
# AI 分析纯函数（时间窗过滤 / prompt / URL）——无副作用，单测直接 import
# ------------------------------------------------------------------

# 跳网页时德语原文片段封顶，避免整段上下文把 URL 撑爆（某些浏览器/系统有上限）
AI_WEB_FRAGMENT_MAX_CHARS = 300


def filter_recent_german_context(sentence_pairs, now=None, window_minutes=None,
                                 max_chars=None):
    """从 sentence_pairs [(german, chinese, ts), ...] 取最近 N 分钟德文，
    按时间顺序拼接；超 max_chars 从最旧开始丢，保留最近内容。无命中返回 ""。"""
    if now is None:
        now = time.time()
    if window_minutes is None:
        window_minutes = getattr(config, "AI_CONTEXT_WINDOW_MINUTES", 5)
    if max_chars is None:
        max_chars = getattr(config, "AI_CONTEXT_MAX_CHARS", 2000)
    cutoff = now - float(window_minutes) * 60.0
    # 保持原列表顺序（时间升序 append）；只取窗口内有德文的
    pieces = []
    for item in sentence_pairs:
        if len(item) < 3:
            continue
        german, _chinese, ts = item[0], item[1], item[2]
        if ts is None or ts < cutoff:
            continue
        g = (german or "").strip()
        if g:
            pieces.append(g)
    if not pieces:
        return ""
    # 从最旧往最新拼；超限时丢最旧
    text = " ".join(pieces)
    if len(text) <= max_chars:
        return text
    # 从尾部保留：丢掉最旧的 piece 直到塞得下
    kept = []
    total = 0
    for g in reversed(pieces):
        add = len(g) + (1 if kept else 0)
        if total + add > max_chars and kept:
            break
        if total + add > max_chars and not kept:
            # 单句就超上限：硬截最近一句的尾部（保留"最近"）
            kept.append(g[-max_chars:])
            break
        kept.append(g)
        total += add
    kept.reverse()
    return " ".join(kept)


def build_background_summary_prompt(german_text):
    """🤖 背景总结：3-5 句中文讲最近在聊什么。"""
    lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)
    return (
        f"/no_think 你在帮一位正在看{lang_name}直播/视频的中文用户快速跟上背景。"
        f"下面是最近几分钟的{lang_name}字幕原文（可能有识别误差）。\n\n"
        f"{german_text}\n\n"
        "请用中文写 3-5 句话：这段时间在讲什么主题、关键人物/事件、"
        "需要知道的背景。不要逐句翻译，不要列表，不要开场白。"
    )


def build_deep_explain_prompt(sentence):
    """点词升级：整句背景/含义/俚语双关，比单词释义更展开。"""
    lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)
    return (
        f"/no_think 你在帮一位中文用户理解一句{lang_name}对白的深层含义。\n"
        f"原句：{sentence}\n\n"
        "请用中文解释：\n"
        "1) 这句话在说什么（自然通顺的释义）\n"
        "2) 背景/语境上可能指什么（人物、事件、文化）\n"
        "3) 若有俚语、双关、讽刺、固定搭配，单独点出\n"
        "控制在 6-10 句以内，不要逐词拆解成词典条目，不要开场白。"
    )


# ☠️ 这两条以前把"德语"写死在问句里。同一个文件里另外两个 build_*_prompt
# 都老老实实按 LANGUAGE_NAMES 取当前源语言，只有出网这两条漏了——中→德时
# 会问出"请帮我解释这句德语的背景：「一段中文」"，把外面那个模型直接带偏。
def build_web_query_for_background(source_text, max_fragment_chars=AI_WEB_FRAGMENT_MAX_CHARS):
    frag = (source_text or "")[:max_fragment_chars]
    lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)
    return f"请帮我总结并解释这段{lang_name}直播内容的背景：「{frag}」"


def build_web_query_for_sentence(sentence, max_fragment_chars=AI_WEB_FRAGMENT_MAX_CHARS):
    frag = (sentence or "")[:max_fragment_chars]
    lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)
    return f"请帮我解释这句{lang_name}的背景：「{frag}」"


def ai_web_enabled():
    """「🌐 问更强的AI」是否可用。

    这是整个程序唯一会把内容送出本机的路径，所以要能一键关掉：
    config_local.py 里把 AI_ANALYSIS_WEB_URL_TEMPLATE 设成空串/None 即可，
    按钮不再出现（以前设空串会变成 webbrowser.open("")，只是打开浏览器主页，
    等于"关不掉"）。
    """
    return bool((getattr(
        config, "AI_ANALYSIS_WEB_URL_TEMPLATE", "") or "").strip())


def build_ai_web_url(prompt_text):
    """把自然语言问题填进 AI_ANALYSIS_WEB_URL_TEMPLATE（{query} 已 URL-encode）。

    模板为空 = 用户关掉了这个功能，返回空串，调用方不要打开浏览器。

    ☠️ 模板是用户在 config_local.py 里随手改的（config 注释就鼓励换成
    ChatGPT 等）。只要里面出现一个不成对的 `{`、或者别的占位符名，
    `.format()` 就抛 KeyError/IndexError/ValueError——而调用点在 Qt 槽函数
    里，异常会直接冒到事件循环，按钮表现为"点了没反应"，用户根本不会去看
    stderr。坏模板一律降级成"功能关闭"（返回空串），调用方已经有对应的
    提示文案。
    """
    from urllib.parse import quote
    template = getattr(
        config, "AI_ANALYSIS_WEB_URL_TEMPLATE", "https://grok.com/?q={query}")
    template = (template or "").strip()
    if not template:
        return ""
    # ☠️ scheme 白名单：调用方是 webbrowser.open()，而 Windows 上它对"不像 URL"
    # 的字符串会退化成 os.startfile，也就是 ShellExecute——模板写成一个本地
    # 路径就会去"打开"那个文件。这不是提权路径（config_local.py 本来就是被
    # exec 的），价值纯粹是把配置写错的后果限制成"按钮不可用"而不是"点一下
    # 弹出个莫名其妙的程序"。
    if not template.lower().startswith(("http://", "https://")):
        print(f"   ⚠️  AI_ANALYSIS_WEB_URL_TEMPLATE 必须以 http:// 或 https:// 开头，"
              f"「问更强的AI」已禁用: {template[:60]!r}")
        return ""
    # ☠️ 没有 {query} 时 .format() **不会报错**，原样返回模板——于是浏览器打开
    # 一个空首页、用户刚才那句问题凭空消失，而且没有任何提示。必须显式挡住
    if "{query}" not in template:
        print(f"   ⚠️  AI_ANALYSIS_WEB_URL_TEMPLATE 里没有 {{query}} 占位符，"
              f"问题会丢失，「问更强的AI」已禁用: {template[:60]!r}")
        return ""
    try:
        return template.format(query=quote(prompt_text or "", safe=""))
    except (KeyError, IndexError, ValueError) as e:
        print(f"   ⚠️  AI_ANALYSIS_WEB_URL_TEMPLATE 格式不对（只支持 {{query}} 一个占位符）"
              f"，「问更强的AI」已禁用: {e.__class__.__name__}: {e}")
        return ""


class LookupMixin:
    """查词 + AI 分析。状态由宿主类的 __init__ 提供，见模块 docstring。"""

    # ------------------------------------------------------------------
    # 点词查词（独立worker，不占字幕翻译的队列）
    # ------------------------------------------------------------------
    def _enter_inflight(self):
        """标记一个"用户主动发起的 Ollama 请求"开始（查词/AI分析）。"""
        with self._inflight_lock:
            self._lookup_inflight_n += 1

    def _exit_inflight(self):
        """结束。计数归零才算真的没人在等——见 translator_queue._maybe_draft
        的让路条件（草稿是奢侈品，用户主动点的请求优先拿 GPU）。"""
        with self._inflight_lock:
            self._lookup_inflight_n = max(0, self._lookup_inflight_n - 1)

    @property
    def _lookup_inflight(self):
        """有任意一个用户请求在飞就是 True（_maybe_draft 读这个让路）。"""
        return getattr(self, "_lookup_inflight_n", 0) > 0

    @_lookup_inflight.setter
    def _lookup_inflight(self, value):
        """只给测试摆初始状态用。生产代码一律走 _enter/_exit_inflight——
        直接赋 False 会把并发中的另一个请求也一并清掉，正是要防的那个 bug。"""
        self._lookup_inflight_n = 1 if value else 0

    def _lookup_cache_path(self):
        """查词缓存落盘路径；config 里设成空/None 就是关闭持久化"""
        name = getattr(config, "LOOKUP_CACHE_FILE", None)
        if not name:
            return None
        return repo_path(name)  # 仓库根，和 .gitignore/uninstall.ps1 一致

    def _load_lookup_cache(self):
        """启动时读回上次的查词缓存（学德语时高频词跨会话继续秒回）。

        文件坏了/格式变了一律当没有——这是纯加速缓存，绝不能让它挡住启动。
        JSON 没有元组键，落盘格式是 [[词, 语言, 释义, 当时的句境], ...]，
        按 LRU 顺序（最旧在前）写，读回来 insert 顺序天然就是原来的 LRU。
        """
        path = self._lookup_cache_path()
        if not path or not os.path.exists(path):
            return
        try:
            with open(path, "r", encoding="utf-8") as f:
                rows = json.load(f)
            loaded = 0
            for row in rows:
                if not isinstance(row, (list, tuple)) or len(row) != 4:
                    continue
                word, lang, text, ctx = row
                if not (isinstance(word, str) and isinstance(lang, str)
                        and isinstance(text, str) and isinstance(ctx, str)):
                    continue
                self._lookup_cache[(word, lang)] = (text, ctx)
                loaded += 1
            while len(self._lookup_cache) > self._LOOKUP_CACHE_MAX:
                self._lookup_cache.popitem(last=False)
            if loaded and config.SHOW_PERFORMANCE:
                print(f"   📖 查词缓存已载入 {len(self._lookup_cache)} 条")
        except Exception as e:
            print(f"   ⚠️  查词缓存读取失败（忽略，当作空缓存）: {e}")
            self._lookup_cache.clear()

    def _save_lookup_cache(self):
        """退出时写回。先写临时文件再替换：中途断电不会留下半个坏 JSON。"""
        path = self._lookup_cache_path()
        if not path:
            return
        try:
            with self._lookup_cache_lock:
                rows = [[w, lang, text, ctx]
                        for (w, lang), (text, ctx) in self._lookup_cache.items()]
            if not rows:
                return
            tmp = path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
            os.replace(tmp, path)
            if config.SHOW_PERFORMANCE:
                print(f"   📖 查词缓存已保存 {len(rows)} 条")
        except Exception as e:
            print(f"   ⚠️  查词缓存保存失败（不影响退出）: {e}")

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

    def lookup_word(self, word, context, callback, on_partial=None):
        """查一个德语/英语单词的词典解释，完成后调 callback(word, text)。

        on_partial(word, text)（可选）：流式生成期间**整行**地把已出的内容
        推给弹窗，让"原形/词性"先上屏，不用干等整段。同样必须线程安全。

        callback 必须线程安全（SubtitleWindow.show_lookup_result 走Qt信号）。
        缓存命中在调用线程同步返回，不进 executor、不打 Ollama。
        """
        # ☠️ seq 必须在**查缓存之前**递增：命中缓存时也要让在飞的上一次查词过期。
        # 否则「点生词A(慢，流式在跑) → 点查过的词B(缓存秒回)」时 seq 不涨，
        # A 的 partial/final 全部判定为"没过期"，会一行行把 B 的结果盖掉，
        # 用户看到刚点的 B 变回 A。缓存越大越持久，这条路径越常走。
        self._lookup_seq += 1  # 只在 UI 线程递增（点击回调），worker 只读
        seq = self._lookup_seq
        cache_key = (word.lower(), lookup_language_for(word))
        if self._serve_cached_lookup(word, cache_key, context, callback):
            return
        try:
            self._lookup_executor.submit(
                self._lookup_worker, word, context, callback, seq, on_partial)
        except RuntimeError:
            pass  # 程序正在退出

    def _lookup_stale(self, seq):
        """有更新的点击了 → 这次的结果不要再弹（沿用 _tx_epoch 的代数门控思路）"""
        return seq is not None and seq != self._lookup_seq

    def _stream_lookup(self, response, word, seq, on_partial):
        """读查词的流式响应，返回最终文本；退出中/已过时返回 None。

        partial 只按【整行】推：查词结果是"原形/词性/释义/本句中"四行的固定
        格式，按 token 推会让弹窗在半个词上抖，按行推则是一行一行长出来。
        """
        parts = []
        emitted_chars = 0  # 已经推给弹窗的字符数（都落在换行边界上）
        total_chars = 0    # 累计收到的字符数（保险丝，见 _MAX_STREAM_CHARS）
        last_emit = 0.0
        for line in response.iter_lines():
            if self.closing:
                return None  # 正在退出：别等生成完，外层 finally 会 close 连接
            if self._lookup_stale(seq):
                # 用户已经点了别的词。这里**中途放弃**是有意的行为改变：
                # 非流式时代拿到的是完整文本，过时了也照样进缓存（下次秒回）；
                # 流式下半截文本进缓存只会污染词典，不如立刻断连——close 会让
                # Ollama 停止生成，把 GPU 让给用户真正在等的那个词
                return None
            if not line:
                continue
            try:
                data = json.loads(line)
            except ValueError:
                continue
            parts.append(data.get("response", ""))
            total_chars += len(parts[-1])
            if data.get("done"):
                break
            if total_chars > _MAX_STREAM_CHARS:
                print(f"   ⚠️  查词响应超过 {_MAX_STREAM_CHARS} 字符，提前截断")
                break
            if not on_partial or time.time() - last_emit <= 0.15:
                continue
            grown = "".join(parts)
            cut = grown.rfind("\n")
            if cut <= emitted_chars:
                continue  # 还没长出新的一整行
            emitted_chars = cut
            partial = re.sub(r'<think>.*?</think>', '', grown[:cut],
                             flags=re.DOTALL).strip()
            if partial:
                last_emit = time.time()
                on_partial(word, partial)
        text = "".join(parts)
        return re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()

    def _lookup_worker(self, word, context, callback, seq=None, on_partial=None):
        # ☠️ 查的是**被点那个词**的语言，不一定是 SOURCE_LANGUAGE：中→德时
        # 德语在译文行上，见 lookup_language_for。两处 cache_key 必须同源，
        # 否则 lookup_word 存的和这里查的对不上，缓存等于永不命中
        word_lang = lookup_language_for(word)
        lang_name = config.LANGUAGE_NAMES.get(word_lang, word_lang)
        cache_key = (word.lower(), word_lang)
        if self._lookup_stale(seq):
            return  # 排队期间用户已经点了别的词，这次白跑，连请求都不用发
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
        self._enter_inflight()  # 草稿翻译看这个让路，见 translator_queue._maybe_draft
        try:
            t0 = time.time()
            # 用查词专属session：和翻译线程共享一个requests.Session
            # 并发使用不保证线程安全
            response = self.lookup_session.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.OLLAMA_MODEL,
                    "prompt": prompt,
                    # 流式：整行整行地往弹窗推，"原形/词性"先上屏。
                    # 2026-08-04 实测（直播翻译流在跑）：首行可见 2.4 秒，
                    # 整段落地 3.3~4.0 秒——省掉最后那 1 秒多的空白等待
                    "stream": True,
                    "think": False,
                    # 不设的话 Ollama 默认5分钟无请求就卸载模型，
                    # 安静段/暂停后第一句要付~9秒冷加载
                    "keep_alive": "2h",
                    # num_predict 220→170（2026-08-04实测）：220 和 170 的查词
                    # 耗时差异很小，170 只省一点最坏情况下的生成时间，格式
                    # （原形/词性/释义最多2条/本句中一句话）仍够用不至于截断。
                    # ⚠️ 当初记的"瓶颈是prompt处理+固定开销"归因是错的——那个
                    # "固定开销"其实是下面 num_ctx 不一致导致的 7 秒模型重载，
                    # 实测生成本身只占 ~1 秒（40~60 token）。别再来砍这个值了
                    "options": {
                        "temperature": 0.2,
                        "num_predict": 170,
                        # ☠️ 必须和翻译请求用同一个值，见 config.OLLAMA_NUM_CTX：
                        # 这里曾写死 2048（翻译是 4096），每次点词都让 Ollama
                        # 重装 5.6GB 模型，实测每次多付 6.9~8.7 秒
                        "num_ctx": getattr(config, "OLLAMA_NUM_CTX", 4096),
                    },
                },
                stream=True,
                # 流式下 timeout 是"相邻数据块间隔"上限，不是总时长
                timeout=15,
            )
            try:
                if response.status_code == 200:
                    self._ollama_hot = True  # 查词成功也证明模型在显存里
                    text = self._stream_lookup(response, word, seq, on_partial)
                    if text is None:
                        return  # 退出中/已过时，_stream_lookup 里已经短路
                    if config.SHOW_PERFORMANCE:
                        print(f"   📖 查词 {word} {time.time() - t0:.1f}秒")
                    if text:
                        # 结果过时也照样进缓存（下次点这个词就秒回），只是不弹窗
                        self._lookup_cache_put(cache_key, (text, context))
                    if self._lookup_stale(seq):
                        return
                    callback(word, text or "（没查到）")
                elif not self._lookup_stale(seq):
                    callback(word, f"查询失败（HTTP {response.status_code}）")
            finally:
                # stream=True 的连接不 close 不会归还连接池（和翻译侧同一个坑）
                response.close()
        except Exception as e:
            if not self.closing and not self._lookup_stale(seq):
                callback(word, f"查询失败: {e}")
        finally:
            self._exit_inflight()

    # ------------------------------------------------------------------
    # AI 分析（背景总结 / 整句深度解释）——独立 executor，既不占翻译队列，
    # 也不挡查词（30秒超时的分析卡在查词前面是实打实的队头阻塞）
    # ------------------------------------------------------------------
    def analyze_background(self, german_text, callback):
        """最近 N 分钟内容的背景总结。callback(text) 必须线程安全。"""
        try:
            self._analysis_executor.submit(
                self._analyze_background_worker, german_text, callback)
        except RuntimeError:
            pass  # 程序正在退出

    def _analyze_background_worker(self, german_text, callback):
        prompt = build_background_summary_prompt(german_text)
        self._run_ai_analysis_request(
            prompt, callback, num_predict=300, label="背景总结")

    def deep_explain(self, sentence, callback):
        """整句深度解释（比查词更展开）。callback(text) 必须线程安全。"""
        try:
            self._analysis_executor.submit(
                self._deep_explain_worker, sentence, callback)
        except RuntimeError:
            pass

    def _deep_explain_worker(self, sentence, callback):
        prompt = build_deep_explain_prompt(sentence)
        self._run_ai_analysis_request(
            prompt, callback, num_predict=400, label="深度解释")

    def _run_ai_analysis_request(self, prompt, callback, num_predict=400, label="分析"):
        """共用 Ollama /api/generate 路径：失败只改弹窗文案，不重试、不打扰主链路。"""
        self._enter_inflight()  # 草稿翻译看这个让路，见 translator_queue._maybe_draft
        try:
            t0 = time.time()
            response = self.analysis_session.post(
                f"{config.OLLAMA_BASE_URL}/api/generate",
                json={
                    "model": config.OLLAMA_MODEL,
                    "prompt": prompt,
                    "stream": False,
                    "think": False,
                    "keep_alive": "2h",
                    "options": {
                        "temperature": 0.3,
                        "num_predict": int(num_predict),
                        "num_ctx": getattr(config, "OLLAMA_NUM_CTX", 4096),
                    },
                },
                timeout=30,
            )
            if self.closing:
                return
            if response.status_code == 200:
                self._ollama_hot = True
                text = response.json().get("response", "").strip()
                text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
                if config.SHOW_PERFORMANCE:
                    print(f"   🤖 {label} {time.time() - t0:.1f}秒")
                callback(text or "（没有分析结果）")
            else:
                callback(f"分析失败（HTTP {response.status_code}）")
        except Exception as e:
            if not self.closing:
                callback(f"分析失败: {e}")
        finally:
            self._exit_inflight()
