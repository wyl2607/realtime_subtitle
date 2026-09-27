"""语言对：配置转发、自动检测投票、解码质量自愈、切换的消费端。

从 translator_queue.py 拆出来的（2026-09-27）。CLAUDE.md 第 4 节第 31/35/41/42
条讲的全是这一块：滞回投票为什么往保守调、切换请求为什么只能走一条路、
源/目标为什么是两个独立的值、旧语言事件为什么按代数拒收。以前这些方法
散在一个 2500 行的类里，前后隔着 900 行；现在放在一个文件里读。

分两层：

- **模块级**：`language_pairs` / `target_for` 等（转发 language_policy）、
  `LanguageVote` / `DecodeHealth`（纯状态机，单测直接跑）、检测日志格式化。
  这些不碰线程也不碰模型。
- **`LanguageSwitchMixin`**：`request_switch_language`（入队）、
  `_maybe_detect_language` / `_check_decode_health`（ASR 线程里判要不要切）、
  `_apply_pending_lang_switch`（ASR 线程里真切）。

☠️ 本 mixin **不自己 __init__**，下列状态由 `WhisperQueueTranslator.__init__`
建好（和 lookup.py / transcript.py / runtime_stats.py 是同一个约定）：

    self._asr_lock            ☠️ 保护下面三个 pending 字段——它们是**一个请求
                              整体**，写和取都必须在这把锁内一次完成（第 35 条）
    self._pending_lang_switch / _pending_lang_source / _pending_lang_target
    self._asr_scheduled       识别线程是否已排上，和 _asr_lock 一起用
    self._asr_executor        识别线程池（切换请求靠它把 _process_inbox 叫醒）
    self.processor            OnlineASRProcessor（检测语言、清 prompt 上下文）
    self._lang_vote           LanguageVote 实例
    self._decode_health       DecodeHealth 实例
    self._lang_rescue         是否处在"解码质量异常 → 抢救"状态
    self._lang_detect_next    下一次允许检测的时刻（间隔/冷却共用）
    self._lang_in_cooldown    当前等待是不是切换后的冷却（只影响日志）
    self._lang_revision       语言对代数，真切了才 +1（第 42 条）
    self._lang_log            LanguageDetectLogger（_lang_logger() 懒建兜底）
    self.on_status / on_language_applied   UI 回调，可为 None

反向依赖宿主类的方法：`clear_context()`、`_process_inbox()`。
`_process_inbox` 里"每批边界先取 pending 再识别"的那段消费逻辑**仍在宿主类**
——它和识别主循环是一体的，拆开反而要跨文件对锁。

☠️ 本模块不许 import translator_queue（那边在模块级 import 本模块，反过来
就是循环）。需要宿主的东西一律走 self。
"""
import re
import time

import realtime_subtitle.config as config
from realtime_subtitle import language_policy


def language_pairs():
    """当前生效的「源语言→目标语言」列表，也是 Ctrl+Alt+L 的循环顺序。

    ☠️ 实现在 realtime_subtitle/language_policy.py，这里只是转发。设置面板
    需要同一份答案，而它不能 import 本模块（会把 torch/faster-whisper 拉进
    UI 的导入链）；两边各写一份解析的后果见 language_policy 的文件头。
    """
    return language_policy.language_pairs()


def target_for(source_lang):
    """这个源语言配的目标语言是哪个（查不到就用 TARGET_LANGUAGE）。"""
    return language_policy.target_for(source_lang)


def current_target_language():
    """当前该翻成哪个语言。

    ☠️ 以目标语言配置项为准、而不是每次都按源语言现查：切语言对时两个值是
    一起写的（_apply_pending_lang_switch），现查会在两次赋值之间出现
    "源已经变了、目标还没变"的窗口，而翻译 worker 是另一个线程。
    """
    return getattr(config, "TARGET_LANGUAGE", "zh")


def language_name(lang):
    return config.LANGUAGE_NAMES.get(lang, lang)


def target_language_name(lang):
    """目标语言在 prompt 里的叫法。zh 要说"简体中文"，见 config 里的注释。"""
    names = getattr(config, "TRANSLATION_TARGET_NAMES", None) or {}
    return names.get(lang) or language_name(lang)


class LanguageVote:
    """自动语言切换的滞回投票。纯状态机，不碰模型也不碰线程，单测直接跑。

    ☠️ 为什么非要滞回：切语言会 clear_context() 丢掉识别缓冲，而新语言还会
    通过 initial_prompt 自我强化（避坑清单记着"英文一旦被误认能锁死近 3 分钟"）。
    也就是说**误切一次的代价远大于晚切几秒**，所以判据一律往保守调：
      - 置信度不够 → 不算数
      - 不在 LANGUAGE_PAIRS 里的语言 → 不算数（一段意大利语歌不该把整场切走）
      - 只要中间断了一次，连击清零重来
    默认 3 连击 × 6 秒检测间隔 ≈ 说话人得持续讲另一种语言 18 秒才会触发。
    """

    def __init__(self):
        self.lang = None      # 正在攒连击的候选语言
        self.streak = 0
        self.last_event = None  # same_language / unsupported / low_confidence / streak / switch

    def reset(self):
        self.lang = None
        self.streak = 0

    def feed(self, lang, prob, current, allowed, min_prob, need_streak):
        """喂一次检测结果。返回该切换到的语言，或 None（不切）。"""
        if not lang or lang == current:
            self.reset()          # 检测结果就是当前语言：本来就没事
            self.last_event = "same_language"
            return None
        if lang not in allowed:
            self.reset()          # 不在配置的语言对里，当噪声
            self.last_event = "unsupported"
            return None
        if prob is None or prob < min_prob:
            self.reset()          # 置信度不够：不仅不切，还要打断连击
            self.last_event = "low_confidence"
            return None
        if lang == self.lang:
            self.streak += 1
        else:
            self.lang = lang
            self.streak = 1
        if self.streak >= max(1, int(need_streak)):
            self.reset()
            self.last_event = "switch"
            return lang
        self.last_event = "streak"
        return None


def describe_language_config_source(exists=None, text=None):
    """自动检测开关是仓库默认还是 config_local 覆盖。只读文本，不 exec。"""
    if exists is None:
        # REPO_ROOT 本身就是 Path（见 paths.py），不需要再 import pathlib.Path
        from realtime_subtitle.paths import REPO_ROOT
        path = REPO_ROOT / "config_local.py"
        exists = path.is_file()
        text = path.read_text(encoding="utf-8") if exists else ""
    if not exists:
        return "config.py（无 config_local.py）"
    if re.search(r"^AUTO_DETECT_LANGUAGE\s*=", text or "", re.M):
        return "config_local.py（覆盖 AUTO_DETECT_LANGUAGE）"
    return "config.py 默认（config_local.py 未覆盖自动检测）"


def language_startup_log_lines(
        auto_detect, source, target, interval, min_sec, min_prob, streak,
        cooldown, config_source, allowed):
    """启动诊断行。不含音频/字幕正文。"""
    status = "开" if auto_detect else "关"
    langs = ", ".join(allowed)
    return [
        f"🌐 自动语言检测: {status}（来源: {config_source}）",
        f"   当前语言对: {source} → {target}",
        f"   检测参数: 间隔{interval}s / 最短音频{min_sec}s / "
        f"置信度≥{min_prob} / 连击{streak} / 冷却{cooldown}s",
        f"   允许切换的源语言: {langs}",
    ]


def log_language_startup():
    """把生效的自动检测配置打进默认日志（不要求 SHOW_PERFORMANCE）。"""
    allowed = tuple(sorted({src for src, _ in language_pairs()}))
    for line in language_startup_log_lines(
            auto_detect=bool(getattr(config, "AUTO_DETECT_LANGUAGE", False)),
            source=config.SOURCE_LANGUAGE,
            target=getattr(config, "TARGET_LANGUAGE", "zh"),
            interval=getattr(config, "LANGUAGE_DETECT_INTERVAL", 4.0),
            min_sec=getattr(config, "LANGUAGE_DETECT_MIN_SEC", 3.0),
            min_prob=getattr(config, "LANGUAGE_SWITCH_MIN_PROB", 0.85),
            streak=getattr(config, "LANGUAGE_SWITCH_STREAK", 3),
            cooldown=getattr(config, "LANGUAGE_SWITCH_COOLDOWN", 20.0),
            config_source=describe_language_config_source(),
            allowed=allowed):
        print(line)


def language_switch_source_label(source):
    return {"manual": "手动", "auto": "自动检测", "rescue": "自愈后自动"}.get(
        source, source or "未知")


def _log_sig(value):
    if isinstance(value, float):
        return round(value, 2)
    return value


def format_language_detect_line(kind, **fields):
    """单条语言诊断。字段只有语言码/置信度/原因，不含音频或字幕正文。"""
    now = time.strftime("%H:%M:%S")
    if kind == "candidate":
        return (f"🌐 [{now}] 语言候选: {fields['lang']} "
                f"({fields['prob']:.2f}) 连击 {fields['streak']}/{fields['need']}")
    if kind == "cooldown":
        return f"🌐 [{now}] 语言检测跳过: 冷却中（剩余 {fields['remaining']:.0f}s）"
    if kind == "audio_short":
        return (f"🌐 [{now}] 语言检测跳过: 音频不足 "
                f"({fields['have']:.1f}s < {fields['need']:.1f}s)")
    if kind == "unsupported":
        return (f"🌐 [{now}] 语言检测跳过: 不支持的语言 "
                f"{fields['lang']} ({fields['prob']:.2f})")
    if kind == "low_confidence":
        return (f"🌐 [{now}] 语言检测跳过: 置信度不足 "
                f"{fields['lang']} {fields['prob']:.2f} < {fields['min_prob']}")
    if kind == "same_language":
        return (f"🌐 [{now}] 语言检测: 当前已是 {fields['lang']} "
                f"({fields['prob']:.2f})")
    if kind == "switch_request":
        return (f"🌐 [{now}] 语言切换请求: → {fields['lang']} "
                f"（来源: {fields['source_label']}）")
    if kind == "switch_applied":
        return (f"🌐 [{now}] 语言对已切换为: {fields['pair']} "
                f"（来源: {fields['source_label']}）")
    return f"🌐 [{now}] 语言检测: {kind}"


class LanguageDetectLogger:
    """状态变化才打日志，避免每帧刷屏。

    remaining / have 每帧都在变，只进文案、不进去重键——否则冷却窗口里
    每次 process_iter 都会打一行。
    """

    _DISPLAY_ONLY = frozenset({"remaining", "have"})

    def __init__(self):
        self._last_sig = None

    def line(self, kind, **fields):
        sig_fields = ((k, _log_sig(v)) for k, v in fields.items()
                      if k not in self._DISPLAY_ONLY)
        sig = (kind,) + tuple(sorted(sig_fields))
        if sig == self._last_sig:
            return None
        self._last_sig = sig
        return format_language_detect_line(kind, **fields)

    def emit(self, kind, **fields):
        text = self.line(kind, **fields)
        if text:
            print(text)
        return text


class DecodeHealth:
    """解码质量的连击判定。纯状态机，不碰模型也不碰线程，单测直接跑。

    职责只有一个：回答"当前源语言是不是设错了"。输入是每轮识别的
    avg_logprob 中位数（见 OnlineASRProcessor._decode_logprob）。

    ☠️ 为什么需要它，而不是靠已有的那几道防线：
      - `LANGUAGE_SWITCH_MIN_PROB` 是**切换前**的闸，而误切那两次报的都是
        1.00，它根本没有被触发的机会；
      - `_prompt_language_mismatch` 数的是拉丁功能词，被强制成中文时吐出来
        的全是汉字，一个都数不到（对这个场景是瞎的）；
      - `HALLUCINATION_BLACKLIST` 只认固定套话，接不住随机乱词。
    三道都在文字层面，而误切之后的文字是**通顺的**——只有解码器自己知道
    它在硬凑。

    只在**跨过阈值那一刻**返回一次 True（trigger 语义，不是电平语义），
    免得一直烂着的音频每轮都触发一次自愈。
    """

    def __init__(self):
        self.bad = 0
        self.fired = False

    def reset(self):
        self.bad = 0
        self.fired = False

    def feed(self, logprob, threshold, need_rounds):
        """喂一轮解码质量。返回 True 表示"刚跨过阈值，该自愈了"。

        ☠️ logprob=None（本轮没有语音段）是**跳过**：既不计坏、也不清零。
        三种取舍里只有这个是对的：
          - 算成"坏"：说话人一停顿、放一段纯音乐就攒够连击，误触发；
          - 算成"好"（清零）：更糟——真误切时，说话人每次换气都会把连击清掉，
            而德语对谈的停顿密度足以让 4 连击**永远攒不满**，整个自愈就是死的；
          - 跳过：连击的语义变成"最近 N 个**有语音**的轮次都解不动"，中间隔多少
            静音都不影响。而只要有一轮解码正常就会清零，所以不存在"两个相隔
            很远的孤立坏轮次也能攒够"——语言设对时正常轮次是连续不断的。
        """
        if logprob is None:
            return False
        if logprob >= threshold:
            self.reset()          # 解码正常：连击清零，自愈资格也一并收回
            return False
        self.bad += 1
        if self.bad < max(1, int(need_rounds)) or self.fired:
            return False
        self.fired = True         # 触发过就不再重复，等恢复正常再重新武装
        return True


class LanguageSwitchMixin:
    def request_switch_language(self, new_lang, source="manual", target=None):
        """切换源语言：写入待切换标志，由 ASR 线程在每批音频边界抢占执行。

        清上下文 + 改 SOURCE_LANGUAGE 必须在识别线程串行（热键线程先改语言
        会拿新语言参数识别旧缓冲，蹦出乱词）。不能只 submit(task) 排在
        _process_inbox 后面——收件箱持续非空时 inbox 循环不返回，切换会饿死
        （GPU 被游戏抢占的正是这种场景）。标志在 _process_inbox 每轮取音频
        前检查，最坏等当前这一批识别结束（~2.5s）而不是永远卡住。

        source: manual / auto / rescue，用于把「检测到了」和「真正应用了」分开记。

        target: 显式指定目标语言（面板按「中文 → 德语」这种整对来点）。
        不传就按 language_policy 定：手动切用 target_for，自动检测走
        resolve_target（不覆盖用户显式选过的目标）。

        ☠️ 语言、目标和来源是**一个请求整体**：字段分开写、分开读的话，后一个
        请求可能只覆盖掉其中一部分（消费到 en 却记成 auto 的来源），日志和冷却
        语义跟着一起错。这里在同一把 _asr_lock 内一次写完，消费端
        （_process_inbox）也在同一把锁内一次取完并清空。
        """
        label = language_switch_source_label(source)
        print(format_language_detect_line(
            "switch_request", lang=new_lang, source_label=label))
        with self._asr_lock:
            self._pending_lang_switch = new_lang
            self._pending_lang_source = source
            self._pending_lang_target = target
            if self._asr_scheduled:
                return  # 识别线程醒着，下一批边界会看到标志
            self._asr_scheduled = True
        try:
            self._asr_executor.submit(self._process_inbox)
        except RuntimeError:
            pass  # 程序正在退出

    def _check_decode_health(self):
        """解码质量烂到一定程度 = 源语言八成设错了，立刻启动自愈（ASR 线程）。

        自愈做三件事，都很便宜：
          1. **清 prompt 上下文**——垃圾文字经 initial_prompt 自我强化是这个
             故障"能锁死几分钟"的原因，断掉它比切不切语言更要紧，而且就算
             判断错了，代价也只是丢一次上下文（下一句自己会补回来）；
          2. **清掉检测冷却**——切换刚发生过时 LANGUAGE_SWITCH_COOLDOWN 会
             把检测憋住 20 秒，而这正是最需要马上重测的时刻；
          3. **降低连击要求**——见 config.LANGUAGE_RESCUE_STREAK。

        注意它**不自己切语言**。要不要切、切到哪个，仍然只由
        _maybe_detect_language 那条路（声学检测 + 置信度门 + 语言对白名单）
        决定；这里只是把它从冷却里放出来。所以误报的最坏结果是"白清一次
        上下文 + 早做一次 180ms 的检测"，不会凭解码质量瞎切语言。
        """
        if not getattr(config, "LANGUAGE_RESCUE_ENABLED", True):
            return
        if not getattr(config, "AUTO_DETECT_LANGUAGE", False):
            return  # 自动检测关着时无处可切，清上下文也没意义
        lp = getattr(self.processor, "last_avg_logprob", None)
        if config.SHOW_PERFORMANCE and lp is not None:
            print(f"   🩺 解码质量 avg_logprob {lp:+.2f}")
        if not self._decode_health.feed(
                lp,
                getattr(config, "LANGUAGE_RESCUE_LOGPROB", -1.0),
                getattr(config, "LANGUAGE_RESCUE_ROUNDS", 4)):
            # 恢复正常了就撤掉抢救状态，检测回到正常连击数
            if self._lang_rescue and not self._decode_health.bad:
                self._lang_rescue = False
            return

        self._lang_rescue = True
        self.processor.reset_prompt_context()
        self._lang_vote.reset()      # 旧连击是在"语言已经错了"的前提下攒的
        self._lang_detect_next = 0.0  # 撕掉冷却，下一句话就重测
        self._lang_in_cooldown = False
        name = language_name(config.SOURCE_LANGUAGE)
        print(f"🩺 解码质量持续异常（avg_logprob {lp:+.2f} < "
              f"{getattr(config, 'LANGUAGE_RESCUE_LOGPROB', -1.0)}），"
              f"疑似源语言不是{name}：已清上下文并立即重测语言")
        if self.on_status:
            self.on_status(f"🩺 字幕异常，正在重新判定语言…")

    def _lang_logger(self):
        log = getattr(self, "_lang_log", None)
        if log is None:
            log = LanguageDetectLogger()
            self._lang_log = log
        return log

    def _maybe_detect_language(self):
        """到点就做一次语言检测，够连击就请求切换语言对（跑在 ASR 线程）。

        整个功能默认关（config.AUTO_DETECT_LANGUAGE），理由写在 config 那边：
        误切一次的代价（丢缓冲 + 新语言经 prompt 自我强化）远大于晚切几秒。

        切换本身仍然走 request_switch_language——那条路已经处理好了
        "在每批音频边界串行执行 + clear_context + 递增 epoch 作废在飞的翻译"，
        这里绝不能自己去改 config.SOURCE_LANGUAGE。

        诊断日志不依赖 SHOW_PERFORMANCE（那个开关会把字幕正文打进 subtitle.log）。
        """
        if not getattr(config, "AUTO_DETECT_LANGUAGE", False):
            return
        interval = getattr(config, "LANGUAGE_DETECT_INTERVAL", 6.0)
        if interval <= 0:
            return
        now = time.time()
        log = self._lang_logger()
        if now < self._lang_detect_next:
            # 只有切语言后的冷却才记；两次检测之间的间隔等待保持静默。
            if getattr(self, "_lang_in_cooldown", False):
                log.emit("cooldown", remaining=self._lang_detect_next - now)
            return
        self._lang_in_cooldown = False
        self._lang_detect_next = now + interval

        allowed = {src for src, _ in language_pairs()}
        if len(allowed) < 2:
            return  # 只配了一个语言对，没什么可切的

        min_sec = getattr(config, "LANGUAGE_DETECT_MIN_SEC", 3.0)
        try:
            got = self.processor.detect_language(min_seconds=min_sec)
        except Exception as e:
            # 检测失败绝不能影响识别主链路——它只是个锦上添花的功能
            print(f"⚠️  语言检测失败（不影响字幕）: {e.__class__.__name__}: {e}")
            return
        if got is None:
            have = 0.0
            buf = getattr(self.processor, "buffer_seconds", None)
            if callable(buf):
                try:
                    have = float(buf())
                except Exception:
                    have = 0.0
            log.emit("audio_short", have=have, need=float(min_sec))
            return
        lang, prob = got

        # 抢救状态下用更短的连击：解码质量已经证明当前语言是错的，
        # "当前语言正确"这个先验塌了，再要 3 次确认纯属拖时间（见
        # _check_decode_health）。置信度门不放松——那是防切错的最后一道
        need_streak = (getattr(config, "LANGUAGE_RESCUE_STREAK", 2)
                       if self._lang_rescue
                       else getattr(config, "LANGUAGE_SWITCH_STREAK", 3))
        min_prob = getattr(config, "LANGUAGE_SWITCH_MIN_PROB", 0.85)
        new_lang = self._lang_vote.feed(
            lang, prob, config.SOURCE_LANGUAGE, allowed, min_prob, need_streak)
        event = getattr(self._lang_vote, "last_event", None)
        if event == "unsupported":
            log.emit("unsupported", lang=lang, prob=prob)
        elif event == "low_confidence":
            log.emit("low_confidence", lang=lang, prob=prob, min_prob=min_prob)
        elif event == "same_language":
            log.emit("same_language", lang=lang, prob=prob)
        elif event == "streak":
            log.emit("candidate", lang=lang, prob=prob,
                     streak=self._lang_vote.streak, need=need_streak)
        elif event == "switch":
            log.emit("candidate", lang=lang, prob=prob,
                     streak=need_streak, need=need_streak)
        if not new_lang:
            return

        # 刚切过就静默一段时间，掐掉来回横跳
        self._lang_in_cooldown = True
        self._lang_detect_next = now + getattr(config, "LANGUAGE_SWITCH_COOLDOWN", 20.0)
        name = language_name(new_lang)
        tname = language_name(target_for(new_lang))
        if self.on_status:
            self.on_status(f"🌐 检测到{name}，自动切换: {name} → {tname}")
        source = "rescue" if self._lang_rescue else "auto"
        self.request_switch_language(new_lang, source=source)

    def _apply_pending_lang_switch(self, new_lang, source=None, target=None):
        """在 ASR 线程内执行：清上下文 + 改语言对（与识别串行）。返回是否真切了。

        ☠️ 源语言和目标语言必须**一起改**。只改源语言的话，放中文视频会变成
        "中文→中文"：识别对了，翻译 prompt 还在要求输出中文，模型于是把原句
        抄一遍。目标语言从 LANGUAGE_PAIRS 查（见 target_for）。

        「请求的语言对 == 当前语言对」这一分支只能在这里判，不能在 UI 层判
        （见 app._request_language_pair 的注释）。此时**没有**旧语言音频要丢、
        也没有旧语言上下文要清，所以只刷 UI 和冷却：无变化的点击不该把用户
        正在看的字幕清空。

        ☠️ 这里**只用传进来的 source，绝不读写 self._pending_lang_source**。
        pending 字段的存/取/清一律留在 _asr_lock 里（request_switch_language 写、
        _process_inbox 取）。本函数跑在锁外且很慢（clear_context），这期间新的
        请求会入队——以前这里收尾时清一次 _pending_lang_source，清掉的其实是
        **下一条请求**的来源，于是一条 auto 请求被消费成 manual。给那行加锁
        也不对：它本来就没有资格动别人的请求。
        """
        source = source or "manual"  # 直接调用（热键/单测）的兼容默认
        old_source = getattr(config, "SOURCE_LANGUAGE", None)
        old_target = getattr(config, "TARGET_LANGUAGE", None)
        if target:
            new_target = target                      # 面板点的是整对，照办
        elif source == "manual":
            new_target = target_for(new_lang)
        else:
            # ☠️ 自动检测/抢救只改源语言：用户显式选过的目标语言不许被它覆盖
            new_target = language_policy.resolve_target(
                new_lang, old_source, old_target)
        changed = (new_lang != old_source or new_target != old_target)
        if changed:
            self.clear_context()
            # 语言对已经变了：这个代数之前发出的 UI 事件都属于旧语言，
            # 消费端（UI 槽）按它拒收，见 O04
            self._lang_revision = getattr(self, "_lang_revision", 0) + 1
        config.SOURCE_LANGUAGE = new_lang
        config.TARGET_LANGUAGE = new_target
        # 手动切（Ctrl+Alt+L）之后也要清投票：否则切换前攒的那点连击会跨过
        # 这次切换继续累加，可能刚切完就被自动检测又切回去
        if getattr(self, "_lang_vote", None) is not None:
            self._lang_vote.reset()
            self._lang_in_cooldown = True
            self._lang_detect_next = time.time() + getattr(
                config, "LANGUAGE_SWITCH_COOLDOWN", 20.0)
        # 抢救结束：语言已经换掉，健康度要从零重新观察。不清的话，切换后
        # 头几轮若仍然偏低会立刻二次触发，而那时候 prompt 才刚锚回去
        if getattr(self, "_decode_health", None) is not None:
            self._decode_health.reset()
            self._lang_rescue = False
        name = language_name(new_lang)
        tname = language_name(new_target)
        print(format_language_detect_line(
            "switch_applied", pair=f"{name} → {tname}",
            source_label=language_switch_source_label(source)))
        if self.on_status:
            self.on_status(
                f"🌐 已切换: {name} → {tname}" if changed
                else f"🌐 当前已是: {name} → {tname}")
        cb = getattr(self, "on_language_applied", None)
        if cb:
            cb(new_lang, new_target, getattr(self, "_lang_revision", 0))
        return changed
