"""
配置文件
所有可调参数集中管理
"""

# ============ Whisper 模型配置 ============
# large-v3-turbo: large-v3的加速蒸馏版，德语准确率明显好于medium，
# 速度与medium相当，int8下显存占用差不多（~2GB）
WHISPER_MODEL = "large-v3-turbo"  # tiny, base, small, medium, large-v3, large-v3-turbo
WHISPER_DEVICE = "cuda"  # cuda 或 cpu
WHISPER_COMPUTE_TYPE = "float16"  # float16, int8, int8_float16
# 源语言（Whisper语言代码）。运行中可用 Ctrl+Alt+L 在 LANGUAGE_PAIRS 里循环切换，
# 不需要重启：语言只是识别参数，模型本身是多语言的
SOURCE_LANGUAGE = "de"
# 翻译目标语言。以前"简体中文"是写死在 prompt 里的，加中→德之后必须参数化
TARGET_LANGUAGE = "zh"
# ☠️ Ctrl+Alt+L 循环的是**语言对**，不是单个源语言——切源语言而不切目标语言的话，
# 放中文视频会得到"中文→中文"。这个列表同时兼任 源语言→目标语言 的映射表，
# 自动检测（AUTO_DETECT_LANGUAGE）也只会在这里出现过的源语言之间切。
LANGUAGE_PAIRS = [("de", "zh"), ("zh", "de"), ("en", "zh")]
# 兼容：老的 config_local.py 里可能写着 LANGUAGE_CYCLE = ["de","en"]（只列源语言）。
# 仍然生效，目标语言按 LANGUAGE_PAIRS 查、查不到就用 TARGET_LANGUAGE。
LANGUAGE_CYCLE = None
LANGUAGE_NAMES = {"de": "德语", "en": "英语", "fr": "法语", "es": "西班牙语",
                  "ja": "日语", "zh": "中文"}
# 目标语言在 prompt 里的叫法（只在这里不一样时才需要写）。
# ☠️ zh 必须是"简体中文"而不是"中文"：不点明简体，模型有概率输出繁体。
# 参数化目标语言之前 prompt 里写死的就是"简体中文"，这里是在保持原行为。
TRANSLATION_TARGET_NAMES = {"zh": "简体中文"}

# ============ 自动语言检测（放中文视频自动切成中→德）============
# ☠️ **默认关**。不是因为没做好，是因为切错的代价很高：切语言会
# clear_context() 丢掉识别缓冲，而且新语言会通过 initial_prompt 自我强化——
# 避坑清单第 4 节记着"英文一旦被误认能锁死近 3 分钟"，LANGUAGE_SEED_PROMPTS
# 这套东西就是为此存在的。自动切换等于把那个风险交给一个概率判断。
# 想开就在 config_local.py 里写 AUTO_DETECT_LANGUAGE = True。
# 开了之后如果发现乱切：先把 LANGUAGE_SWITCH_STREAK 调大（最有效），
# 再考虑抬 LANGUAGE_SWITCH_MIN_PROB。
AUTO_DETECT_LANGUAGE = False
# 每隔这么多秒做一次检测。
# ☠️ 这个值和 LANGUAGE_SWITCH_STREAK 一起决定了**换语言时的垃圾字幕窗口**：
# 切换真正发生之前要攒够连击，而那段时间里新语言的音频是拿旧语言参数解码的，
# 出来的是废话。2026-08-12 真机实测（德语切回中文时）那 18 秒长这样：
#     China und China, China, China, China, China...
#     和国.和国.和国.
# 窗口 = STREAK × INTERVAL，而单次检测实测只要 **180ms**（large-v3-turbo
# float16，且几乎不随缓冲长度变化——30 秒 pad_or_trim，见避坑清单第 20 条）。
# 所以缩短间隔是最划算的旋钮：4 秒一次 = 窗口 12 秒、额外 GPU 约 4.5%。
# 嫌垃圾窗口长就继续往下调（3 秒 → 窗口 9 秒、6% GPU）；嫌费卡就往上调。
LANGUAGE_DETECT_INTERVAL = 4.0
# 缓冲里至少攒够这么多秒音频才值得检测——太短的片段检测极不可靠
LANGUAGE_DETECT_MIN_SEC = 3.0
# 置信度门槛：低于它的检测结果直接丢弃（不计入连击）
LANGUAGE_SWITCH_MIN_PROB = 0.85
# 连续这么多次检测到**同一个**新语言才真的切。这是防误切的主力旋钮：
# 3 次 × 6 秒 = 说话人得持续讲另一种语言约 18 秒才会触发，一句外语引用
# 或一段外语歌不会把整场切走
LANGUAGE_SWITCH_STREAK = 3
# 刚切过之后这么多秒内不再检测，掐掉来回横跳
LANGUAGE_SWITCH_COOLDOWN = 20.0
# 语言锚：initial_prompt 为空时（刚启动/切语言/长静音重置）用这句话垫底，
# 把解码器锚在目标语言上。开头几句嘈杂/游戏音容易被认成英文，而英文一旦
# 进了上下文 prompt 会自我强化好几分钟（2026-07-11 实测锁死近3分钟）；
# 已提交上下文若明显是"错误语言"也会被丢弃、换回这句锚。锚只做偏置，
# 不会出现在字幕里。加新语言时往这里补一句对应语言的自然句子。
LANGUAGE_SEED_PROMPTS = {
    "de": "Hallo zusammen, wir sprechen heute auf Deutsch über dieses Thema.",
    "en": "Hello everyone, today we are talking about this topic in English.",
    "zh": "大家好，我们今天用中文来聊一聊这个话题。",
}
WHISPER_TASK = "transcribe"  # "transcribe"=转录原语言, "translate"=翻译成英文
WHISPER_BEAM_SIZE = 3  # beam search 大小。whisper_streaming作者用5，这里用3保延迟
# 注：whisper_streaming作者在L40上实测 int8_float16 比 float16 慢~20%且质量更差，
# 如果换 float16 后单次识别耗时反而降了，就保持 float16

# ============ Qwen + Ollama 翻译配置 ============
# 2026-07-13 默认升级 qwen3:8b → qwen3.5:9b：24句真实转录A/B人工评估，
# 9b 语域明显更口语自然；加载后同为5.6GB、37/37层全GPU，显存占用不变。
# 回退：先 `ollama pull qwen3:8b`（本地已删），再 config_local.py 写
# OLLAMA_MODEL="qwen3:8b"。qwen3:14b 精听选项已被 9b 事实取代（也已删）。
OLLAMA_MODEL = "qwen3.5:9b"  # Ollama 模型名称
# ☠️ 必须写 127.0.0.1，不要写 localhost。Ollama 只监听 IPv4 127.0.0.1:11434
# （`netstat -ano | findstr 11434` 可验证，没有 IPv6 监听），而 Windows 上
# getaddrinfo("localhost") 返回 ::1 在前、127.0.0.1 在后，于是每次新建连接都要
# 先往 ::1 捅一刀——Windows 的 IPv6 环回**不会快速失败**，实测要 2021ms 才拒绝，
# 之后才回退到 IPv4。2026-08-04 实测（GPU 空闲、模型已驻留）：
#     翻译 p50 2.88秒 → 0.60秒     查词 p50 3.11秒 → 0.87秒
# 这 2.04 秒是每个请求都付的固定税，和显卡、模型、prompt 一概无关。
# 而且流式响应 done 后 break + close 会让连接无法复用（排干也没用，实测过），
# 所以每一句字幕都要重连一次、每一句都付满 2 秒。
# 仓库里的 .ps1 脚本一直用的就是 127.0.0.1，只有这一行是 localhost，
# 所以"脚本很快、字幕很慢"看着像 GPU 问题，其实是 DNS。
OLLAMA_BASE_URL = "http://127.0.0.1:11434"  # Ollama API 地址
# ☠️ 启动时会校验 OLLAMA_BASE_URL 解析出来的地址**全部是环回地址**，否则直接
# 拒绝启动（translator_queue._assert_local_ollama）。因为本程序会把识别出的
# 全部原文发到这个地址，而 README 承诺的是"不向任何云端发送音频或文本"——
# 没有这道校验的话，config_local.py 里改错一个字就能静默把系统全部声音的
# 转录（可能含语音通话）送出本机，屏幕上和日志里都看不出任何异常。
# 确实要用局域网里另一台机器的 Ollama：在 config_local.py 里设 True 显式声明。
ALLOW_REMOTE_OLLAMA = False
# Ollama GPU offload layers：None = 让 Ollama 按当前显存自动决定。
# 不要把某台机器测出的固定层数写死；小显存设备会因此把模型挤进系统内存，
# 而不同模型的总层数也不一样。需要手动限制时可在 config_local.py 覆盖为整数。
OLLAMA_NUM_GPU = None
# ☠️ 所有打 Ollama 的请求（翻译/草稿/查词/AI分析）必须用**同一个** num_ctx。
# Ollama 的 runner 是按 (模型, 上下文长度) 缓存的：换一个 num_ctx 就等于换一个
# runner，会把 5.6GB 模型整个重新装一遍。2026-08-04 实测（字幕程序在跑、
# 翻译流持续占用）——查词曾写死 num_ctx=2048 而翻译是 4096，于是每次点词都触发
# 重载：load_duration 6.9~8.7 秒，单次查词 10.4~12.5 秒；统一成 4096 之后
# load_duration 0.27 秒、单次查词 3.3~4.0 秒。所以这里只留一个常量，
# 别在任何请求里写字面量。
OLLAMA_NUM_CTX = 4096

# 翻译请求超时（流式下是"相邻数据块间隔"上限，不是总时长）。
# ☠️ 冷热两档不能合并成一个值：模型已在显存时 15 秒足够且能快速发现 Ollama 卡死；
# 但开机后第一次请求要等 Ollama 从磁盘读几个 GB——2026-08-02 实测 qwen3.5:9b
# 冷加载 33.8 秒，用 15 秒的话首句必被丢弃且永远不会有中文
OLLAMA_TIMEOUT = 15
OLLAMA_TIMEOUT_COLD = 90
# 冷热之外的两个"该给长超时"的信号（都只影响超时选择，不改模型/不改画面）。
# 起因：15 秒够不够用其实取决于这一刻 GPU 排不排得上队，而不只是模型在不在
# 显存里。首启时 Whisper 刚加载完、在集中消化启动积压，模型是热的但翻译排在
# ASR 后面拿不到卡，于是每句都先白烧满 15 秒（详见 translator_queue
# ._translate_timeout 的注释）。
# 1) ASR 收件箱攒够这么多块 = 识别正占着 GPU，翻译直接用长超时。
#    默认 4 块（CHUNK_SUBMIT_SECONDS=0.5 时约 2 秒音频）；设 0 关闭这条规则
TRANSLATE_SLOW_BACKLOG_BLOCKS = 4
# 2) 超时之后维持长超时多久。没有它的话重试一成功就翻回短超时，下一句再超一次
TRANSLATE_SLOW_STICKY_SEC = 60
# 首句翻译最多等后台预热线程这么久（等它落地再发请求，避免两边各自计时）
OLLAMA_WARM_WAIT = 60
# 翻译队列硬顶（字符）：Ollama 半死时排空速率远低于产出，超了丢最旧的句子。
# 德语原文不受影响，丢的只是这些句子的中文
TRANSLATE_QUEUE_MAX_CHARS = 3000
# 连续这么多次翻译失败 → 熔断，期间直接显示德语不再发请求
TRANSLATE_FAIL_STREAK_OPEN = 3
TRANSLATE_CIRCUIT_SEC = 30  # 熔断持续秒数，到期后下一句正常翻译即为探测

# ============ 音频配置 ============
SAMPLE_RATE = 16000  # 采样率（Hz）
CHUNK_SIZE = 4096  # 每次读取的帧数（减少处理频率）
# 捕获哪路系统声音：空字符串 = 系统「默认播放设备」的 loopback。
# 非空则按设备名子串匹配（不区分大小写），例如 "FiiO" / "Speakers" / "Headphones"。
# 可在 config_local.py 里写，或运行中在 ⚙️ 面板改（约 5 秒内热切换）。
LOOPBACK_DEVICE_NAME = ""

# ============ 流式识别配置（streaming_asr.py / audio_capture.py）============
# local agreement 增量识别（2026-07-06 重写）：不再由能量VAD切"语音片段"，
# 采集端按固定节奏喂音频块，识别端整缓冲识别+词级前缀提交
CHUNK_SUBMIT_SECONDS = 0.5  # 采集端每攒够这么多秒就提交一块（识别节奏）
BUFFER_TRIM_SEC = 12.0  # 识别音频缓冲超过这么多秒就在已完成segment处裁剪
BUFFER_KEEP_SEC = 8.0  # segment裁剪不满足条件时的兜底：按已提交词边界裁到只剩这么多秒
IDLE_FLUSH_SEC = 2.0  # 静音这么多秒后，把未提交尾部/未翻译残句冲出去
# 聊天/嘈杂语音下Whisper经常整段不打标点，句子永远凑不齐 → 残句超过
# 这么多词就不等标点直接送翻译（正常有标点的语音不会触发）。
# 分句合并上线后它还兼任"合并别把句子拖太长"的安全阀
MAX_PENDING_WORDS = 24
# 无空格语言（中文/日文）用这个：上面那条按 .split() 数词，而中文整段永远
# 等于 1 个"词"，那道兜底对中文是死的。60 字约等于德语 24 词的信息量
MAX_PENDING_CHARS = 60
# ☠️ 不用空格分词的书写系统。切句终止符、残句长度兜底、复读压缩三处的规则
# 都是按"空格分词的拉丁语系"写的，这些语言必须走另一套（见 translator_queue
# 的 _no_space_language）。加韩语/泰语等往这里补即可。
NO_SPACE_LANGUAGES = ("zh", "ja")

# ============ 分句边界（2026-08-02）============
# Whisper 会在缩写/序数/口语停顿处打句号，只按 .!? 切的话 38.5% 的翻译单元
# 是半截句（19个转录文件23526句的实测），碎片单独送翻会翻错。
# 句号后面的词还没出来时最多扣留这么久再判定；连续说话时下一块音频 0.4 秒
# 就到、根本等不到这个上限，一段话讲完的最后一句最多慢这么多
SENTENCE_HOLD_SEC = 0.6
# 自带句号的德语缩写（小写、去掉末尾的点后比较）。往里加词条很安全：
# 只影响"这个点算不算句尾"，误收的代价是两句话合成一次翻译请求
SENTENCE_ABBREVIATIONS = {
    "bzw", "z.b", "zb", "ca", "usw", "u.a", "d.h", "ggf", "evtl", "inkl",
    "bspw", "sog", "vgl", "etc", "dr", "prof", "nr", "abs", "bzgl", "ggfs",
    "mio", "mrd", "jhd", "u.s.w", "z.t", "i.d.r", "st",
}
ENERGY_THRESHOLD_SPEECH = 0.01  # 静音门：整块能量低于此且上一块也静音则不提交（省GPU）

# ============ ASR 收件箱硬顶 ============
# GPU被抢时收件箱攒块、识别追上后消化——正常滞后几十秒，宁可滞后不丢词。
# 但识别线程若彻底卡死（驱动挂死等），收件箱会无限增长吃内存。超过这个
# 块数（240块×0.5秒≈2分钟积压）说明已无实时性可言，丢最旧的块保内存
ASR_INBOX_MAX_BLOCKS = 240

# ============ 翻译批次 ============
# 积压句子合并成一次请求的字符上限：说话快时无限合并会让单次请求
# 越来越长（延迟和幻觉风险都涨），超限的留给下一轮
TRANSLATE_BATCH_MAX_CHARS = 300

# ============ 查词缓存 ============
# 点词查词 LRU 容量（词×语言）。重复点同一词不打 Ollama。
# 200→800（2026-08-04）：一条缓存不到 200 字节，800 条也就 ~150KB，
# 而德语词形不做还原（gehen/geht/ging 各占一条），精听一整天 200 条盖不住。
LOOKUP_CACHE_MAX = 800
# 查词缓存落盘文件（相对仓库目录，在 .gitignore 里）。退出时写、启动时读，
# 跨会话保留——学德语时高频词第二天还是秒回，不用再付一次 3.5 秒。
# 设成 None 或空串即关闭持久化。
LOOKUP_CACHE_FILE = "lookup_cache.json"

# ============ AI 分析（🤖按钮 / 点词深度解释）============
# 本地 Ollama 先出结果，弹窗里"问更强的AI"再跳网页版追问。
# {query} 会被 URL-encode 后替换；可改成 ChatGPT 等（如
# "https://chatgpt.com/?q={query}&hints=search"）。
# ⚠️ 这是整个程序**唯一**会把内容送出本机的路径（而且必须用户点那个按钮）：
# 最近几分钟的德语原文截 300 字拼成问句，交给系统浏览器打开。本程序抓的是
# 系统全部声音，可能含语音通话内容。不想要这个按钮就把模板设成空串。
AI_ANALYSIS_WEB_URL_TEMPLATE = "https://grok.com/?q={query}"
AI_CONTEXT_WINDOW_MINUTES = 5      # 🤖 分析最近几分钟的句对
AI_CONTEXT_MAX_CHARS = 1400        # 喂给本地模型的德文上限；超了从最旧丢

# ============ 草稿翻译（中文第一时间上屏）============
# 德语先行显示解决了德语的即时性，但中文要等句子凑齐(.!?)+正式翻译，
# 长句能滞后5-15秒。开启后：残句在翻译worker空闲时先翻一版"草稿中文"
# （浅蓝斜体显示），正式句对完成后自动替换。只用空闲算力，不抢正式翻译。
DRAFT_TRANSLATION = True
DRAFT_MIN_INTERVAL = 1.5  # 两次草稿翻译的最小间隔（秒），防止频繁打Ollama
DRAFT_MIN_WORDS = 3       # 残句至少这么多词才值得出草稿
# ☠️ 无空格语言必须另算，和 MAX_PENDING_WORDS / MAX_PENDING_CHARS 同一个坑：
# 上面那条按 .split() 数词，而中文残句整段恒等于 1 个"词"，`1 < 3` 永真——
# 于是**放中文视频时草稿翻译整个功能是死的**，一次都不会触发。
# 后果不是"少个锦上添花"：中文这边 _boundary_is_real 恒真、不用等
# SENTENCE_HOLD_SEC，本该比德语快一拍，可草稿一死，长句就退化成
# "等凑成完整句 + 一次完整 Ollama"，新闻播报那种 30 字以上的句子要 3-8 秒。
# 8 字 ≈ 德语 3 词（按 MAX_PENDING_CHARS=60 ≈ MAX_PENDING_WORDS=24 的换算）
DRAFT_MIN_CHARS = 8
CHINESE_TEXT_COLOR = "#c8c8c8"  # 正式中文颜色（句对里的译文行）
DRAFT_TEXT_COLOR = "#8fb8e0"  # 草稿中文颜色（和正式中文区分）

# ============ 「性能」模式的轻量翻译模型 ============
# 给游戏腾~3GB显存，GPU争用下tok/s也快得多。qwen3.5:4b(3.4GB)基准分还略高于
# 老qwen3:8b，质量不降级。设成 None 则「性能」模式不切模型（小显存机器的
# install.ps1 会这么写，防止反向升到大模型）
GAME_MODE_OLLAMA_MODEL = "qwen3.5:4b"

# ============ 字幕窗口配置 ============
# 位置/大小/字号在用户拖过之后就以 window_state.json 为准，下面这组只在
# 「首次运行」生效，而且不是直接用：window_geometry.default_geometry() 拿
# 宽高按实际屏幕算贴底居中的落点，字号按屏幕缩放倍率放大。
# 这两个绝对坐标在 1366x768 的小笔记本上会让整窗掉出屏幕，所以 X/Y 首次运行
# 时**不会被使用**（写在 config_local.py 里也一样）——想固定位置就直接拖窗口，
# 拖完的位置会被记住。
WINDOW_WIDTH = 1200  # 窗口宽度（像素，小屏上会按屏宽收窄）
WINDOW_HEIGHT = 300  # 窗口高度（像素）- 双语3条句对+live行需要的高度
WINDOW_X = 360  # 窗口X坐标（仅在拿不到屏幕信息的兜底路径用）
WINDOW_Y = 750  # 窗口Y坐标（同上）
FONT_SIZE = 22  # 字体大小（首次运行按屏幕 DPI 缩放倍率放大）

# ============ 电视全屏模式（📺）============
TV_FONT_SIZE = 64       # 全屏大字字号（Ctrl+滚轮调节）
TV_FONT_SIZE_MIN = 24
TV_FONT_SIZE_MAX = 160
FONT_FAMILY = "Microsoft YaHei, Arial"  # 字体
MAX_SUBTITLE_LENGTH = 400  # 字幕最大字符数（双语显示需要更多空间）
SHOW_BILINGUAL = True  # 同时显示德语原文和中文翻译
UNSTABLE_TEXT_COLOR = "#999999"  # live行里未稳定（还可能变）的德语尾部颜色
MAX_SENTENCE_PAIRS = 20  # 悬浮窗句对（德+中）条数上限；实际显示条数按窗口高度
                         # 精确排版测算，窗口拉大自动填满（更早的在📜历史窗口里）

# ============ 模式（⚙️面板四个按钮 / Ctrl+Alt+G 快捷跳「性能」）============
# 2026-08-02 合并：以前"场景预设"（只动5个面板键）和"Ctrl+Alt+G 游戏模式"
# （另切 beam 和翻译模型、且是"开关+还原快照"语义）是两套都叫游戏的机制，
# 命名和行为都混。现在统一成四个平等并列的模式——点哪个就整套套用哪套值，
# 没有"临时开关/退出时还原"这层。「性能」额外把翻译模型换成
# GAME_MODE_OLLAMA_MODEL，其它模式切回启动时的基线模型。
PRESETS = {
    "直播": dict(CHUNK_SUBMIT_SECONDS=0.5, IDLE_FLUSH_SEC=2.0,
                 MAX_SENTENCE_PAIRS=20, SHOW_BILINGUAL=True,
                 DRAFT_TRANSLATION=True, WHISPER_BEAM_SIZE=3,
                 TRANSLATION_STYLE="新闻访谈"),
    "看剧": dict(CHUNK_SUBMIT_SECONDS=0.5, IDLE_FLUSH_SEC=2.0,
                 MAX_SENTENCE_PAIRS=6, SHOW_BILINGUAL=True,
                 DRAFT_TRANSLATION=True, WHISPER_BEAM_SIZE=3,
                 TRANSLATION_STYLE="影视对白"),
    "性能": dict(CHUNK_SUBMIT_SECONDS=1.0, IDLE_FLUSH_SEC=2.5,
                 MAX_SENTENCE_PAIRS=4, SHOW_BILINGUAL=False,
                 DRAFT_TRANSLATION=False, WHISPER_BEAM_SIZE=1,
                 TRANSLATION_STYLE="影视对白"),
    "精听": dict(CHUNK_SUBMIT_SECONDS=0.4, IDLE_FLUSH_SEC=1.5,
                 MAX_SENTENCE_PAIRS=20, SHOW_BILINGUAL=True,
                 DRAFT_TRANSLATION=True, WHISPER_BEAM_SIZE=3,
                 TRANSLATION_STYLE="精听直译"),
}

# ============ 翻译语域 ============
# 以前 prompt 里无条件写死"你是德语影视剧的字幕翻译…口语俚语粗话按中文对白
# 习惯翻"——看新闻直播（tagesschau/党代会这类，用户的主要场景）时语域是错的，
# 会把播报翻得过分口语。现在跟着模式走（PRESETS 里的 TRANSLATION_STYLE）。
#
# ☠️ {source} / {target} 是占位符，发请求前按当前语言对替换（中→德时
# {target} 就是"德语"）。**别把它们换回写死的"中文"**——那样中→德会变成
# 一边让模型输出德语、一边让它"中文要通顺"，模型会照着规则输出中文。
# 替换用的是字符串 replace 不是 .format()，所以规则里出现 { } 不会炸。
TRANSLATION_STYLE = "新闻访谈"
TRANSLATION_STYLE_PROMPTS = {
    "新闻访谈": {
        "role": "新闻/访谈节目的字幕翻译",
        "rules": ("1. 这是新闻播报或访谈：{target}要通顺、准确、克制，不要加戏也不要过分口语化\n"
                  "2. 机构名、职务、数字、地名必须准确；{source}长句按{target}习惯拆成短句\n"
                  "3. 说话人自己的口语停顿/重复可以省略，但事实一个都不能改"),
    },
    "影视对白": {
        "role": "影视剧的字幕翻译",
        "rules": ("1. 这是剧集对白：口语、俚语、粗话按{target}对白的习惯翻，保持角色语气，不要书面腔\n"
                  "2. 结合上下文理解句意，不要机械直译；歌词就按歌词翻\n"
                  "3. 专有名词保持一致"),
    },
    "精听直译": {
        "role": "{source}学习者的精听字幕翻译",
        "rules": ("1. 学习用途：尽量贴着原文的结构和用词翻，方便对照着学，但{target}仍要通顺\n"
                  "2. 原文有的信息一个都不要漏，也不要自行补充原文没有的内容\n"
                  "3. 专有名词保持一致"),
    },
}

# 窗口样式
BACKGROUND_OPACITY = 235  # 背景不透明度 0-255（255=纯黑不透；设置面板有滑块，会记住）
TEXT_COLOR = "white"  # 文字颜色
BORDER_COLOR = "rgba(255, 255, 255, 0.3)"  # 边框颜色
BORDER_RADIUS = 10  # 圆角半径
PADDING = "15px 20px"  # 内边距

# ============ 翻译术语表（德 → 中）============
# 人名/党派/固定政治术语的标准译名，防止Qwen自由发挥。
# 只有当前句子或上下文里真出现的词条才会被注入prompt（translator_queue里过滤），
# 所以放心往里加，不会撑爆模型上下文。
GLOSSARY = {
    # 政党
    "CDU": "基民盟",
    "CSU": "基社盟",
    "SPD": "社民党",
    "AfD": "德国选择党",
    "FDP": "自民党",
    "Die Grünen": "绿党",
    "Die Linke": "左翼党",
    "BSW": "瓦根克内希特联盟",
    # 政治人物
    "Merz": "梅尔茨",
    "Weidel": "魏德尔",
    "Chrupalla": "克鲁帕拉",
    "Scholz": "朔尔茨",
    "Steinmeier": "施泰因迈尔",
    "Söder": "泽德",
    "Habeck": "哈贝克",
    "Baerbock": "贝尔伯克",
    "Wagenknecht": "瓦根克内希特",
    "Pistorius": "皮斯托里乌斯",
    "Klingbeil": "克林拜尔",
    # 机构/固定术语
    "Bundestag": "联邦议院",
    "Bundesrat": "联邦参议院",
    "Bundeskanzler": "联邦总理",
    "Bundesregierung": "联邦政府",
    "Ampel": "红绿灯联盟",
    "Schuldenbremse": "债务刹车",
    "Sondervermögen": "特别基金",
    "Brandmauer": "防火墙（主流政党对选择党的隔离政策）",
    "Koalitionsvertrag": "联合执政协议",
    "Landtagswahl": "州议会选举",
    "Bundestagswahl": "联邦议院选举",
    "Parteitag": "党代会",
    "Fraktion": "议会党团",
    "Kanzlerkandidat": "总理候选人",
    "Bürgergeld": "公民金",
    "Mindestlohn": "最低工资",
    "öffentlich-rechtlich": "公共广播",
    "Rundfunkbeitrag": "广播电视费",
    "Verfassungsschutz": "联邦宪法保卫局",
    "Grundgesetz": "基本法",
    # 地名（2026-08-02 实测译名在"塞乌塔/图塔"之间摇摆，标准译名是休达）
    "Ceuta": "休达（西班牙在北非的飞地）",
    "Melilla": "梅利利亚（西班牙在北非的飞地）",
    # 惯用语（直播实测被字面直译的）
    "Kohl nicht fett": "（惯用语 den Kohl nicht fett machen = 起不了多大作用，不是人名科尔）",
}

# ============ ASR 专有名词纠错表 ============
# GLOSSARY 只管翻译 prompt，修不了 Whisper 本身听错的词（听成别的词之后，
# 术语表根本匹配不上）。这里在词流入口做确定性替换：
#   key = 小写、去标点的错词；value = 正确写法（大小写按德语规范写）
# 2026-08-02 转录实证：Ceuta 被听成 Theuta（然后译名在"塞乌塔/图塔"之间摇摆）、
# Hubschrauber 听成 Hubschreiber。
# ☠️ 只放 1:1 的词级替换，且必须是"听错"而不是"同义词"——替换是确定性的
# （相邻两次识别得到同样结果），local agreement 的前缀一致判定才不会被扰动
ASR_CORRECTIONS = {
    "theuta": "Ceuta",
    "hubschreiber": "Hubschrauber",
}

# ============ 幻觉字幕黑名单 ============
# Whisper在静音/纯音乐段落会凭空生成电视字幕组的版权惯用语
# （训练数据里的电视字幕都带这些，实测2小时里606条字幕混进17条）。
# 识别出的片段只要包含以下任一子串（不区分大小写）就直接丢弃。
# 注意只放"真人说话几乎不可能出现"的词，避免误杀
HALLUCINATION_BLACKLIST = [
    "untertitel",          # "Untertitelung des ZDF, 2020" 及各种变体
    "amara.org",           # 社区字幕平台署名
    "copyright",           # "Copyright WDR 2021" 等
    "danke fürs zuschauen",
    "vielen dank für's zuschauen",
    "thanks for watching",  # 英语源的同类幻觉
    "subtitles by",
    # ☠️ 中文源的同类幻觉。加中→德那轮这个表一条中文都没有，也就是**放中文
    # 视频时这道防线是空的**。下面只收"真人几乎不可能说出口"的固定套话，
    # 每一条都够长够怪，不会撞上正常内容。
    #
    # 刻意**没有**收 "订阅" / "频道" / "谢谢观看" 这类看着很像的词：它们在
    # 真人说话里是常规词汇（讲订阅制的段落反复出现"订阅费"，科技评测的片尾
    # 本来就会请观众订阅频道），收了等于把整段真话静默丢掉，而用户屏幕上
    # 只是缺了一句、毫无提示。
    # ☠️ 往这里加词条前，先在自己的 transcripts/ 里 grep 一遍候选词、确认它
    # 不出现在真人说话中，别凭印象加。（存档是本机私有数据，核对结果留在
    # 本地即可，不要把原文抄进仓库。）
    "不吝点赞",          # "请不吝点赞 订阅 转发 打赏支持明镜与点点栏目"
    "明镜与点点",        # 同上，换个说法的变体
    "优独播剧场",        # "优优独播剧场——YoYo Television Series Exclusive"
]

# ============ 字幕记录 ============
SAVE_TRANSCRIPT = True  # 把每条字幕（原文+译文+时间）存到文件，方便回看/搜索/学语言
TRANSCRIPT_DIR = "transcripts"  # 相对仓库目录，每天一个文件 YYYY-MM-DD.txt
# 启动时清理超过这么多天的存档文件。**默认 0 = 永久保留**，和一直以来的行为
# 一致——transcripts 是拿来回看和学德语的，自动删掉用户攒的语料不该是默认。
# ⚠️ 但要知道它的另一面：本程序抓的是**系统全部声音**（可能含语音通话），
# 存档是明文、按天一个文件、SAVE_TRANSCRIPT 默认开着，所以装多久就攒多久。
# 介意的话在 config_local.py 里写个天数（如 TRANSCRIPT_KEEP_DAYS = 30），
# 完全不想留就 SAVE_TRANSCRIPT = False。
TRANSCRIPT_KEEP_DAYS = 0

# ============ 感叹词直译词典 ============
# ≤3词的句子命中词典就跳过 Ollama 直接上屏（游戏/聊天场景实测21%的字幕
# 是这类，每条单独打一次Ollama纯浪费GPU）。key=小写去首尾标点的德语。
# 只收含义无歧义的词条；有语境依赖的（如 "gerne"）别放进来
INTERJECTION_TRANSLATIONS = {
    "ja": "是", "nein": "不", "gut": "好", "okay": "好的", "ok": "好的",
    "was": "什么？", "warum": "为什么？", "wow": "哇", "whoa": "哇哦",
    "hey": "嘿", "danke": "谢谢", "genau": "没错", "richtig": "对",
    "super": "太棒了", "geil": "太爽了", "krass": "离谱", "scheiße": "该死",
    "mist": "糟糕", "verdammt": "可恶", "warte": "等等", "moment": "等一下",
    "hilfe": "救命", "vorsicht": "小心", "egal": "无所谓", "stimmt": "没错",
    "na ja": "嗯……", "ach so": "原来如此", "alles klar": "明白了",
    "keine ahnung": "不知道", "oh mein gott": "我的天哪", "mein gott": "天哪",
    "oh gott": "天哪", "komm schon": "来吧", "sehr gut": "非常好",
    "geh": "走！", "lauf": "快跑！", "los": "快！", "komm": "过来",
    "sehr lustig": "真好笑", "kein problem": "没问题", "warte mal": "等一下",
}

# ============ 日志配置 ============
# 默认关闭：长直播时每 0.5s 刷一行会把 subtitle.log 撑很大。
# 排障时在 config_local.py 里设 True，或临时改这里。
# ⚠️ 隐私：打开它之后 subtitle.log 里会有**识别出的原文和译文正文**
# （识别结果、草稿、查词、收尾翻译都会打）。而 README 的常见问题和
# .github/ISSUE_TEMPLATE 都会让你把日志尾部贴到 issue 里——排障完记得关掉，
# 贴之前也扫一眼有没有你不想公开的内容（本程序抓的是系统全部声音）。
SHOW_PERFORMANCE = False
# 每隔这么多秒打一行性能概况（识别p50/p90、缓冲峰值、翻译耗时等，0=关）。
# 这是 SHOW_PERFORMANCE=False 后仅剩的观测手段：约1行/分钟，撑不大日志
STATS_SUMMARY_INTERVAL = 60

# ============ 本机覆盖（放最后，能覆盖上面所有配置）============
# install.ps1 在没有NVIDIA显卡的机器上会生成 config_local.py
# （WHISPER_DEVICE="cpu" 等降级配置）；个人调参也可以写在那里，
# 不污染仓库文件（config_local.py 在 .gitignore 里）
# ☠️ 加载失败**必须出声**。以前这里是裸 `except Exception: pass`：
# config_local.py 里打错一个字（或 install.ps1 生成的降级配置语法有问题），
# 结果是静默退回全部默认值——没显卡的机器于是拿着 WHISPER_DEVICE="cuda"
# 去跑，然后在加载 cublas 时崩，而日志里一个字都不会提到 config_local，
# 排障方向全错。捕获照旧（坏配置不该挡住启动），但要把原因打出来。
try:
    # config_local.py lives at the repo root (next to main.py), not inside the package
    import importlib.util
    from realtime_subtitle.paths import REPO_ROOT as _root
    _local = _root / "config_local.py"
    if _local.is_file():
        _spec = importlib.util.spec_from_file_location("config_local", _local)
        _mod = importlib.util.module_from_spec(_spec)
        assert _spec.loader is not None
        _spec.loader.exec_module(_mod)
        globals().update({k: v for k, v in vars(_mod).items() if not k.startswith("_")})
except Exception as _e:
    print(f"⚠️  config_local.py 加载失败，本机配置【未生效】，正在使用 config.py 默认值: "
          f"{_e.__class__.__name__}: {_e}")
    print("   （没有显卡的机器会因此按 CUDA 档启动并在加载模型时报错——先修这个文件）")
