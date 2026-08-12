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
import socket
import ipaddress
from urllib.parse import urlparse
from threading import Event, Lock, Thread
from concurrent.futures import ThreadPoolExecutor
from collections import deque, OrderedDict

import requests

from realtime_subtitle.asr.streaming_asr import OnlineASRProcessor
from realtime_subtitle.paths import repo_path
import realtime_subtitle.config as config
# 过滤所有警告信息
warnings.filterwarnings("ignore")
logging.basicConfig(level=logging.ERROR)

# 句子结束符：只认 .!?（旧管线按逗号切句是碎句/上下文错乱的来源之一）。
# 切分时要按位置往后扫，不能每次都从头 match，所以只留匹配终止符本身的这一条
# （成对的 _SENTENCE_END 在改成 finditer 之后就没有调用点了，已删）
_SENTENCE_TERMINATOR = re.compile(r'[.!?…]["»«\']?(?=\s|$)')

_PUNCT_STRIP = " \t.!?…,;:–—\"'«»„“”"
_QUOTE_CHARS = "\"'«»„“”"

# 单次流式响应累计的字符硬顶。
# ☠️ num_predict 只是**请求**参数，服务端愿不愿意听是另一回事：模型钻进复读
# 循环、或者 11434 端口被别的进程占了（Ollama 没起来时任何本地程序都能占），
# iter_lines() 就会一直把内容往 parts 里堆，内存跟着涨、翻译 worker 也永远
# 不返回。一条字幕的中文再长也就几百字，20000 是留足余量的保险丝。
_MAX_STREAM_CHARS = 20000


def _ends_with_terminator(text):
    """文本是不是正好停在一个句子终止符上（判断"要不要扣留"用）。"""
    return bool(text) and text.rstrip().rstrip(_QUOTE_CHARS).endswith((".", "!", "?", "…"))


# 模型偶尔在译文后面追加"（注：……）"——句子被截断时最容易触发（实测
# "US-Präsident Trump hat dem Iran..." 收到一整段"建议补全后半句"的说明）。
# prompt 里已经明说不要加，这里是兜底：字幕条上多这么一坨没人想看。
# num_predict 截断会让右括号丢掉，所以右括号是可选的。
_TRANSLATOR_NOTE = re.compile(r'[（(]\s*(?:译注|注|说明|备注)\s*[：:][^）)]*[）)]?\s*$')
# 译注不只出现在末尾：2026-08-04 复核 23157 条真实句对，实测有"多特蒙德（注：
# 此处应为杜塞尔多夫）住过、干过活儿"这种夹在句子中间的。中间的那种右括号一定
# 在（没被 num_predict 截断），所以这条要求右括号闭合，不会误吃掉正文
_TRANSLATOR_NOTE_INLINE = re.compile(r'[（(]\s*(?:译注|注|说明|备注)\s*[：:][^）)]*[）)]')


def _strip_translator_note(text):
    """去掉译文里的译注；如果整条都是译注就原样返回（宁可多显示不可显示空白）。

    先去中间的（要求括号闭合），再去末尾的（右括号可选——生成被
    num_predict 截断时右括号会丢）。
    """
    cleaned = _TRANSLATOR_NOTE_INLINE.sub("", text)
    cleaned = _TRANSLATOR_NOTE.sub("", cleaned).strip()
    return cleaned or text.strip()


# 德语时间是"点"分隔（19.10 Uhr = 19:10），模型经常把这个点当小数点或序数点，
# 实测错法有三种：19.10→"晚上九点"（小时算错）、23.30→"十一点"（丢了分钟）、
# 21 .43→"0点43分"（ASR 在数字间插了空格，模型彻底读歪）。
# prompt 里那条"时间是24小时制不要改"的硬约束挡不住，因为歧义在输入端。
# 送去翻译前先归一化成 19:10 Uhr——冒号在任何语言里都只可能是时间，
# 模型没有可误读的余地。只动喂给模型的文本，屏幕上和存档里的德语原文不变。
_DE_CLOCK = re.compile(r'\b([01]?\d|2[0-3])\s*\.\s*\.?\s*([0-5]\d)(?=\s*Uhr\b)')


def _normalize_clock_times(text):
    """德语 'HH.MM Uhr' / 'HH .MM Uhr' → 'HH:MM Uhr'。非德语时间格式不动。"""
    if not text:
        return text
    return _DE_CLOCK.sub(lambda m: f"{m.group(1)}:{m.group(2)}", text)


def _boundary_is_real(candidate, remainder, final):
    """这个句号/问号是真句尾，还是 Whisper 打错的？

    2026-08-02 统计 19 个转录文件共 23526 句：**38.5% 的翻译单元以小写德语词
    开头**——德语句首必大写，所以那都是上一句在非句尾处被切开的碎片。碎片单独
    送翻译会翻错（实测 "Demnach darf, wer schwimmend bzw." 被译成"均不得……"，
    否定词其实在下一段）。三条否决规则：
    """
    tail = candidate.rstrip(_QUOTE_CHARS)
    if tail.endswith("."):
        words = tail[:-1].split()
        token = words[-1] if words else ""
        # ① 缩写否决：德语 bzw./z.B./ca. 这类缩写自带句号
        if token.lower().strip(_QUOTE_CHARS) in config.SENTENCE_ABBREVIATIONS:
            return False
        # ② 序数否决："am 3. Mai" 这种日期序数自带句号——注意德语名词首字母
        #    大写，"Mai" 是大写的，规则③抓不到这种，必须单独否决。
        #    ☠️ 只否决 1-2 位数（能当序数/日期的范围）。以前这里是无条件
        #    `token.isdigit()`，于是**四位年份/大数结尾的句子永远不成句**：
        #    "Der Vertrag läuft bis 2030." 连 final=True 都放不出来（这条
        #    return 在 `if not remainder: return final` 之前），只能等
        #    IDLE_FLUSH_SEC 兜底，或者和下一句合并成一个翻译单元——中文因此
        #    整整晚一句。新闻直播是主场景，年份/金额结尾极常见。
        #    四位数几乎不可能是序数，两位数（"Es waren genau 20."）仍按老规则
        #    保守合并，代价只是两句合成一次请求。
        if token.isdigit() and len(token) <= 2:
            return False
    if not remainder:
        # ③b 句尾扣留：后面还没有词，看不出这个句号是真是假。
        # final=True（收尾/有界放行）时不再等，照常成句
        return final
    # ③a 续行否决：下一个词是小写 = 上一句还没说完
    return not remainder[0].islower()


def _split_sentences(text, final=False):
    """切出完整句子 + 剩余残句。返回 (sentences, rest)。

    模块级纯函数：单测直接测它，不用在测试里复制一份切分逻辑（复制必然漂移）。
    """
    sentences = []
    start = 0
    for m in _SENTENCE_TERMINATOR.finditer(text):
        end = m.end()
        candidate = text[start:end].strip()
        if not candidate:
            continue
        remainder = text[end:].lstrip()
        if not _boundary_is_real(candidate, remainder, final):
            continue  # 不是真句尾：跳过这个终止符，接着往后找
        sentences.append(candidate)
        start = end
    return sentences, text[start:].strip()


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


def build_web_query_for_background(german_text, max_fragment_chars=AI_WEB_FRAGMENT_MAX_CHARS):
    frag = (german_text or "")[:max_fragment_chars]
    return f"请帮我总结并解释这段德语直播内容的背景：「{frag}」"


def build_web_query_for_sentence(sentence, max_fragment_chars=AI_WEB_FRAGMENT_MAX_CHARS):
    frag = (sentence or "")[:max_fragment_chars]
    return f"请帮我解释这句德语的背景：「{frag}」"


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

_warm_thread = None  # 启动预热线程句柄：_unload_our_models 退出时要等它收尾
# 预热线程实际装进显存的那个模型名。☠️ 不能事后现读 config.OLLAMA_MODEL：
# 窗口是秒开的、⚙️面板在模型加载的十几秒里就能点，用户切「性能」模式会把
# config.OLLAMA_MODEL 改掉，而预热早就按旧名字发出去了（见 __init__ 末尾的对账）
_warm_model = None
# 预热落地信号：首句翻译等它，别自己另开一次冷加载还撞上 15 秒超时
# （2026-08-02 实测：开机冷读 5.6GB 模型花了 33.8 秒，其间两句翻译被超时丢弃）
_warm_done = Event()
_warm_ok = False  # 预热是否真的成功（失败也要 set 事件，但不能当模型已热）


class RemoteOllamaRefused(RuntimeError):
    """OLLAMA_BASE_URL 指向本机之外，且用户没有显式声明这是有意的。"""


def _assert_local_ollama(base_url):
    """☠️ 拦住"把转录发到本机之外"的配置。返回 True 表示通过（给测试用）。

    README 第一句就是「不向任何云端发送音频或文本」，但在这个函数之前，
    真正保证这件事的只有 config.py 里那一行默认值——而 config_local.py 是被
    `exec_module` 加载的，CLAUDE.md 第 2 节还明确鼓励让 AI 助手去写它。
    一个手滑（或一段被投毒）的 OLLAMA_BASE_URL 就会把**系统全部声音的转录**
    连同上下文一路 POST 到外网，而屏幕上和日志里不会有任何异样：翻译照常出
    中文，用户完全无从察觉。这是本项目唯一一条能静默违反自身隐私承诺的路径。

    所以这里是**硬失败**而不是警告——静默泄露比"字幕起不来"严重得多，而且
    起不来是看得见的，能被修。真要连局域网里另一台机器的 Ollama（确实有人
    这么用）：在 config_local.py 里写 `ALLOW_REMOTE_OLLAMA = True` 显式声明，
    那就是知情同意，本函数直接放行。

    ☠️ 必须在 `_spawn_startup_warm()` 之前调用——预热请求本身就打这个地址。
    虽然预热的 prompt 是空的（不含转录内容），但"确认过是本机才发第一个包"
    是更容易讲清楚的语义。

    解析不出来时放行：那说明请求本来也发不出去，交给 __init__ 里的连通性
    检查去报，在这里拦只会把"Ollama 没起来"升级成"程序起不来"。
    """
    if getattr(config, "ALLOW_REMOTE_OLLAMA", False):
        return True
    host = urlparse(base_url).hostname
    if not host:
        return True
    try:
        infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
    except OSError:
        return True
    remote = set()
    for info in infos:
        # IPv6 可能带 %scope 后缀（fe80::1%eth0），ip_address 不认
        addr = info[4][0].split("%")[0]
        try:
            if not ipaddress.ip_address(addr).is_loopback:
                remote.add(addr)
        except ValueError:
            continue
    if not remote:
        return True
    raise RemoteOllamaRefused(
        f"OLLAMA_BASE_URL 指向本机之外的地址：{host} → {', '.join(sorted(remote))}。\n"
        f"   本程序会把**识别出的全部原文**（抓的是系统全部声音，可能含语音通话）"
        f"发到这个地址，\n"
        f"   这与 README「不向任何云端发送音频或文本」的承诺冲突，因此拒绝启动。\n"
        f"   → 正常情况：把 config_local.py 里的 OLLAMA_BASE_URL 改回 "
        f"http://127.0.0.1:11434\n"
        f"   → 确实想用另一台机器上的 Ollama：在 config_local.py 里加一行 "
        f"ALLOW_REMOTE_OLLAMA = True")


def _warn_if_ipv6_first_host(base_url):
    """☠️ 主机名解析成 IPv6 在前、IPv4 在后 ⇒ 每个请求白付约 2 秒。

    Ollama 默认只监听 IPv4 127.0.0.1:11434，而 Windows 上 "localhost" 解析出
    ::1 在前。IPv6 环回**不会快速失败**（实测 2021ms 才拒绝），之后才回退到
    IPv4——这 2 秒是每个请求的固定税。config.py 的默认值已经是 127.0.0.1，
    这个检查是为了兜住在 config_local.py 里写回主机名的情况。
    返回 True 表示发了警告（给测试用）。纯观测，不改用户配置。
    """
    try:
        host = urlparse(base_url).hostname
        if not host:
            return False
        infos = socket.getaddrinfo(host, None, 0, socket.SOCK_STREAM)
        families = [i[0] for i in infos]
        if not families or families[0] != socket.AF_INET6:
            return False
        if socket.AF_INET not in families:
            return False  # 只有 IPv6：服务大概真在 IPv6 上，别乱报
    except Exception:
        return False  # 解析不了是别的问题，交给下面的连通性检查报
    print(f"⚠️  OLLAMA_BASE_URL 用的是主机名 {host}，它解析出 IPv6(::1) 在前。")
    print(f"   Ollama 只监听 IPv4，Windows 的 IPv6 环回要约 2 秒才拒绝——")
    print(f"   每个翻译/查词请求都会白等这 2 秒。改成 http://127.0.0.1:11434")
    return True


def _spawn_startup_warm():
    global _warm_thread, _warm_model
    _warm_done.clear()
    # 在 spawn 的这一刻定下预热哪个模型，并记下来供后面对账
    _warm_model = config.OLLAMA_MODEL
    _warm_thread = Thread(target=_startup_warm_ollama, args=(_warm_model,),
                          daemon=True, name="OllamaWarm")
    _warm_thread.start()


def _startup_warm_ollama(model=None):
    """启动时后台预热翻译模型（prompt 留空 = Ollama 只加载不生成，官方用法）。

    刻意用独立的 requests.post 而不是 self.ollama_session：这个线程和
    __init__ 里的健康检查/后续翻译并发，requests.Session 跨线程并发不安全。

    model 由 _spawn_startup_warm 在 spawn 时定死并传进来，不在这里现读
    config.OLLAMA_MODEL——否则"预热到底装了哪个模型"这件事会随用户在加载
    期间切模式而变，退出对账就对不上了。"""
    global _warm_ok
    model = model or config.OLLAMA_MODEL
    try:
        t0 = time.time()
        requests.post(
            f"{config.OLLAMA_BASE_URL}/api/generate",
            json={"model": model, "prompt": "", "keep_alive": "2h"},
            timeout=120,  # 冷加载可能要十几秒，网络栈慢时再宽些
        ).close()
        _warm_ok = True
        print(f"🔥 翻译模型 {model} 后台预热完成 {time.time() - t0:.1f}秒")
    except Exception:
        pass  # Ollama 不可达由 __init__ 的健康检查负责提示
    finally:
        # ☠️ 成功失败都要 set：等待方（_await_model_ready）否则会白等满超时
        _warm_done.set()


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

    # ☠️ transformers 只是 ctranslate2「模型格式转换」的可选依赖，本项目运行时
    # 完全用不到，但只要装着（optimum 连带装的），ctranslate2 import 就会把它
    # 整个拉起来——实测占 faster_whisper 导入 3.9 秒里的 3.2 秒。放个 None 挡住
    # （converters/transformers.py 对 ImportError 有 try/except 守卫，安全），
    # faster_whisper 导入降到 0.1 秒。以后若真要在本进程用 transformers，删这行
    sys.modules.setdefault("transformers", None)

    from faster_whisper import WhisperModel
    _WhisperModel = WhisperModel
    return _WhisperModel


class WhisperQueueTranslator:
    """local agreement 增量识别 + 异步 Ollama 翻译"""

    # 上一条存档写在哪一天（跨天要重跑保留期清理，见 _save_transcript）。
    # 放类属性而不是只在 __init__ 里赋值：单测习惯用 __new__ 造个壳只塞它关心的
    # 几个字段，__init__ 中途失败时也一样是半成品——存档路径不该因此炸
    _transcript_day = None
    # 11434 后面确实是 Ollama 吗（见 _check_ollama_identity）。同样放类属性。
    # 默认是"已验证、无需重验"，所以正常路径一次额外请求都不会发
    _ollama_impostor = False         # 上次校验的结论是"有回应但不是 Ollama"
    _ollama_recheck_pending = False  # 连接断过/启动时没验成，恢复后要重新确认
    _ollama_verify_next = 0.0        # 下次允许重新校验的时刻（节流）

    def __init__(self):
        """初始化翻译器"""
        # ☠️ 第一件事：确认翻译地址在本机。放在所有 print / 模型加载 / 预热
        # 之前，是为了"一个包都还没发出去"就把不合规的配置挡下来。
        # 抛出的异常由 app._load_models 接住，报错会持久显示在悬浮窗上
        _assert_local_ollama(config.OLLAMA_BASE_URL)

        print("🔄 正在加载 Faster-Whisper 模型...")
        print(f"   模型: {config.WHISPER_MODEL}")
        print(f"   计算类型: {config.WHISPER_COMPUTE_TYPE}")

        start_time = time.time()

        # 并行预热 Ollama 翻译模型：停止脚本会主动卸载模型，所以每次启动
        # 第一句翻译都要付 5-9 秒冷加载费。趁 Whisper 加载的这十几秒让
        # Ollama 同时把模型装进显存，首句翻译就是热的。独立线程+独立请求，
        # 不碰 self（此时实例还没建完）；失败静默——Ollama 不可达时
        # 下面的健康检查会给出明确提示，这里不重复报。
        # 显存说明：两个模型在稳态本来就要同时驻留（各档位模型就是按这个
        # 选的），并行加载不会推高稳态峰值，小显存档也不用禁用并行
        _spawn_startup_warm()

        WhisperModel = _ensure_ml_deps()

        try:
            # 先只认本地缓存：默认路径每次启动都去 HuggingFace 做一轮
            # etag 检查（实测热缓存下多花 1.4 秒，网络差时是十几秒超时）。
            # 只有本地没有模型（首次运行）才回落到网络下载
            try:
                self.model = WhisperModel(
                    config.WHISPER_MODEL,
                    device=config.WHISPER_DEVICE,
                    compute_type=config.WHISPER_COMPUTE_TYPE,
                    local_files_only=True,
                )
            except Exception as e:
                # 不只是"没缓存"会走到这（CUDA错/缓存损坏也会），把真实原因
                # 带上——否则驱动问题会被误报成"在下载"，排障方向全错
                print(f"   本地缓存不可用({e.__class__.__name__}: {e})，"
                      f"尝试从网络下载模型（首次需要几分钟）...")
                self.model = WhisperModel(
                    config.WHISPER_MODEL,
                    device=config.WHISPER_DEVICE,
                    compute_type=config.WHISPER_COMPUTE_TYPE,
                )
            self.processor = OnlineASRProcessor(self.model)

            # committed 但还没凑成完整句子的德语残句
            self.pending_text = ""
            # 句尾扣留起点（0=没扣着东西）：见 _extract_sentences / _release_held_boundary
            self._held_since = 0.0
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
            self._asr_busy = False      # process_iter 在GPU上跑的期间为True（草稿让路）
            self._idle_flushed = False  # 本轮空闲是否已 flush 过（防每秒空跑 finish）
            self._inbox_dropped = 0     # 硬顶丢块累计（正常永远是0）
            self._inbox_drop_warned = 0
            # 收件箱长度的无锁快照：翻译线程要用它判断"GPU 正被 ASR 占着"。
            # 单独存一个 int 而不是让翻译线程去拿 _asr_lock——那会引入
            # _tx_lock/_asr_lock 的锁序问题，而这里只需要一个近似值
            # （CPython 里 int 读写本身是原子的，读到旧一拍无所谓）
            self._asr_backlog_n = 0
            # 待切换源语言（热键写入；ASR 每批处理前抢占执行，避免 inbox
            # 循环不返回时 submit(task) 永远排在后面饿死）
            self._pending_lang_switch = None
            self._asr_executor = ThreadPoolExecutor(max_workers=1)

            # 翻译队列：ASR线程往里放完整句子，翻译worker每次醒来把积压的全部
            # 合并成一次Ollama请求（自带积压治理，说话快时翻译永远追得上）
            self._tx_lock = Lock()
            self._tx_queue = []      # 已入队待翻译
            self._tx_inflight = []   # 正在翻译中
            self._tx_epoch = 0       # 切语言时+1：在飞的翻译完成时代数不符就丢弃
            # 最近一次翻译超时后，"慢"状态保持到这个时刻为止（见 _translate_timeout）
            self._tx_slow_until = 0.0
            self.closing = False     # shutdown置True：所有worker出口不再回调UI
            self._tx_executor = ThreadPoolExecutor(max_workers=1)
            # 点词查词单独一个worker：查词典不该排在字幕翻译后面等
            self._lookup_executor = ThreadPoolExecutor(max_workers=1)
            # 🤖 背景总结/深度解释再单独一个：它们和查词曾共用一个
            # max_workers=1 的池，而 AI 分析的 HTTP 超时是 30 秒（查词 15 秒）。
            # 后果是"点了深度解释再点个词"，查词要在队列里干等最多 30 秒才发出
            # ——_lookup_stale 只能让排队中的请求最后不发，取消不了队头阻塞。
            # 拆开之后两者互不排队（Ollama 侧仍串行生成，但那是秒级不是 30 秒）
            self._analysis_executor = ThreadPoolExecutor(max_workers=1)
            # 查词 LRU：重复点同一词零 Ollama 成本（精听高频）；
            # OrderedDict + move_to_end = 真 LRU，锁保护 UI 线程与 worker 并发
            self._lookup_cache = OrderedDict()  # (word_lower, lang) -> text
            self._lookup_cache_lock = Lock()
            # 查词序号：只认最后一次点击的结果。精听时连点好几个词会把请求排成
            # 队（单线程 worker，每个最长15秒），过时的结果回来会覆盖当前弹窗
            self._lookup_seq = 0
            self._LOOKUP_CACHE_MAX = int(getattr(config, "LOOKUP_CACHE_MAX", 200))
            self._load_lookup_cache()  # 跨会话复用；文件坏了只是当空缓存
            # 查词/AI分析请求在GPU上跑的期间 >0：草稿翻译看这个让路，
            # 别跟用户主动点的请求抢卡（2026-08-04实测：查词/分析这类一次性
            # 人工请求跟直播翻译流抢GPU会明显变慢）。
            # ☠️ 必须是**计数器**不是裸 bool：查词和 AI 分析在两个 executor 上，
            # 可以真并发。裸 bool 下"分析开始(True) → 查词开始(True) →
            # 查词结束(False)"会在分析还在飞的时候就把标志清掉，草稿提前恢复
            # 抢卡。用 _inflight_lock 保护，读侧只看 >0
            self._lookup_inflight_n = 0
            self._inflight_lock = Lock()

            # 最近已翻译的德语句子，作为翻译上下文
            self.context_history = deque(maxlen=6)

            # UI回调，由main.py接线（都必须线程安全——SubtitleWindow用Qt信号保证）
            self.on_display = None  # (committed_live, unstable) -> None
            self.on_pair = None     # (german, chinese) -> None
            self.on_draft = None    # (chinese_draft) -> None 残句的草稿中文
            self.on_status = None   # (text) -> None 状态提示（如Ollama挂了）
            self._ollama_down_notified = 0.0  # 上次提示"翻译服务未运行"的时间（节流）

            # 冷启动治理（2026-08-02）：模型没进显存前，翻译请求要等预热、用长超时、
            # 超时重试一次；否则首句直接被 15 秒超时丢掉且永远没有中文
            self._ollama_hot = False      # 拿到过一次 200 就算热
            self._warm_notified = False   # "翻译模型加载中"提示只发一次
            # 熔断：Ollama 半死（每次都超时）时别再让句子堆在队列里等
            self._tx_fail_streak = 0
            self._tx_circuit_until = 0.0
            self._tx_dropped = 0          # 队列硬顶丢弃的句数（正常永远是0）
            self._tx_drop_warned = 0

            # 分钟级性能概况（SHOW_PERFORMANCE=False 后仅剩的观测手段）
            self._stats_lock = Lock()
            self._stats_t0 = time.time()
            self._stat_asr = []       # 每轮识别耗时
            self._stat_tx = []        # 每次Ollama翻译耗时
            self._stat_buf_max = 0.0  # 音频缓冲峰值（秒）
            self._stat_merge = 0      # 合并多块处理的轮数（GPU落后的信号）
            self._stat_draft = 0      # 草稿翻译次数
            self._stat_dict = 0       # 感叹词词典直译次数（省下的Ollama请求）
            self._stat_held_release = 0  # 句尾扣留到点放行的次数
            self._stat_held_misfire = 0  # 其中放行后接了小写词的（=切在句中，切早了）
            self._release_pending = False
            # ASR 耗时分桶诊断（2026-08-04）：短/长缓冲、独占/与翻译并发
            self._stat_asr_shortbuf = []
            self._stat_asr_longbuf = []
            self._stat_asr_solo = []
            self._stat_asr_overlap = []

            # 草稿翻译节流状态（残句还没凑成完整句子时先出一版灰色中文）
            self._draft_last_time = 0.0
            self._draft_last_text = ""

            # 复用的HTTP会话：**每个 executor 独享一个**。requests.Session
            # 跨线程并发使用不保证安全，所以有几个能同时发请求的线程就要有几个
            # session。☠️ 查词和 AI 分析拆成两个 executor 之后就能并发了，
            # 再共用 lookup_session 就是真并发了——必须一起拆
            self.ollama_session = requests.Session()   # 翻译 + 草稿（同一个池，串行）
            self.lookup_session = requests.Session()   # 查词
            self.analysis_session = requests.Session()  # 🤖 背景总结 / 深度解释

            # 字幕记录（原文+译文+时间戳，每天一个文件）
            self._transcript_ok = bool(getattr(config, "SAVE_TRANSCRIPT", False))
            if self._transcript_ok:
                # 仓库根下的 transcripts\：README / uninstall.ps1 都按这个位置
                # 告诉用户，别再用 __file__ 推（会掉进 translate\ 里）
                self._transcript_dir = repo_path(config.TRANSCRIPT_DIR)
                try:
                    os.makedirs(self._transcript_dir, exist_ok=True)
                    print(f"📝 字幕记录已开启: {config.TRANSCRIPT_DIR}\\日期.txt")
                    self._prune_old_transcripts()
                    # 记下"这次清理覆盖的是哪一天"，免得第一条字幕又白清一次
                    self._transcript_day = time.strftime("%Y-%m-%d")
                except OSError as e:
                    print(f"⚠️  字幕记录目录创建失败，记录功能关闭: {e}")
                    self._transcript_ok = False

            _warn_if_ipv6_first_host(config.OLLAMA_BASE_URL)
            # 端口身份校验（不可达只警告，不中断启动）。详见 _check_ollama_identity
            state, version = self._check_ollama_identity()
            self._ollama_impostor = (state == "impostor")
            if state == "ok":
                print(f"✅ Ollama 连接正常 (v{version}, {config.OLLAMA_MODEL})")
            elif state == "impostor":
                self._warn_ollama_impostor()
            else:
                # 现在没验成（Ollama 还没起来）：等它起来之后、第一次真要发
                # 转录之前补验一次，别在"没验过"的状态下把原文发出去
                self._ollama_recheck_pending = True
                print(f"⚠️  无法连接 Ollama ({config.OLLAMA_BASE_URL})，字幕将只显示德语原文")
                print(f"   请确认 Ollama 已启动: ollama serve")

            # ☠️ 和启动预热对账：窗口是秒开的，⚙️面板在 Whisper 加载的这十几秒
            # 里就能点。用户点「⚡性能」时 translator 还是 None，main._apply_mode
            # 只能改 config.OLLAMA_MODEL——而预热线程早在本函数开头就按**旧**
            # 名字把大模型往显存里装了。不对账的话两个模型同时驻留（8GB 卡上
            # 正是这套显存分档刻意要避免的情况），要等退出才卸。
            if _warm_model and _warm_model != config.OLLAMA_MODEL:
                print(f"   ℹ️ 加载期间翻译模型已切换 {_warm_model} → {config.OLLAMA_MODEL}，"
                      f"卸掉预热的那个")
                self.request_warm_model(old_model=_warm_model,
                                        new_model=config.OLLAMA_MODEL)

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
    def _prune_old_transcripts(self):
        """启动时清掉超过 TRANSCRIPT_KEEP_DAYS 天的存档（0 = 永久保留）。

        ⚠️ 这是隐私措施，不是省磁盘：本程序抓的是系统全部声音，存档是明文，
        而且 SAVE_TRANSCRIPT 默认开着。以前没有任何保留期，装多久就攒多久。

        按**文件名**里的日期判断而不是 mtime：文件名就是 YYYY-MM-DD.txt
        （_save_transcript 的格式），而 mtime 会被同步网盘/备份工具刷新。
        认不出日期的文件一律不碰（可能是用户自己放进去的）。
        """
        # ☠️ 配置值必须容错。这个函数现在也从 _save_transcript 的跨天分支调，
        # 而那条路径在 _translation_worker 里、外面没有 try——config_local.py
        # 里写成 TRANSCRIPT_KEEP_DAYS = "30" 之类的话，一个 ValueError 就会把
        # 整个翻译 worker 打断（句对不上屏），症状和"配置写错"毫无关联
        try:
            days = int(getattr(config, "TRANSCRIPT_KEEP_DAYS", 0) or 0)
        except (TypeError, ValueError):
            print("⚠️  TRANSCRIPT_KEEP_DAYS 不是整数，本次不清理存档"
                  f"（当前值 {getattr(config, 'TRANSCRIPT_KEEP_DAYS', None)!r}）")
            return
        if days <= 0:
            return
        cutoff = time.time() - days * 86400
        removed = 0
        try:
            names = os.listdir(self._transcript_dir)
        except OSError:
            return
        for name in names:
            stem, ext = os.path.splitext(name)
            if ext.lower() != ".txt":
                continue
            try:
                ts = time.mktime(time.strptime(stem, "%Y-%m-%d"))
            except ValueError:
                continue  # 不是当天存档的命名，别动
            if ts >= cutoff:
                continue
            try:
                os.remove(os.path.join(self._transcript_dir, name))
                removed += 1
            except OSError:
                pass  # 被占用/只读：跳过，下次启动再试
        if removed:
            print(f"🧹 已清理 {removed} 个超过 {days} 天的字幕存档"
                  f"（保留期在 config.TRANSCRIPT_KEEP_DAYS，设 0 可关闭）")

    def _save_transcript(self, source_text, translation):
        """把一条字幕追加到当天的记录文件（失败一次就关闭，不刷屏）。

        translation 为空 = 这句没翻出来（Ollama 挂了/熔断中）。原文照写，
        只是不留那行空的译文——回看时能看出"这句当时没有中文"，而不是整句消失。

        ☠️ 跨天时要再跑一次 _prune_old_transcripts。以前它只在 __init__ 里调
        一次，而字幕程序的典型用法是**开机挂着不关**——也就是说设了
        TRANSCRIPT_KEEP_DAYS 的用户，只要不重启就永远不会真的清理，配置看着
        生效实际没生效。这是隐私措施不是省磁盘（存档是明文、抓的是系统全部
        声音），"以为在删其实没删"比不提供这个选项更糟。
        本函数只在 _tx_executor 这一个线程里被调，_transcript_day 不用加锁。
        """
        if not self._transcript_ok:
            return
        try:
            day = time.strftime("%Y-%m-%d")
            if day != self._transcript_day:
                self._transcript_day = day
                self._prune_old_transcripts()
            path = os.path.join(self._transcript_dir, day + ".txt")
            with open(path, "a", encoding="utf-8") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {source_text}\n")
                if translation:
                    f.write(f"           {translation}\n")
                f.write("\n")
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
        self._held_since = 0.0
        self._last_unstable = ""
        self._draft_last_text = ""
        with self._tx_lock:
            self._tx_queue.clear()
            self._tx_epoch += 1  # 使在飞的翻译/草稿作废
            self.context_history.clear()
        print("🧹 已清空识别与翻译上下文")
        self._emit_display()

    def request_switch_language(self, new_lang):
        """切换源语言：写入待切换标志，由 ASR 线程在每批音频边界抢占执行。

        清上下文 + 改 SOURCE_LANGUAGE 必须在识别线程串行（热键线程先改语言
        会拿新语言参数识别旧缓冲，蹦出乱词）。不能只 submit(task) 排在
        _process_inbox 后面——收件箱持续非空时 inbox 循环不返回，切换会饿死
        （GPU 被游戏抢占的正是这种场景）。标志在 _process_inbox 每轮取音频
        前检查，最坏等当前这一批识别结束（~2.5s）而不是永远卡住。
        """
        with self._asr_lock:
            self._pending_lang_switch = new_lang
            if self._asr_scheduled:
                return  # 识别线程醒着，下一批边界会看到标志
            self._asr_scheduled = True
        try:
            self._asr_executor.submit(self._process_inbox)
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
            dropped = self._trim_tx_queue_locked()
        if dropped:
            self._warn_tx_dropped(dropped)
        try:
            self._tx_executor.submit(self._translation_worker)
        except RuntimeError:
            pass  # 程序正在退出

    def _trim_tx_queue_locked(self):
        """翻译队列硬顶：Ollama 半死（每句都超时重试）时排空速率远低于产出速率，
        队列会一直涨。丢最旧的——它们的德语早就在屏幕上滚过去了，用户在看的是
        最新几句。调用方必须持 _tx_lock。返回丢弃条数。

        （照 enqueue_audio 里 ASR 收件箱硬顶的同一套写法：丢最旧 + 累计计数 +
        节流告警。之前评估过"批量合并够用先不做"，但本轮给翻译加了超时重试，
        排空速率进一步下降，这个上限就成了必需品。）"""
        cap = getattr(config, "TRANSLATE_QUEUE_MAX_CHARS", 3000)
        total = sum(len(s) for s in self._tx_queue)
        dropped = 0
        while total > cap and len(self._tx_queue) > 1:
            total -= len(self._tx_queue.pop(0))
            dropped += 1
        self._tx_dropped += dropped
        return dropped

    def _warn_tx_dropped(self, dropped):
        """队列丢弃告警（每累计10条报一次，别刷屏）。在锁外调，回调可能碰 UI。

        ☠️ print 必须和 on_status 一起被节流住。以前它在 if 外面，于是被节流的
        只有屏幕提示，日志是**每次丢弃都打一行**——而这个函数触发的场景恰恰是
        "Ollama 半死、每句都超时"，也就是它会以每句一行的频率刷 subtitle.log。
        对照组就在本文件的 enqueue_audio 里：收件箱那条告警的 print 是放在节流
        条件内部的，两边不一致本身就说明这是漏改。
        （SHOW_PERFORMANCE 默认关掉的理由就是"别把 subtitle.log 撑大"，
        start_subtitles.ps1 还专门为跨天累积的概况行做了日志归档。）
        """
        if not (self._tx_dropped - self._tx_drop_warned >= 10 or self._tx_drop_warned == 0):
            return
        self._tx_drop_warned = self._tx_dropped
        print(f"⚠️  翻译积压超过 {getattr(config, 'TRANSLATE_QUEUE_MAX_CHARS', 3000)} 字符，"
              f"已丢弃最旧 {dropped} 句（累计 {self._tx_dropped}）——翻译服务可能很慢或半死")
        if self.on_status:
            self.on_status(f"⚠️ 翻译跟不上，已丢弃 {self._tx_dropped} 句的中文（德语不受影响）")

    def _circuit_open(self):
        """熔断中 = 最近连续多次翻译失败，这段时间内直接跳过 Ollama。

        不另做"探测请求"：熔断到期后的下一句正常翻译本身就是探测，
        成功即清零（失败的代价也只是一次超时，和专门发探测请求一样）。"""
        return time.time() < self._tx_circuit_until

    def _note_tx_result(self, ok, hard=False):
        """记翻译成败，连续失败到阈值就熔断一段时间。

        hard=True 表示"连冷超时（OLLAMA_TIMEOUT_COLD，默认 90 秒）都没等到第一个
        数据块"——这不是"这次赶巧慢了"，是 Ollama 侧真的半死。
        ☠️ 这种失败一次就该熔断，不能再攒够 TRANSLATE_FAIL_STREAK_OPEN 次：
        翻译 worker 是单线程的，一句最坏要串着烧 OLLAMA_TIMEOUT + 冷超时
        （15+90=105 秒），攒 3 次就是 **5 分多钟**。这期间队列按
        TRANSLATE_QUEUE_MAX_CHARS 一直丢最旧的句子，用户看到的是"中文时有时
        无"，而不是熔断本来要给的那种干净降级（整段只显德语 + 一条状态提示）。
        """
        if ok:
            self._tx_fail_streak = 0
            self._tx_circuit_until = 0.0
            self._ollama_hot = True
            return
        self._tx_fail_streak += 1
        if hard:
            self._tx_fail_streak = max(
                self._tx_fail_streak,
                getattr(config, "TRANSLATE_FAIL_STREAK_OPEN", 3))
        if self._tx_fail_streak >= getattr(config, "TRANSLATE_FAIL_STREAK_OPEN", 3):
            secs = getattr(config, "TRANSLATE_CIRCUIT_SEC", 30)
            self._tx_circuit_until = time.time() + secs
            self._tx_fail_streak = 0
            print(f"⛔ 翻译连续失败，暂停请求 {secs} 秒（期间只显示德语原文）")
            if self.on_status:
                self.on_status(f"⛔ 翻译服务无响应，暂停 {secs} 秒后自动重试（先只显德语）")

    # ------------------------------------------------------------------
    # Ollama 端口身份校验
    # ------------------------------------------------------------------
    def _check_ollama_identity(self, timeout=2):
        """11434 后面到底是不是 Ollama。返回 (状态, version)。

        状态取值 'ok' / 'impostor' / 'unreachable'。

        ☠️ 光看"请求没抛异常"不够：11434 没被 Ollama 占着时，本机任何进程都能
        抢先 bind 它。那样我们会把**系统全部声音的转录**连同上下文一路 POST
        给它，再把它返回的任意文本当字幕上屏并写进 transcripts。校验响应体里
        确实有 version 字段，成本一次 JSON 解析，就能把两者分开。
        """
        try:
            resp = self.ollama_session.get(
                f"{config.OLLAMA_BASE_URL}/api/version", timeout=timeout)
        except requests.RequestException:
            return "unreachable", ""
        try:
            version = (resp.json() or {}).get("version", "")
        except ValueError:
            version = ""
        return ("ok", version) if version else ("impostor", "")

    def _warn_ollama_impostor(self):
        print(f"🚨 {config.OLLAMA_BASE_URL} 有回应，但它不像是 Ollama"
              f"（/api/version 没返回 version 字段）。")
        print(f"   这个端口可能被别的程序占了。本程序会把识别出的原文发到这个地址，"
              f"在确认它确实是 Ollama 之前，翻译已暂停（只显示原文）。")
        if self.on_status:
            self.on_status("🚨 11434 端口上的程序不像 Ollama，已暂停翻译（只显原文）")

    def _ollama_identity_ok(self):
        """发翻译请求前的门禁。返回 False 表示这一句不要发出去。

        ☠️ 启动时校验一次是不够的：Ollama 是独立安装的服务、会自动更新并重启
        （CLAUDE.md 第 4 节第 6 条自己记着这事），重启的窗口期里端口是空的，
        任何本机进程都能补位。而此后每一句字幕仍会无条件发过去。

        但重验只在**真的需要**时做——启动验过且此后没断过连的话，这里是一次
        属性判断、零请求。触发重验的只有两种情况：启动时没验成（Ollama 当时
        还没起来），以及此后发生过 ConnectionError（我们验证过的那个服务可能
        已经不在了）。

        判据只拦 impostor，**不拦 unreachable**：连不上的话请求本来就会失败，
        由现有的 ConnectionError 路径去报（还带 60 秒节流的屏幕提示），在这里
        拦只会把"Ollama 没起来"升级成"字幕永远没有中文"。
        校验本身有 30 秒节流，Ollama 长时间不在时不会每句都多付一次 2 秒 GET。
        """
        if not (self._ollama_impostor or self._ollama_recheck_pending):
            return True
        now = time.time()
        if now < self._ollama_verify_next:
            return not self._ollama_impostor  # 节流期内沿用上次结论
        self._ollama_verify_next = now + 30
        state, _version = self._check_ollama_identity()
        if state != "unreachable":
            self._ollama_recheck_pending = False  # 有明确结论了
        was_impostor = self._ollama_impostor
        self._ollama_impostor = (state == "impostor")
        if self._ollama_impostor and not was_impostor:
            self._warn_ollama_impostor()
        return not self._ollama_impostor

    def _await_model_ready(self):
        """首句翻译前等后台预热落地（有界）。

        预热和第一句翻译打的是同一个模型：并发发出去时 Ollama 那边串行加载，
        客户端却按各自的超时计时——15 秒超时必然先到，句子被丢。等预热完成再发，
        请求本身就是热的，15 秒够用。预热失败/超时也照常往下走（用冷超时兜底）。

        ☠️ 这个等待必须能被退出打断。以前是一发 `_warm_done.wait(60)` 干等：
        shutdown() 对 _tx_executor 是 wait=True，打断不了正在等的 worker，
        于是"模型还在加载时点停止"会让优雅退出挂到 60 秒——而
        stop_subtitles.ps1 只给 5 秒宽限，到点强杀。强杀掉的正是
        shutdown() 里排在后面的 _save_lookup_cache() 和 _unload_our_models()：
        查词缓存丢一份，显存还按 keep_alive="2h" 占着。分片轮询 closing 之后，
        退出路径最多多等 0.25 秒。
        """
        if self._ollama_hot:
            return
        if _warm_done.is_set():
            if _warm_ok:
                self._ollama_hot = True
            return
        if self.on_status and not self._warm_notified:
            self._warm_notified = True
            self.on_status("✅ 识别已就绪；⏳ 翻译模型首次加载中，中文稍后跟上…")
        deadline = time.time() + getattr(config, "OLLAMA_WARM_WAIT", 60)
        while not _warm_done.wait(min(0.25, max(0.0, deadline - time.time()))):
            if self.closing or time.time() >= deadline:
                return  # 退出中/等够了：不再把 shutdown 堵在这
        if _warm_ok:
            self._ollama_hot = True

    def _translate_timeout(self):
        """选这次翻译请求的读超时（秒）。

        ☠️ 不能只看 `_ollama_hot`：它的语义是"模型在显存里"（`:493` 注释原话
        「拿到过一次 200 就算热」），而 15 秒够不够用取决于**这一刻 GPU 排不
        排得上队**，两者不是一回事。首启时 Whisper 刚加载完、正在集中消化
        启动积压，模型明明是热的，翻译却排在 ASR 后面拿不到 GPU，15 秒必然
        不够；更糟的是超时重试成功后 `_note_tx_result(True)` 立刻把
        `_ollama_hot` 置回 True，下一句又从 15 秒重新开始——日志里连着三次
        「翻译超时(15秒)」就是这么来的，不是同一次重试的回显。

        所以除了"模型热不热"，再看两个信号：
        - **降级粘性**：刚超时过就维持长超时一段时间，掐掉来回震荡；
        - **ASR 积压**：收件箱里攒着块 = 识别正占着 GPU，翻译肯定排在后面。
        """
        hot = getattr(config, "OLLAMA_TIMEOUT", 15)
        cold = getattr(config, "OLLAMA_TIMEOUT_COLD", 90)
        if not self._ollama_hot:
            return cold
        if time.time() < self._tx_slow_until:
            return cold
        # ☠️ 光看 _asr_backlog_n 会在**最该长超时的那一刻**读到 0：
        # _process_inbox 是先把整个收件箱取走、把快照清零，**然后**才去跑
        # process_iter。也就是说 ASR 真正霸着 GPU 的那 0.26~2.5 秒里，积压
        # 计数恰好是 0，这条规则整个失效（issue #16 的震荡正是靠它兜底的）。
        # _asr_busy 补的就是这一段：它在 process_iter 前后置位，和
        # _asr_backlog_n 一样是无锁快照，不引入 _tx_lock/_asr_lock 的锁序问题。
        if self._asr_busy:
            return cold
        threshold = getattr(config, "TRANSLATE_SLOW_BACKLOG_BLOCKS", 4)
        if threshold > 0 and self._asr_backlog_n >= threshold:
            return cold
        return hot

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
            # 纯词典直译也要重绘 live：句对已进历史，queue/inflight 已空，
            # 不 emit 的话 ASR 上一次画上去的同一句德语会残留在 live 行
            if direct and not self.closing:
                with self._tx_lock:
                    stale_direct = (epoch != self._tx_epoch)
                if not stale_direct:
                    self._emit_display()
            return  # 本轮全是词典感叹词，不用打Ollama

        german = " ".join(batch)
        translation = None
        try:
            t0 = time.time()
            # 流式：翻译的中文逐段推到live区的草稿行，不等整句翻完
            translation = self._translate_single_sentence(
                german, context, on_partial=self._epoch_gated_draft(epoch))
            tx_elapsed = time.time() - t0
            self._stat_note_tx(tx_elapsed)
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

        # ☠️ 翻译失败时 translation==german（原样返回）。屏幕上只显示一遍德语是
        # 对的，但**存档必须照常写**：以前这里是 `if translation != german`，
        # Ollama 挂掉/熔断的那几分钟里德语原文一条都不入档，而 transcripts/
        # 的用途正是回看和学德语——用户事后完全无从知道那段说了什么。
        # 中文留空，格式和"只有原文"的行一致
        self._save_transcript(german, "" if translation == german else translation)
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
        # ☠️ 这里曾经有一条 `if self._asr_busy: return`，它是**死代码**：
        # _maybe_draft 的唯一调用点是 _process_items 末尾，而 _asr_busy 在同
        # 函数的 finally 里刚被置回 False，且 _asr_executor 是单线程——检查
        # 执行时它恒为 False。而且即使不恒为 False 也拦不住：草稿是 submit 到
        # _tx_executor 异步跑的，真正打 Ollama 的时刻 ASR 早就开始下一轮
        # process_iter 了。所以让路检查搬进 _draft_worker（跑在 tx 线程，
        # 那里 _asr_busy 才可能真是 True），见那里的注释。
        if self._lookup_inflight:
            return  # 用户点了查词/AI分析在等结果，草稿让路（这类是一次性人工
                     # 请求，比"每1.5秒一次"的草稿更该优先拿到GPU）
        if not self._ollama_hot:
            return  # 模型还没进显存：那唯一一次冷加载要留给正式句子，草稿别去排队占坑
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
        # ☠️ 让路检查必须在**这里**，不能在 _maybe_draft 里：那边跑在 ASR 线程
        # 上、且刚好在 process_iter 结束之后，_asr_busy 恒为 False（这个判断在
        # 那边当了很久的死代码）。到了这一刻才是真的要发请求，ASR 也确实可能
        # 正占着 GPU——草稿是奢侈品，让给识别。
        if self._asr_busy:
            return
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
    def _enter_inflight(self):
        """标记一个"用户主动发起的 Ollama 请求"开始（查词/AI分析）。"""
        with self._inflight_lock:
            self._lookup_inflight_n += 1

    def _exit_inflight(self):
        """结束。计数归零才算真的没人在等——见 _maybe_draft 的让路条件。"""
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
        cache_key = (word.lower(), config.SOURCE_LANGUAGE)
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
        lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)
        cache_key = (word.lower(), config.SOURCE_LANGUAGE)
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
        self._enter_inflight()  # 草稿翻译看这个让路，见 _maybe_draft
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
        self._enter_inflight()  # 草稿翻译看这个让路，见 _maybe_draft
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

    def _translate_single_sentence(self, sentence, german_context, on_partial=None):
        """翻译一段对白（源语言 -> 中文）。失败时返回原文。

        on_partial: 每~0.15秒把已生成的部分中文回调出去（流式上屏，
        中文首字延迟从"整句翻完"降到首token到达）。回调必须线程安全。
        """
        try:
            lang_name = config.LANGUAGE_NAMES.get(config.SOURCE_LANGUAGE, config.SOURCE_LANGUAGE)

            # 时间归一化只作用于**喂给模型的副本**：屏幕上的德语行、transcripts
            # 存档、context_history 存的都还是 ASR 原样输出。
            # ☠️ 绝不能就地覆盖 sentence——失败路径 `return sentence` 的返回值
            # 会和 _translation_worker 里未归一化的 german 做相等判断，
            # 不等就会把德语显示两遍（2026-07-04 修过的那个"德语\n德语"）
            if config.SOURCE_LANGUAGE == "de":
                prompt_sentence = _normalize_clock_times(sentence)
                prompt_context = _normalize_clock_times(german_context)
            else:
                prompt_sentence, prompt_context = sentence, german_context

            # 术语表只注入当前句子/上下文里真出现的词条，prompt保持精简
            haystack = f"{prompt_context} {prompt_sentence}".lower()
            matched_terms = [
                f"{de} → {zh}" for de, zh in config.GLOSSARY.items()
                if de.lower() in haystack
            ]
            glossary_block = ""
            if matched_terms:
                glossary_block = "\n【术语表：以下人名/党派/术语必须照用这些译名】\n" + "\n".join(matched_terms[:12]) + "\n"

            # 语域跟着当前模式走（新闻/影视/精听），别再无条件按"剧集对白"翻
            styles = getattr(config, "TRANSLATION_STYLE_PROMPTS", {}) or {}
            style = styles.get(getattr(config, "TRANSLATION_STYLE", ""), None)
            if style is None:
                style = next(iter(styles.values()), {"role": "字幕翻译", "rules": ""})
            n_rules = len([ln for ln in style["rules"].splitlines() if ln.strip()])

            # 下面三条是所有语域共有的硬约束，都是实测撞出来的（2026-08-02 ZDF 实测）：
            # 少了第一条，遇到"Dem Ersten Weltkrieg. und der Corona-Pandemie."这种
            # 半句片段，模型会把整段上下文重翻一遍上屏（实测 3/3 复现，74字 vs 15字），
            # 用户看到的是刚读过的几句又滚一遍；第二条挡"（注：建议补全后半句…）"这
            # 类译注；第三条挡 24 小时制时间（22.15 Uhr 实测 3/3 被翻成"九点十五"）。
            prompt = f"""/no_think 你是{lang_name}{style['role']}。请把{lang_name}对白翻译成自然的简体中文。

【要求】
{style['rules']}
{n_rules + 1}. 只输出中文翻译，不要解释，不要输出{lang_name}原文
{n_rules + 2}. 【上下文】只用来帮助理解，绝对不要翻译上下文里的句子——它们已经显示过了
{n_rules + 3}. 当前对白哪怕只是半句、不完整，也只翻这半句：不要补全、不要加括号注释或说明
{n_rules + 4}. 时间是24小时制（22:15 Uhr = 22点15分，19:10 Uhr = 19点10分），照抄小时和分钟，不要换算成上午/下午，数字一律不改
{glossary_block}
【{lang_name}上下文（此前的对白）】
***
{prompt_context if prompt_context else "（无上下文）"}
***

【当前对白】
***
{prompt_sentence}
***

中文翻译：
"""

            # 熔断中：直接降级成德语，一个请求都不发——Ollama 半死时每句都付
            # 满超时，队列只会越堆越长（德语原文照常上屏，用户不是什么都看不到）
            if self._circuit_open():
                return sentence

            # 确认端口后面确实是 Ollama 才把转录发出去（见 _ollama_identity_ok）
            if not self._ollama_identity_ok():
                return sentence

            # 模型还没进显存时先等后台预热落地，并改用长超时
            self._await_model_ready()
            if self.closing:
                return sentence  # 等预热期间用户点了退出：别再发这个请求

            ollama_options = {
                "temperature": 0.3,
                "top_p": 0.9,
                "num_predict": 512,
                # 全程序共用一个 num_ctx，换值会让 Ollama 重装模型，见 config
                "num_ctx": getattr(config, "OLLAMA_NUM_CTX", 4096),
            }
            # 不把某台机器/某个模型的层数硬编码成 50：qwen 版本的总层数
            # 不同，小显存机器固定 offload 层数还会把剩余层挤到系统内存。
            # None 交给 Ollama 自动按显存决定；高级用户可在 config_local.py
            # 写整数覆盖。
            num_gpu = getattr(config, "OLLAMA_NUM_GPU", None)
            if num_gpu is not None:
                try:
                    ollama_options["num_gpu"] = max(0, int(num_gpu))
                except (TypeError, ValueError):
                    print(f"   ⚠️  OLLAMA_NUM_GPU 无效({num_gpu!r})，改用自动分配")

            payload = {
                "model": config.OLLAMA_MODEL,
                "prompt": prompt,
                "stream": True,  # 流式：中文逐段上屏，不等整句
                "think": False,
                "keep_alive": "2h",  # 默认5分钟卸载，安静段后第一句付~9秒冷加载
                "options": ollama_options,
            }

            # 读超时重试一次：模型冷加载期间首句必然超时，丢掉的话那句话永远
            # 没有中文（2026-08-02 实测撞上）。只重试 ReadTimeout——
            # ☠️ ConnectionError（Ollama 没起）绝不能重试，那会把单线程 worker
            # 堵住，且现有的快速失败+60秒节流提示才是对的处理
            last_timeout = None
            for attempt in (0, 1):
                timeout = self._translate_timeout()
                try:
                    response = self.ollama_session.post(
                        f"{config.OLLAMA_BASE_URL}/api/generate",
                        json=payload,
                        stream=True,
                        # 流式下timeout是"相邻数据块间隔"上限，不是总时长
                        timeout=timeout,
                    )
                except requests.ReadTimeout as e:
                    last_timeout = e
                    # ☠️ 降级必须有粘性：只置 _ollama_hot=False 的话，重试一成功
                    # _note_tx_result 就把它翻回 True，下一句又从短超时开始，
                    # 每句白烧一次满超时（issue #16）
                    self._tx_slow_until = time.time() + getattr(
                        config, "TRANSLATE_SLOW_STICKY_SEC", 60)
                    if attempt == 0 and not self.closing:
                        self._ollama_hot = False  # 让重试走冷超时
                        print(f"   ⏳ 翻译超时({timeout}秒)，重试一次"
                              f"（GPU 可能正忙于识别，或模型在加载）")
                        continue
                    break

                try:
                    if response.status_code == 200:
                        parts = []
                        grown = 0
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
                            grown += len(parts[-1])
                            if data.get("done"):
                                break
                            if grown > _MAX_STREAM_CHARS:
                                # 见 _MAX_STREAM_CHARS：模型复读或服务端异常，
                                # 已经拿到的部分照常用，别再无限收下去
                                print(f"   ⚠️  翻译响应超过 {_MAX_STREAM_CHARS} 字符，提前截断")
                                break
                            if on_partial and time.time() - last_emit > 0.15:
                                partial = "".join(parts).strip()
                                if partial:
                                    last_emit = time.time()
                                    on_partial(partial)
                        translation = re.sub(r'<think>.*?</think>', '', "".join(parts), flags=re.DOTALL)
                        # ☠️ 拿到 200 但一个字都没生成时不能算成功（撞上模型卸载
                        # 边界、或整段被 <think> 吃掉都会这样）。算成功的话
                        # _note_tx_result 会把 _ollama_hot 置真、熔断计数清零、
                        # 超时从冷档翻回 15 秒——而这一句其实根本没有中文，
                        # 等于用一次空响应把所有降级保护都解除了
                        self._note_tx_result(ok=bool(translation.strip()))
                        return _strip_translator_note(translation)
                    else:
                        print(f"   ⚠️  Ollama 返回错误 (HTTP {response.status_code})，显示德语原文")
                        self._note_tx_result(ok=False)
                        return sentence
                except requests.ReadTimeout as e:
                    # 流式响应中途断流也是 ReadTimeout（首token前无数据最常见）
                    last_timeout = e
                    if attempt == 0 and not self.closing:
                        self._ollama_hot = False
                        print(f"   ⏳ 翻译流中断({timeout}秒无数据)，重试一次")
                        continue
                    break
                finally:
                    # stream=True的连接不close不会归还连接池：done后break出来、
                    # 半途超时、非200，都必须显式关，否则长session连接泄漏
                    response.close()

            # 走到这 = 短超时和随后的冷超时都没等到数据。hard=True 让熔断立刻
            # 打开，别再让后面的句子一句一句去烧满 105 秒（见 _note_tx_result）
            print(f"   ⚠️  翻译超时: {last_timeout}，显示德语原文")
            self._note_tx_result(ok=False, hard=True)
            return sentence

        except requests.ConnectionError as e:
            # Ollama没在运行——屏幕上给用户明确提示（60秒节流），
            # 否则只是默默全德语，用户不知道发生了什么。
            # ☠️ 顺带作废端口身份校验：连接断过就说明我们验证过的那个服务可能
            # 已经不在了（Ollama 自动更新会重启），恢复之后必须重新确认一次
            # 端口后面还是它，而不是趁虚而入的别的进程
            self._ollama_recheck_pending = True
            print(f"   ⚠️  翻译失败: {e}，显示德语原文")
            if self.on_status and time.time() - self._ollama_down_notified > 60:
                self._ollama_down_notified = time.time()
                self.on_status("⚠️ 翻译服务(Ollama)未运行，暂时只显示德语——请运行 启动字幕.bat 或 ollama serve")
            self._note_tx_result(ok=False)
            return sentence
        except Exception as e:
            print(f"   ⚠️  翻译失败: {e}，显示德语原文")
            self._note_tx_result(ok=False)
            return sentence

    # ------------------------------------------------------------------
    # 识别（ASR线程，单线程executor保证串行）
    # ------------------------------------------------------------------
    def _extract_sentences(self, final=False):
        """从pending_text里切出完整句子（切分规则见 _split_sentences），残句留下。

        final=False 时，正好落在文本末尾的句尾会被【扣留】——还看不见下一个词，
        判断不了这个句号是不是 Whisper 在缩写/停顿处误打的。连续说话时下一块
        音频 0.4 秒就到，自然就判定了；说完一段没有下文时由 flush_pending 的
        有界放行兜底（最多 SENTENCE_HOLD_SEC）。
        """
        sentences, rest = _split_sentences(self.pending_text, final=final)
        self.pending_text = rest
        # 记扣留起点：还有残句且它正停在终止符上 = 有句子被扣着等下文
        if not final and rest and _ends_with_terminator(rest):
            if not self._held_since:
                self._held_since = time.time()
        else:
            self._held_since = 0.0
        return sentences

    def _release_held_boundary(self):
        """扣留的句尾到点放行。

        ☠️ 只动文字，绝不碰 processor.finish()——那会清掉正在用的音频缓冲，
        而这时候用户很可能只是句子中间换了口气，音频还要接着识别。
        """
        held = self._held_since
        if not held:
            return
        if time.time() - held < getattr(config, "SENTENCE_HOLD_SEC", 0.6):
            return
        sentences = self._extract_sentences(final=True)
        if sentences:
            # 计数进概况：放行次数高 = SENTENCE_HOLD_SEC 太短，很多是说话人
            # 句中换气就被切了（这个问题在 transcripts 里看不出来，
            # 时间戳是"存盘时刻"不是"停顿时长"，只能在运行时数）
            with self._stats_lock:
                self._stat_held_release += 1
            # 标记"刚放行过"：下一段提交的文本如果以小写词开头，说明这句话其实
            # 还没说完，0.6 秒是切在了说话人的换气上（德语句首必大写）。
            # 光看放行次数区分不出"说完了停顿"和"句中换气"，这个标志才能
            self._release_pending = True
            self._enqueue_sentences(sentences)
            self._emit_display()

    def _append_committed(self, committed_text):
        if not committed_text:
            return
        if getattr(self, "_release_pending", False):
            self._release_pending = False
            # 刚有过扣留放行，而接上来的第一个词是小写 ⇒ 上一句被切在句中了
            head = committed_text.lstrip()
            if head and head[0].isalpha() and head[0].islower() and ord(head[0]) < 0x2E80:
                with self._stats_lock:
                    self._stat_held_misfire += 1
        if self.pending_text:
            self.pending_text += " " + committed_text
        else:
            self.pending_text = committed_text

    def enqueue_audio(self, audio_data, capture_time):
        """采集线程调用：音频进收件箱，识别线程没醒着就唤醒它。

        常规场景（GPU被抢）永不丢块、滞后自动追上；只有积压超过
        ASR_INBOX_MAX_BLOCKS（≈2分钟，识别线程卡死级别的异常）才丢最旧
        的块保内存。注意"不丢词"指的是这一级：采集侧 audio_capture 的
        audio_queue(maxsize=10) 在处理线程堵死时仍会丢并打日志"""
        self._idle_flushed = False  # 有新音频了，空闲flush兜底重新武装
        with self._asr_lock:
            self._audio_inbox.append((audio_data, capture_time))
            cap = getattr(config, "ASR_INBOX_MAX_BLOCKS", 240)
            if len(self._audio_inbox) > cap:
                dropped = len(self._audio_inbox) - cap
                del self._audio_inbox[:dropped]  # 丢最旧的：反正早失去实时性了
                self._inbox_dropped += dropped
                # 每累计100块（≈50秒音频）报一次，别刷屏
                if self._inbox_dropped - self._inbox_drop_warned >= 100 or self._inbox_drop_warned == 0:
                    self._inbox_drop_warned = self._inbox_dropped
                    print(f"⚠️  识别积压超过{cap}块(≈{cap * config.CHUNK_SUBMIT_SECONDS:.0f}秒)，"
                          f"已丢弃最旧音频保内存(累计{self._inbox_dropped}块)——识别线程可能已卡死，建议重启程序")
            n = len(self._audio_inbox)
            self._asr_backlog_n = n  # 给翻译线程看的无锁快照
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

    # 注：曾有一个 request_clear_context()（submit(clear_context) 到 ASR 池），
    # 是 request_switch_language 的前身。后者改成"写标志 + 每批边界抢占执行"
    # 正是因为收件箱持续非空时 submit 的任务会被饿死（见 request_switch_language
    # 的注释），旧的那个从此零调用方，已删——留着只会让人以为还有第二条切换路径。

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
        # ☠️ 启动预热若还在飞，必须先等它落地再卸旧模型：卸载先到、预热后到的话
        # 模型又被 keep_alive="2h" 拉回显存留驻两小时（和 CLAUDE.md 第 13 条
        # 同一类竞态，当时只处理了退出路径）。有界等待，卡住就放弃继续走。
        if old_model:
            t = _warm_thread
            if t is not None and t.is_alive():
                t.join(timeout=10)
        if old_model and old_model != model:
            self._ollama_hot = False  # 换了模型：新的还没进显存，回到冷超时
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
            self._ollama_hot = True  # 新模型已进显存，翻译可以回到正常超时
            print(f"🔥 翻译模型 {model} 预热完成 {time.time() - t0:.1f}秒")
        except Exception as e:
            if not self.closing:
                print(f"   ⚠️  模型预热失败（首句翻译会稍慢）: {e}")

    def _apply_pending_lang_switch(self, new_lang):
        """在 ASR 线程内执行：清上下文 + 改语言（与识别串行）。"""
        self.clear_context()
        config.SOURCE_LANGUAGE = new_lang
        name = config.LANGUAGE_NAMES.get(new_lang, new_lang)
        print(f"🌐 源语言已切换为: {name}")
        if self.on_status:
            self.on_status(f"🌐 源语言已切换: {name}")

    def _process_inbox(self):
        """识别线程主循环：每批边界先处理语言切换，再消化收件箱，空了才睡。"""
        while True:
            with self._asr_lock:
                pending_lang = self._pending_lang_switch
                if pending_lang is not None:
                    self._pending_lang_switch = None
                items = self._audio_inbox
                self._audio_inbox = []
                self._asr_backlog_n = 0  # 已全部取走，积压清零
                if not items and pending_lang is None:
                    self._asr_scheduled = False
                    return
            if pending_lang is not None:
                try:
                    self._apply_pending_lang_switch(pending_lang)
                except Exception as e:
                    print(f"⚠️  切换源语言失败: {e}")
                # ☠️ 这一批音频是切换【之前】抓的旧语言声音，绝不能用新语言参数
                # 去识别（蹦出乱词）。_apply_pending_lang_switch → clear_context
                # 本来就已经把识别缓冲整个丢掉了，再把切换前的音频塞进新缓冲
                # 自相矛盾。用户主动按热键时丢掉不到一秒的旧语言音频是正确取舍
                if items:
                    print(f"🧹 切换语言，丢弃切换前的 {len(items)} 块音频")
                    items = []
            if not items:
                continue  # 只有切语言、没有音频：回去看有没有新标志/新块
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

        # 推理**开始前**的缓冲长度 + 此刻 Ollama 是否也在跑。
        # 这两个是回答"ASR 耗时到底受什么影响"的关键自变量，之前只记了推理
        # 结束、裁剪之后的缓冲长度（那个恒等于裁剪阈值，什么也说明不了）
        buf_before = self.processor.buffer_seconds()
        with self._tx_lock:
            overlapped = bool(self._tx_inflight)

        self._asr_busy = True  # 草稿翻译看这个标志让路（识别在GPU上跑的期间）
        try:
            committed, unstable = self.processor.process_iter()
        finally:
            self._asr_busy = False
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
            self._held_since = 0.0  # 连扣留的句尾一起送走了

        self._emit_display()
        self._maybe_draft()

        elapsed = time.time() - start_time
        self._stat_note_asr(elapsed, self.processor.buffer_seconds(), len(items),
                            buf_before=buf_before, overlapped=overlapped)
        if config.SHOW_PERFORMANCE:
            shown = committed if committed else "(无新提交)"
            merged = f"(合并{len(items)}块)" if len(items) > 1 else ""
            print(f"   ⏱️  {elapsed:.2f}秒{merged} | 缓冲{self.processor.buffer_seconds():.1f}秒 | ✅ {shown[:60]}")
            if unstable:
                print(f"   ⏳ 未稳定: {unstable[:60]}")

    @staticmethod
    def _stats_enabled():
        """概况关掉时（STATS_SUMMARY_INTERVAL<=0）一个样本都不该再收。

        ☠️ 排空点只有 _stat_note_asr 里那个到点打印的分支，而它在
        `interval <= 0` 时直接 return。翻译耗时以前是在 _translation_worker
        里【无条件】 append 的，于是用户一旦按 config 注释把概况关掉，
        _stat_tx 就再也没有出口了——一条一句、长跑几小时只涨不消。
        采样入口统一走这个门。
        """
        return getattr(config, "STATS_SUMMARY_INTERVAL", 60) > 0

    def _stat_note_tx(self, elapsed):
        """记一次翻译耗时（概况关掉时直接丢弃，不进内存）。"""
        if not self._stats_enabled():
            return
        with self._stats_lock:
            self._stat_tx.append(elapsed)

    def _stat_note_asr(self, elapsed, buf_sec, n_items,
                       buf_before=None, overlapped=False):
        """记一轮识别指标；到间隔就打一行概况（跑在ASR线程，无音频时不打）

        buf_before/overlapped 是 2026-08-04 加的诊断项，用来回答两个之前
        answer 不了的问题（都需要真实使用数据，读代码得不出来）：
        1. **ASR 耗时到底受不受缓冲长度影响？** 分短/长缓冲两桶记 p50。
           faster-whisper 的 `pad_or_trim` 会把每段都补到固定 30 秒再进编码器
           （transcribe.py:1180 + feature_extractor nb_max_frames=3000），
           所以理论上编码开销与缓冲长度无关、只有解码随 token 数变。
           两桶 p50 若基本持平，就证实了这一点，`BUFFER_TRIM_SEC` 也就没有
           调小的价值——省得以后再有人去做那个 A/B。
        2. **翻译和识别同时跑会不会互相拖慢？** 按"推理开始时 Ollama 是否
           在飞"分桶记 p50，直接看差值。
        """
        interval = getattr(config, "STATS_SUMMARY_INTERVAL", 60)
        if not self._stats_enabled():
            return
        with self._stats_lock:
            self._stat_asr.append(elapsed)
            self._stat_buf_max = max(self._stat_buf_max, buf_sec)
            if buf_before is not None:
                # 分桶阈值取 BUFFER_KEEP_SEC：裁剪后停在它附近，涨到 TRIM 再裁，
                # 所以它天然就是"短缓冲/长缓冲"的分界
                bucket = (self._stat_asr_longbuf if buf_before >= config.BUFFER_KEEP_SEC
                          else self._stat_asr_shortbuf)
                bucket.append(elapsed)
            (self._stat_asr_overlap if overlapped else self._stat_asr_solo).append(elapsed)
            if n_items > 1:
                self._stat_merge += 1
            if time.time() - self._stats_t0 < interval:
                return
            asr, tx = sorted(self._stat_asr), sorted(self._stat_tx)
            merge, buf_max = self._stat_merge, self._stat_buf_max
            draft, dhit = self._stat_draft, self._stat_dict
            held, misfire = self._stat_held_release, self._stat_held_misfire
            shortb, longb = sorted(self._stat_asr_shortbuf), sorted(self._stat_asr_longbuf)
            solo, overlap = sorted(self._stat_asr_solo), sorted(self._stat_asr_overlap)
            self._stat_asr, self._stat_tx = [], []
            self._stat_merge, self._stat_buf_max = 0, 0.0
            self._stat_draft = self._stat_dict = 0
            self._stat_held_release = self._stat_held_misfire = 0
            self._stat_asr_shortbuf, self._stat_asr_longbuf = [], []
            self._stat_asr_solo, self._stat_asr_overlap = [], []
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
        if held:
            line += f" | 扣留放行{held}"
            if misfire:
                line += f"(切早{misfire})"
        # 诊断项：只在两桶都有样本时打，否则是噪声
        if shortb and longb:
            line += (f" | 缓冲短{pct(shortb, .5):.2f}s({len(shortb)})"
                     f"/长{pct(longb, .5):.2f}s({len(longb)})")
        if solo and overlap:
            line += (f" | 独占{pct(solo, .5):.2f}s({len(solo)})"
                     f"/与翻译并发{pct(overlap, .5):.2f}s({len(overlap)})")
        print(line)

    def flush_pending(self):
        """空闲兜底：一段话说完后没有新音频，未提交尾部/未成句残句会一直挂着。
        main的定时器每秒调这里：距上次音频超过IDLE_FLUSH_SEC才动手。
        和translate()跑在同一个单线程池里，天然串行。"""
        if time.time() - self.last_audio_time < config.IDLE_FLUSH_SEC:
            # 还没到收尾时机，但被扣留的句尾到点了就先放行（只动文字不碰音频缓冲）
            self._release_held_boundary()
            return
        if self._idle_flushed:
            return  # 这轮空闲已经冲干净了，没有新音频前每秒空跑 finish 纯浪费
        self._idle_flushed = True

        tail = self.processor.finish()
        self._append_committed(tail)

        if not self.pending_text:
            # 没有要冲的内容；如果屏幕上还挂着灰色未稳定尾部，清掉重绘一次
            if self._last_unstable:
                self._last_unstable = ""
                self._emit_display()
            return

        sentences = self._extract_sentences(final=True)
        if self.pending_text:
            sentences.append(self.pending_text)
            self.pending_text = ""
        self._held_since = 0.0

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
        # ☠️ 查词请求也带 keep_alive="2h"：在飞的那一个如果在
        # _unload_our_models **之后**才落地，会把刚卸掉的模型重新拉回显存
        # 留驻两小时（和 2026-07-20 修的预热线程竞态同类，当时只处理了预热）。
        # 有界等待：正常查词 1-2 秒就回来；卡住的话最多等 3 秒放弃继续退出
        # （宁可漏卸一次也不拖住退出——stop 脚本还有 HTTP 卸载兜底）
        # AI 分析走的是另一个池（同样带 keep_alive="2h"），一起有界排干
        drains = []
        for executor, name in ((self._lookup_executor, "LookupDrain"),
                               (self._analysis_executor, "AnalysisDrain")):
            t = Thread(
                target=executor.shutdown,
                kwargs={"wait": True, "cancel_futures": True},
                daemon=True, name=name)
            t.start()
            drains.append(t)
        deadline = time.time() + 3
        for t in drains:
            t.join(timeout=max(0.0, deadline - time.time()))
        self._save_lookup_cache()
        self._unload_our_models()
        try:
            self.ollama_session.close()
            self.lookup_session.close()
            self.analysis_session.close()
        except Exception:
            pass

    @staticmethod
    def _model_name_matches(loaded_name, configured):
        """/api/ps 报的名字和 config 里写的是不是同一个模型。

        ☠️ 必须做前缀匹配，不能只 `==`：config_local.py 里写不带 tag 的名字
        （"qwen3.5"）是 README 明确鼓励的用法，而 /api/ps 报回来的是
        "qwen3.5:latest"。stop_subtitles.ps1 从一开始就做的是
        `-eq $m -or -like "$m`:*"`，Python 侧却只做集合精确比较——于是点 ❌ /
        Alt+F4 退出（不经过停止脚本）时漏卸模型，5.6GB 显存按 keep_alive="2h"
        白占两小时。同一个坑当时只修了 PowerShell 那一半。
        """
        if not loaded_name or not configured:
            return False
        return loaded_name == configured or loaded_name.startswith(configured + ":")

    def _unload_our_models(self):
        """退出时主动卸载本程序加载的翻译模型。

        翻译请求带 keep_alive=2h：stop脚本会CLI卸载，但❌按钮/Ctrl+C退出不经过
        stop脚本，模型会在显存里赖到2小时到期（2026-07-17 实测：程序退了，
        9b 还独占 5.6GB）。必须放在两个 executor shutdown **之后**：在飞的
        翻译/预热任务收尾会重新加载模型，先卸就白卸了。
        只卸"/api/ps 里确实加载着、且名字是本程序配置"的模型——对未加载的
        模型发 keep_alive=0 会先触发一次完整加载（纯浪费退出时间），
        用户自己跑的无关模型更不能碰。"""
        # 启动预热若还在飞，先等它落地（有界3秒）：预热请求在卸载**之后**
        # 完成会把模型重新留驻2小时——"加载中途退出"的窗口期正好撞上
        t = _warm_thread
        if t is not None and t.is_alive():
            t.join(timeout=3)

        ours = [m for m in (config.OLLAMA_MODEL,
                            getattr(config, "GAME_MODE_OLLAMA_MODEL", None)) if m]
        try:
            loaded = self.ollama_session.get(
                f"{config.OLLAMA_BASE_URL}/api/ps", timeout=2,
            ).json().get("models", [])
            for m in loaded:
                name = m.get("name")
                if any(self._model_name_matches(name, ours_name) for ours_name in ours):
                    self.ollama_session.post(
                        f"{config.OLLAMA_BASE_URL}/api/generate",
                        json={"model": name, "prompt": "", "keep_alive": 0},
                        timeout=3,
                    ).close()
                    print(f"🧹 已卸载翻译模型 {name}（释放显存）")
        except Exception:
            pass  # Ollama不在/超时都无所谓：模型最多赖到keep_alive到期，不阻塞退出

    def __del__(self):
        """清理资源"""
        try:
            if hasattr(self, 'model'):
                del self.model
            if hasattr(self, 'ollama_session'):
                self.ollama_session.close()
        except Exception:
            pass
