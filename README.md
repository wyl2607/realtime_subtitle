# 🎬 实时字幕翻译软件

<div align="center">

[![Python](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License](https://img.shields.io/badge/License-Apache%202.0-green.svg)](LICENSE)
[![Whisper](https://img.shields.io/badge/Whisper-Faster--Whisper-orange.svg)](https://github.com/SYSTRAN/faster-whisper)
[![Qwen](https://img.shields.io/badge/LLM-Qwen-purple.svg)](https://github.com/QwenLM/Qwen)

**基于 Faster-Whisper + Qwen 的高质量实时字幕翻译系统**

[功能特点](#-功能特点) • [快速开始](#-快速开始) • [配置说明](#-配置说明) • [常见问题](#-常见问题)

</div>

---

## 📖 项目简介

这是一款完全本地运行的实时字幕翻译软件，能够捕获系统音频输出并实时生成高质量的中文字幕。采用先进的 AI 技术栈，提供影院级的字幕体验。

> ⚠️ **重要提示**：本项目需要本地安装 **Ollama** 来运行 Qwen 大语言模型进行翻译推理，所有处理完全在本地完成，无需联网，保护隐私安全。

### 核心技术

- 🎯 **语音识别**：Faster-Whisper（OpenAI Whisper 的优化实现）
- 🤖 **智能翻译**：Qwen 大语言模型（通过本地 Ollama 运行）
- 🎵 **音频捕获**：WASAPI Loopback（系统级音频捕获）
- 🖼️ **界面框架**：PyQt5（跨平台 GUI）

---

## ✨ 功能特点

### 🚀 核心功能

- ✅ **完全本地运行** - 所有处理在本地完成，隐私安全
- ✅ **通用音频支持** - 支持任何播放声音的应用（YouTube、Netflix、本地视频等）
- ✅ **超低延迟** - 0.3-0.8秒的端到端延迟
- ✅ **高质量翻译** - 基于 Qwen 大模型，理解上下文，翻译自然流畅
- ✅ **智能断句** - 自动检测语音停顿，准确分句
- ✅ **去重机制** - 避免重复翻译，节省资源
- ✅ **上下文管理** - 保留历史句子，提高翻译连贯性

### 🎨 用户体验

- 🖼️ **悬浮窗显示** - 置顶显示，不干扰其他操作
- ⚙️ **GUI 参数调节** - 可视化调整各项参数，无需修改代码
- 📊 **性能监控** - 实时显示处理时间和队列状态
- 🎛️ **灵活配置** - 支持自定义能量阈值、时长、窗口样式等

### 💡 智能特性

- 🧠 **情境感知翻译** - 根据上下文判断词语真实含义（如分手场景中的"I'm done"翻译为"我受够了"而非"我完成了"）
- 📦 **短句合并** - 自动合并短句子（≤20词），提高翻译质量
- ⏱️ **静音触发** - 静音超过0.5秒自动翻译，确保最后一句不遗漏
- 🔄 **连续性检测** - 句子连续出现2次即确认翻译，响应迅速

---

## 🖥️ 系统要求

### 最低配置

- **操作系统**：Windows 10/11（64位）
- **Python**：3.8 或更高版本
- **GPU**：NVIDIA GPU，4GB+ 显存
- **内存**：8GB RAM
- **硬盘**：5GB 可用空间（模型存储）

### 推荐配置

- **GPU**：RTX 3060 或更高
- **内存**：16GB RAM
- **处理器**：Intel i5 / AMD Ryzen 5 或更高

### 软件依赖

- **Ollama**：⚠️ **必须安装**，用于本地运行 Qwen 翻译模型（[下载地址](https://ollama.com/)）
- **CUDA Toolkit**：11.8+ （GPU 加速，推荐）

---

## 🚀 快速开始

### ⚠️ 前置要求：安装 Ollama（必需）

本项目使用 **Ollama** 在本地运行 Qwen 大语言模型进行翻译推理，这是项目正常运行的**必要条件**。

**第一步：安装 Ollama**

1. 从 [Ollama官网](https://ollama.com/) 下载并安装 Ollama
2. 安装后，Ollama 会在后台运行（默认端口 11434）
3. 拉取 Qwen 翻译模型：
```bash
ollama pull qwen3:8b
```

**验证 Ollama 安装：**
```bash
# 检查 Ollama 是否运行
ollama list

# 测试模型（应该能看到输出）
ollama run qwen3:8b "Hello"
```

### 第二步：安装本项目

```bash
# 克隆项目
git clone https://github.com/your-username/realtime_subtitle.git
cd realtime_subtitle

# 创建虚拟环境（推荐）
python -m venv venv
venv\Scripts\activate  # Windows

# 安装 Python 依赖
pip install -r requirements.txt
```

### 第三步：启动 Ollama 服务（如果未运行）

```bash
# Windows: Ollama 通常会自动启动
# 如果没有运行，手动启动：
ollama serve
```

### 第四步：运行程序

```bash
python main.py
```

**首次运行说明：**
- ✅ 确保 Ollama 正在运行（`ollama list` 检查）
- ✅ Whisper 模型会自动下载（约 150MB-1.5GB）
- ✅ 模型缓存到 `~/.cache/huggingface/`
- ✅ 启动完成后会出现字幕悬浮窗
- ✅ 翻译请求会发送到本地 Ollama（http://localhost:11434）

### 第五步：使用

1. ✅ 启动程序后，字幕窗口显示在屏幕底部
2. ✅ 播放任何视频或音频
3. ✅ 字幕会自动识别并翻译显示
4. ✅ 点击 ⚙️ 按钮可调整参数
5. ✅ 按 `Ctrl+C` 或点击 ❌ 退出程序

---

## ⚙️ 配置说明

### 核心配置（config.py）

#### Whisper 模型配置

```python
WHISPER_MODEL = "small"              # 模型：tiny, base, small, medium, large-v3
WHISPER_DEVICE = "cuda"              # 设备：cuda 或 cpu
WHISPER_COMPUTE_TYPE = "int8"        # 精度：float16, int8
WHISPER_LANGUAGE = None              # 语言：None（自动检测）或 "en", "zh", "ja"
```

#### Qwen 翻译配置（Ollama）

```python
OLLAMA_MODEL = "qwen3:8b"            # Ollama 模型名称（需提前 ollama pull）
OLLAMA_BASE_URL = "http://localhost:11434"  # Ollama 本地 API 地址
```

> 💡 **注意**：所有翻译请求都发送到本地 Ollama 服务，不会上传到云端。

#### 音频捕获配置

```python
MIN_AUDIO_DURATION = 0.4             # 最小音频时长（秒）
MAX_AUDIO_DURATION = 0.5             # 最大音频时长（秒）
SILENCE_DURATION = 0.6               # 静音持续时长（秒）
ENERGY_THRESHOLD_SPEECH = 0.01       # 语音能量阈值
```

#### 字幕窗口配置

```python
WINDOW_WIDTH = 1200                  # 窗口宽度（像素）
WINDOW_HEIGHT = 150                  # 窗口高度（像素）
WINDOW_X = 360                       # 窗口X坐标
WINDOW_Y = 750                       # 窗口Y坐标
FONT_SIZE = 22                       # 字体大小
MAX_SUBTITLE_LENGTH = 300            # 字幕最大字符数
```

### 模型选择指南

| 模型 | 显存占用 | 速度 | 准确度 | 适用场景 |
|------|---------|------|--------|----------|
| tiny | 1GB | ⚡⚡⚡⚡⚡ | ⭐⭐ | 极速模式，实时性优先 |
| base | 1GB | ⚡⚡⚡⚡ | ⭐⭐⭐ | **推荐**，平衡性能与质量 |
| small | 2GB | ⚡⚡⚡ | ⭐⭐⭐⭐ | 高质量，标点符号更准确 |
| medium | 5GB | ⚡⚡ | ⭐⭐⭐⭐⭐ | 专业场景 |
| large-v3 | 10GB | ⚡ | ⭐⭐⭐⭐⭐ | 最高质量 |

**推荐配置：** `small` + `int8` + `qwen3:8b`

---

## 📊 性能指标

### 测试环境

- **显卡**：RTX 3060 (12GB)
- **模型**：small + int8
- **翻译**：qwen3:8b

### 实测数据

| 指标 | 数值 | 说明 |
|-----|------|------|
| 端到端延迟 | 0.5-0.8秒 | 语音结束到字幕显示 |
| Whisper 识别 | 0.2-0.3秒 | 语音识别时间 |
| Qwen 翻译 | 0.3-0.5秒 | 单句翻译时间 |
| 显存占用 | 2-3GB | Whisper + Qwen |
| CPU 占用 | 10-20% | 音频捕获+处理 |
| 准确度 | 90-95% | 清晰音频环境 |

---

## 🎯 使用场景

### 适用场景 ✅

- 🎬 **观看外语视频** - YouTube、Netflix、本地视频
- 🎮 **游戏实况** - 实时翻译游戏语音
- 📺 **直播字幕** - Twitch、B站等直播平台
- 🎵 **音乐翻译** - 理解歌词含义
- 📞 **会议记录** - 实时翻译远程会议
- 🎓 **学习辅助** - 外语学习材料

### 不适用场景 ❌

- ❌ 多人同时说话（会识别混乱）
- ❌ 环境噪音很大（影响识别准确度）
- ❌ 说话速度极快（可能遗漏部分内容）
- ❌ 专业术语很多（需要调整翻译模型）

---

## 🔧 项目结构

```
realtime_subtitle/
├── main.py                    # 主程序入口
├── translator_queue.py        # Whisper + Qwen 句子队列翻译器
├── audio_capture.py           # 系统音频捕获（WASAPI Loopback）
├── subtitle_window.py         # 字幕悬浮窗 + GUI 参数面板
├── config.py                  # 配置文件（所有可调参数）
├── requirements.txt           # Python 依赖列表
├── README.md                  # 项目说明文档
└── 实时字幕软件开发方案.md   # 开发设计文档
```

### 核心模块说明

#### 1. audio_capture.py
- 使用 WASAPI Loopback 捕获系统音频
- 实时能量检测（VAD）
- 动态音频缓冲管理
- 语音停顿检测

#### 2. translator_queue.py
- Whisper 语音识别（faster-whisper）
- Qwen 上下文感知翻译
- 句子队列管理（去重、排序）
- 短句合并（≤20词）
- 稳定性检测（连续出现/静音触发）

#### 3. subtitle_window.py
- PyQt5 悬浮窗
- 字幕实时显示
- GUI 参数调节面板（能量阈值、时长、样式等）
- 窗口置顶显示

#### 4. main.py
- 线程管理（音频捕获、翻译、UI）
- 异步任务调度
- 资源清理

---

## ❓ 常见问题

### Q1: 提示 "Ollama 连接失败" 或 "翻译失败"？

**原因**：Ollama 服务未运行或模型未安装。

**解决方案：**
```bash
# 1. 检查 Ollama 是否运行
ollama list

# 2. 如果没有运行，启动 Ollama 服务
ollama serve

# 3. 确认模型已安装
ollama list | grep qwen3

# 4. 如果模型未安装，拉取模型
ollama pull qwen3:8b

# 5. 测试 Ollama API 是否正常
curl http://localhost:11434/api/tags

# 6. 测试模型是否能推理
ollama run qwen3:8b "translate to chinese: Hello"
```

**Windows 用户注意**：
- Ollama 安装后会自动启动，托盘图标显示在任务栏
- 如果看不到 Ollama 图标，从开始菜单启动 Ollama
- 确保防火墙允许 Ollama 使用 11434 端口

### Q2: 模型下载很慢或失败？

**解决方案：**
```bash
# 使用国内镜像
export HF_ENDPOINT=https://hf-mirror.com

# 或手动下载模型到：
~/.cache/huggingface/hub/
```

### Q3: GPU 内存不足？

**解决方案：**
1. 使用更小的 Whisper 模型：`WHISPER_MODEL = "tiny"` 或 `"base"`
2. 使用更小的 Qwen 模型：`ollama pull qwen3:1.7b`
3. 调整精度：`WHISPER_COMPUTE_TYPE = "int8"`

### Q4: 字幕不显示或卡顿？

**排查步骤：**
1. ✅ 检查终端是否有错误信息
2. ✅ 确认系统正在播放音频
3. ✅ 检查 Ollama 是否正常运行
4. ✅ 降低 `AUDIO_CONTEXT_WINDOW` 到 10-20
5. ✅ 增加 `SILENCE_DURATION` 到 0.8-1.0

### Q5: 翻译质量不理想？

**优化建议：**
1. 📝 使用更大的 Qwen 模型：
   ```bash
   ollama pull qwen3:14b    # 14B 参数模型（更好的理解能力）
   ollama pull qwen3:32b    # 32B 参数模型（最高质量）
   ```
   然后修改 `config.py`：`OLLAMA_MODEL = "qwen3:14b"`

2. 📝 确保音频清晰，减少背景噪音
3. 📝 调整翻译 Prompt（在 `translator_queue.py` 的 `_translate_single_sentence` 方法中）
4. 📝 增加 `LLM_TRANSLATION_HISTORY_SIZE` 提供更多上下文

### Q6: 句子被截断或重复？

**解决方案：**
1. 调整 `MIN_AUDIO_DURATION` 和 `MAX_AUDIO_DURATION`
2. 调整 `SILENCE_DURATION`（静音阈值）
3. 调整 `ENERGY_THRESHOLD_SPEECH`（能量阈值）
4. 使用 GUI 面板实时调整参数

### Q7: CPU/GPU 占用过高？

**优化方案：**
1. 减小 `AUDIO_CONTEXT_WINDOW`（默认 30）
2. 增加 `CHUNK_SIZE`（减少处理频率）
3. 使用 INT8 量化：`WHISPER_COMPUTE_TYPE = "int8"`
4. 限制 Ollama GPU 层数：`ollama run qwen3:8b --gpu-layers 20`

---

## 🛠️ 高级用法

### 自定义翻译 Prompt

编辑 `translator_queue.py` 中的 `_translate_single_sentence` 方法：

```python
prompt = f"""你是专业字幕翻译，请翻译：

上下文：{english_context}
当前句子：{sentence}

要求：
1. 理解情境，不要字面翻译
2. 保持语气和情感
3. 只输出中文翻译

翻译：
"""
```

### 添加其他语言支持

```python
# config.py
WHISPER_LANGUAGE = "ja"  # 日语
WHISPER_LANGUAGE = "ko"  # 韩语
WHISPER_LANGUAGE = "fr"  # 法语
# 支持所有 Whisper 支持的语言
```

### 使用其他 LLM 模型

本项目支持任何 Ollama 支持的模型，只需在本地拉取模型并修改配置：

```bash
# 拉取其他 Ollama 模型
ollama pull llama3:8b      # Meta Llama 3
ollama pull mistral:7b     # Mistral AI
ollama pull deepseek-coder # DeepSeek

# 修改 config.py
OLLAMA_MODEL = "llama3:8b"
```

**模型对比：**
| 模型 | 参数量 | 中文能力 | 推荐度 |
|------|--------|----------|--------|
| qwen3:8b | 8B | ⭐⭐⭐⭐⭐ | **推荐** |
| qwen3:14b | 14B | ⭐⭐⭐⭐⭐ | 高质量 |
| llama3:8b | 8B | ⭐⭐⭐ | 英文优先 |
| mistral:7b | 7B | ⭐⭐ | 速度快 |

---

## 🤝 贡献指南

欢迎贡献代码！请遵循以下步骤：

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 提交 Pull Request

### 代码规范

- 使用中文注释
- 遵循 PEP 8 规范
- 添加必要的类型提示
- 保持函数简洁（单一职责）

---

## 📝 开发路线图

### 已完成 ✅

- [x] Whisper 语音识别
- [x] Qwen 上下文翻译
- [x] 句子队列去重
- [x] 短句合并翻译
- [x] GUI 参数调节
- [x] 能量检测 VAD
- [x] 静音触发翻译

### 进行中 🚧

- [ ] 支持双语字幕显示
- [ ] 字幕历史记录
- [ ] 导出字幕文件（SRT）
- [ ] 多语言支持（日语、韩语等）

### 计划中 📋

- [ ] 字幕样式自定义
- [ ] 热键控制（开始/暂停/清屏）
- [ ] 多显示器支持
- [ ] 性能分析工具
- [ ] Docker 容器化部署
- [ ] Web 界面版本

---

## 📄 许可证

本项目采用 [Apache-2.0](LICENSE) 许可证。

---

## 🙏 致谢

### 核心技术

- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) - 高性能 Whisper 实现
- [Qwen](https://github.com/QwenLM/Qwen) - 阿里云通义千问大语言模型
- [Ollama](https://ollama.com/) - 本地 LLM 运行框架

### 开源库

- [PyQt5](https://www.riverbankcomputing.com/software/pyqt/) - GUI 框架
- [pyaudiowpatch](https://github.com/s0d3s/pyaudiowpatch) - Windows 音频捕获
- [librosa](https://librosa.org/) - 音频处理
- [CTranslate2](https://github.com/OpenNMT/CTranslate2) - 推理引擎

### 特别感谢

- OpenAI Whisper 团队
- Hugging Face 社区
- 所有贡献者和使用者

---

## 📮 联系方式

- **Issues**：[GitHub Issues](https://github.com/leik1000/realtime_subtitle/issues)
- **Discussions**：[GitHub Discussions](https://github.com/leik1000/realtime_subtitle/discussions)
- **Email**：409239349@qq.com

---

<div align="center">

**如果这个项目对你有帮助，请给个 ⭐ Star 支持一下！**

Made with ❤️ by [leik1000]

</div>
