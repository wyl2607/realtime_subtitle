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
# 源语言（Whisper语言代码）。运行中可用 Ctrl+Alt+L 在 LANGUAGE_CYCLE 里循环切换，
# 不需要重启：语言只是识别参数，模型本身是多语言的
SOURCE_LANGUAGE = "de"
LANGUAGE_CYCLE = ["de", "en"]  # 热键循环切换的语言列表，想看别的语言往里加
LANGUAGE_NAMES = {"de": "德语", "en": "英语", "fr": "法语", "es": "西班牙语", "ja": "日语"}
# 语言锚：initial_prompt 为空时（刚启动/切语言/长静音重置）用这句话垫底，
# 把解码器锚在目标语言上。开头几句嘈杂/游戏音容易被认成英文，而英文一旦
# 进了上下文 prompt 会自我强化好几分钟（2026-07-11 实测锁死近3分钟）；
# 已提交上下文若明显是"错误语言"也会被丢弃、换回这句锚。锚只做偏置，
# 不会出现在字幕里。加新语言时往这里补一句对应语言的自然句子。
LANGUAGE_SEED_PROMPTS = {
    "de": "Hallo zusammen, wir sprechen heute auf Deutsch über dieses Thema.",
    "en": "Hello everyone, today we are talking about this topic in English.",
}
WHISPER_TASK = "transcribe"  # "transcribe"=转录原语言, "translate"=翻译成英文
WHISPER_BEAM_SIZE = 3  # beam search 大小。whisper_streaming作者用5，这里用3保延迟
# 注：whisper_streaming作者在L40上实测 int8_float16 比 float16 慢~20%且质量更差，
# 如果换 float16 后单次识别耗时反而降了，就保持 float16

# ============ Qwen + Ollama 翻译配置 ============
# 质量升级选项：qwen3:14b 译文明显更自然（2026-07-09 在4070 12GB实测：
# 与Whisper同驻显存热态3.2秒/次、100% GPU，但显存零余量——专心精听可以
# 在 config_local.py 里改成 "qwen3:14b"，边玩游戏边看字幕时别用（会挤爆）
OLLAMA_MODEL = "qwen3:8b"  # Ollama 模型名称
OLLAMA_BASE_URL = "http://localhost:11434"  # Ollama API 地址

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
# 这么多词就不等标点直接送翻译（正常有标点的语音不会触发）
MAX_PENDING_WORDS = 24
ENERGY_THRESHOLD_SPEECH = 0.01  # 静音门：整块能量低于此且上一块也静音则不提交（省GPU）

# ============ 翻译批次 ============
# 积压句子合并成一次请求的字符上限：说话快时无限合并会让单次请求
# 越来越长（延迟和幻觉风险都涨），超限的留给下一轮
TRANSLATE_BATCH_MAX_CHARS = 300

# ============ 查词缓存 ============
# 点词查词 LRU 容量（词×语言）。重复点同一词不打 Ollama
LOOKUP_CACHE_MAX = 200

# ============ 草稿翻译（中文第一时间上屏）============
# 德语先行显示解决了德语的即时性，但中文要等句子凑齐(.!?)+正式翻译，
# 长句能滞后5-15秒。开启后：残句在翻译worker空闲时先翻一版"草稿中文"
# （浅蓝斜体显示），正式句对完成后自动替换。只用空闲算力，不抢正式翻译。
DRAFT_TRANSLATION = True
DRAFT_MIN_INTERVAL = 1.5  # 两次草稿翻译的最小间隔（秒），防止频繁打Ollama
DRAFT_MIN_WORDS = 3       # 残句至少这么多词才值得出草稿
DRAFT_TEXT_COLOR = "#8fb8e0"  # 草稿中文颜色（和正式中文#c8c8c8区分）

# ============ 游戏模式（Ctrl+Alt+G 一键降配）============
# 游戏和识别抢显卡时按热键临时降配：识别频率减半+贪心解码+关草稿中文。
# 字幕定稿延迟约从1-1.5秒涨到2-3秒，但GPU占用明显下降。再按一次恢复。
GAME_MODE_SUBMIT_SECONDS = 1.0  # 游戏模式下的提交节奏（正常0.5秒）
GAME_MODE_BEAM_SIZE = 1         # 游戏模式下贪心解码（正常beam=3）
GAME_MODE_DISABLE_DRAFT = True  # 游戏模式下关掉草稿中文（省Ollama请求）

# ============ 字幕窗口配置 ============
WINDOW_WIDTH = 1200  # 窗口宽度（像素）
WINDOW_HEIGHT = 300  # 窗口高度（像素）- 双语3条句对+live行需要的高度
WINDOW_X = 360  # 窗口X坐标（屏幕中心偏移）
WINDOW_Y = 750  # 窗口Y坐标（底部）
FONT_SIZE = 22  # 字体大小
FONT_FAMILY = "Microsoft YaHei, Arial"  # 字体
MAX_SUBTITLE_LENGTH = 400  # 字幕最大字符数（双语显示需要更多空间）
SHOW_BILINGUAL = True  # 同时显示德语原文和中文翻译
UNSTABLE_TEXT_COLOR = "#999999"  # live行里未稳定（还可能变）的德语尾部颜色
MAX_SENTENCE_PAIRS = 20  # 悬浮窗句对（德+中）条数上限；实际显示条数按窗口高度
                         # 精确排版测算，窗口拉大自动填满（更早的在📜历史窗口里）

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
    # 惯用语（直播实测被字面直译的）
    "Kohl nicht fett": "（惯用语 den Kohl nicht fett machen = 起不了多大作用，不是人名科尔）",
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
]

# ============ 字幕记录 ============
SAVE_TRANSCRIPT = True  # 把每条字幕（原文+译文+时间）存到文件，方便回看/搜索/学语言
TRANSCRIPT_DIR = "transcripts"  # 相对仓库目录，每天一个文件 YYYY-MM-DD.txt

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
SHOW_PERFORMANCE = False
# 每隔这么多秒打一行性能概况（识别p50/p90、缓冲峰值、翻译耗时等，0=关）。
# 这是 SHOW_PERFORMANCE=False 后仅剩的观测手段：约1行/分钟，撑不大日志
STATS_SUMMARY_INTERVAL = 60

# ============ 本机覆盖（放最后，能覆盖上面所有配置）============
# install.ps1 在没有NVIDIA显卡的机器上会生成 config_local.py
# （WHISPER_DEVICE="cpu" 等降级配置）；个人调参也可以写在那里，
# 不污染仓库文件（config_local.py 在 .gitignore 里）
try:
    from config_local import *  # noqa: F401,F403
except ImportError:
    pass
