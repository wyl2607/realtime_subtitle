# 实时字幕翻译系统

**Windows 全离线实时字幕**：捕获系统声音 → 本地语音识别 → 本地翻译 → 置顶悬浮窗双语显示。

不向任何云端发送音频或文本。识别与翻译均在本机完成。

[English](README.md) · [Deutsch](README.de.md) · [目录结构](docs/STRUCTURE.md)

```text
系统声音 ──WASAPI Loopback──▶ Faster-Whisper (large-v3-turbo, CUDA/CPU)
              │                       │ local agreement 增量识别
              │                       ▼
              │               源语言句子 ──▶ Ollama（本地 LLM）翻译
              │                       │
              ▼                       ▼
        悬浮窗：源语言先行 + 草稿译文 + 正式双语句对
```

## 特点

- **源语言先行显示**：识别一提交立即上屏（灰色部分表示还可能修正），译文随后跟上
- **草稿译文**：半句即可出浅蓝斜体草稿，正式翻译完成后自动替换
- **local agreement 增量识别**：词级前缀提交，减少流式重复碎片（移植自 [whisper_streaming](https://github.com/ufal/whisper_streaming)，MIT）
- **术语表**：政党/政治等专名在 `config.py` 的 `GLOSSARY` 中维护
- **幻觉过滤**：拦截静音/音乐段的电视字幕惯用语幻觉
- **抗 GPU 抢占**：游戏占 GPU 时字幕滞后而非永久丢词
- **窗口自适应**：边缘拖拽缩放，位置/大小/字号可持久化
- **点词查词**：单击德语词，本地 LLM 给原形/词性/释义
- **鼠标穿透**：`Ctrl+Alt+M` 点击穿过字幕落到视频/游戏上
- **字幕存档**：按天写入 `transcripts/`
- **热键**：暂停、切语言、性能模式（见「使用」）

## 系统要求

| | 推荐 | 最低 |
|---|---|---|
| 系统 | Windows 10/11 64 位 | 同左（**仅 Windows**，WASAPI） |
| 显卡 | NVIDIA 8GB+ 显存 | 无独显可 CPU（延迟更大） |
| 内存 | 16GB | 8GB |
| 硬盘 | 约 10GB | 约 6GB |
| Python | 3.10–3.13 | 同左 |
| 其它 | [Ollama](https://ollama.com/) | 同左 |

## 安装

请克隆到**纯英文路径**（如 `C:\realtime_subtitle`）。桌面快捷方式会内嵌绝对路径，非 ASCII 用户目录会弄坏生成的 `.bat`。

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# 国内镜像：powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror

# 根目录兼容入口仍然可用：
# powershell -ExecutionPolicy Bypass -File install.ps1
```

安装脚本会检查路径与 Python、检测显存（或 CPU 降级）、创建 venv、装依赖、引导 Ollama，并在桌面生成「德语直播实时字幕」快捷方式文件夹。

若未安装 Ollama，脚本会询问是否用 **`winget install --id Ollama.Ollama -e`** 自动安装（装完在常见路径查找 exe，不依赖当前 PATH 刷新）。拒绝则打开官网下载页。

首次启动会下载 Whisper 模型（约 1.6GB）。

**交给 AI 装**：克隆本仓库并按 [CLAUDE.md](CLAUDE.md) 的硬件分档与避坑清单操作。

## 更新与卸载

| 动作 | 方式 |
|---|---|
| 更新 | 桌面「更新字幕.bat」或 `scripts\windows\update_subtitles.ps1` |
| 卸载 / 腾空间 | `scripts\windows\uninstall.ps1`（逐项询问，默认保留） |
| 只清缓存 | `scripts\windows\uninstall.ps1 -CleanCache` |

个人配置不进 git：`config_local.py`、窗口状态、`transcripts/`。

<details>
<summary>手动安装（不用脚本）</summary>

```powershell
python -m venv venv
venv\Scripts\pip install -r requirements.txt
# 安装 Ollama: https://ollama.com/download
ollama pull qwen3.5:9b
venv\Scripts\python -u main.py
```
</details>

## 使用

| 操作 | 方式 |
|---|---|
| 移动窗口 | 拖动窗口任意位置 |
| 缩放 | 拖边缘/四角 |
| 查词 | 单击德语词 |
| 鼠标穿透 | `Ctrl+Alt+M` |
| 暂停/继续 | `Ctrl+Alt+P` |
| 切换识别语言 | `Ctrl+Alt+L`（默认德语↔英语） |
| 性能模式 | `Ctrl+Alt+G` |
| 回看本场 | 📜 |
| 调参 | ⚙️ |
| 退出 | ❌ 或停止快捷方式 |

**颜色**：**白**=已确定源语言；*灰斜体*=可能修正的尾部；*浅蓝斜体*=草稿译文；浅灰=正式译文。

## 配置

默认在 [config.py](config.py)。个人覆盖写 **`config_local.py`**（gitignore）：

```python
WHISPER_MODEL = "large-v3-turbo"   # 显存不够改 medium / small
OLLAMA_MODEL = "qwen3.5:9b"        # 弱机可 qwen3.5:2b
SOURCE_LANGUAGE = "de"
DRAFT_TRANSLATION = True
GLOSSARY = {...}
```

显存分档见 [CLAUDE.md](CLAUDE.md)。

## 目录结构

| 路径 | 职责 |
|---|---|
| 根目录 `*.py` | 运行时核心（采集 / 识别 / 翻译 / UI） |
| `scripts/windows/` | 安装、启动、停止、暂停、更新、卸载 |
| `tests/` | 单元测试 + 独立 GUI 脚本套件 |
| `docs/` | 设计文档、中文笔记 |

详见 [docs/STRUCTURE.md](docs/STRUCTURE.md)。三语 README 同步规范见 [docs/README-i18n.md](docs/README-i18n.md)。

## 测试

```powershell
venv\Scripts\pip install -r requirements-dev.txt
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
# GUI 独立套件（pytest 不收集）：
# venv\Scripts\python tests\test_hittest.py
```

## 常见问题

| 现象 | 处理 |
|---|---|
| 缺少 `cublas64_12.dll` | 按 `requirements.txt` 重装 nvidia-cublas/cudnn |
| 只有原文没有译文 | 启动 Ollama；`ollama pull` 程序实际使用的 `config.OLLAMA_MODEL` |
| 频繁 “GPU 繁忙” | 设置里加大提交节奏，或换小 Whisper 模型 |
| 换耳机后无字幕 | 设置「设备名包含」或 `LOOPBACK_DEVICE_NAME` |

日志：`subtitle.log` / `subtitle.err.log`，以及 `logs/` 轮转。

## 致谢与许可

- 最初基于 [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle)（Apache-2.0），识别管线已重写
- 增量识别思路来自 [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming)（MIT）
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) · [Qwen](https://github.com/QwenLM/Qwen) · [Ollama](https://ollama.com/) · [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

本项目采用 [Apache-2.0](LICENSE) 许可证。
