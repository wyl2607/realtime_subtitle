"""
配置文件
所有可调参数集中管理
"""

# ============ Whisper 模型配置 ============
WHISPER_MODEL = "small"  # tiny, base, small, medium, large-v3 (small标点更好)
WHISPER_DEVICE = "cuda"  # cuda 或 cpu
WHISPER_COMPUTE_TYPE = "int8"  # float16, int8, int8_float16 (large-v3建议int8)
WHISPER_LANGUAGE = None  # None=自动检测，或指定如 "en", "zh", "ja"
WHISPER_TASK = "transcribe"  # "transcribe"=转录原语言, "translate"=翻译成英文
WHISPER_BEAM_SIZE = 3  # beam search 大小，large-v3用3就够了（减少延迟）
WHISPER_TEMPERATURE = 0.0  # 温度参数，0=确定性输出，提高标点一致性

# ============ Qwen + Ollama 翻译配置 ============
OLLAMA_MODEL = "qwen3:8b"  # Ollama 模型名称
OLLAMA_BASE_URL = "http://localhost:11434"  # Ollama API 地址
OLLAMA_USE_STREAM = False  # 是否使用流式输出（边生成边显示）

# ============ 音频配置 ============
SAMPLE_RATE = 16000  # 采样率（Hz）
CHANNELS = 2  # 音频通道数
CHUNK_SIZE = 4096  # 每次读取的帧数（减少处理频率）

# ============ 音频捕获层VAD配置（audio_capture.py使用）============
MIN_AUDIO_DURATION = 0.4  # 最小音频时长（秒）
MAX_AUDIO_DURATION = 0.5  # 最大音频时长（秒）
SILENCE_DURATION = 0.6  # 静音持续时长（秒）
ENERGY_THRESHOLD_SPEECH = 0.01  # 语音能量阈值

# ============ 音频上下文配置 ============
AUDIO_CONTEXT_WINDOW = 20  # Whisper识别时保留的音频片段数量（滑动窗口）

# ============ 翻译器配置 ============
LLM_TRANSLATION_HISTORY_SIZE = 5  # LLM翻译历史保留条数（用于去重和上下文）

# ============ 字幕窗口配置 ============
WINDOW_WIDTH = 1200  # 窗口宽度（像素）
WINDOW_HEIGHT = 150  # 窗口高度（像素）- 增加高度支持多行
WINDOW_X = 360  # 窗口X坐标（屏幕中心偏移）
WINDOW_Y = 750  # 窗口Y坐标（底部）
FONT_SIZE = 22  # 字体大小
FONT_FAMILY = "Microsoft YaHei, Arial"  # 字体
MAX_SUBTITLE_LENGTH = 300  # 字幕最大字符数（增加到300字符）

# ============ 字幕历史配置 ============
SUBTITLE_MAX_CHARS = 150  # 最多显示字符数
SUBTITLE_MAX_LINES = 2  # 最多显示行数
SUBTITLE_KEEP_HISTORY = False  # 禁用累积显示（whisper_streaming已处理）

# 窗口样式
BACKGROUND_COLOR = "rgba(0, 0, 0, 255)"  # 背景色（纯黑）
TEXT_COLOR = "white"  # 文字颜色
BORDER_COLOR = "rgba(255, 255, 255, 0.3)"  # 边框颜色
BORDER_RADIUS = 10  # 圆角半径
PADDING = "15px 20px"  # 内边距

# ============ 日志配置 ============
LOG_LEVEL = "INFO"  # 日志级别
SHOW_PERFORMANCE = True  # 显示性能指标
