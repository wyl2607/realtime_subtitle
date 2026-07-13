# 🎬 实时字幕翻译系统（德语直播 → 中文双语字幕）

完全本地运行的实时字幕系统：捕获电脑正在播放的任何声音（YouTube / ZDF 直播 /
Netflix / 语音聊天……），实时识别德语并翻译成中文，以置顶悬浮窗双语显示。
**全程离线推理，不向任何云端发送音频或文本。**

```
系统声音 ──WASAPI Loopback──▶ Faster-Whisper (large-v3-turbo, CUDA)
              │                       │ local agreement 增量识别
              │                       ▼
              │               德语句子流 ──▶ Ollama (qwen3.5:9b) 德→中翻译
              │                       │
              ▼                       ▼
        悬浮窗：德语先行上屏 + 草稿中文 + 正式双语句对
```

## ✨ 特点

- **德语先行显示**：识别一提交立即上屏（灰色部分表示还可能修正），中文随后跟上
- **草稿中文**：不用等句子说完——识别到半句就先给一版浅蓝斜体草稿翻译，正式翻译完成后自动替换
- **local agreement 增量识别**：词级前缀提交，根治流式识别的重复碎片问题
  （移植自 [whisper_streaming](https://github.com/ufal/whisper_streaming)，MIT）
- **术语表翻译**：德国政党/政客/政治术语按标准译名翻（AfD→德国选择党、Schuldenbremse→债务刹车），在 `config.py` 的 `GLOSSARY` 里可自行扩充
- **幻觉过滤**：自动拦截 Whisper 在静音/音乐段凭空生成的"Untertitelung des ZDF"类电视字幕惯用语
- **抗 GPU 抢占**：边玩游戏边用也不丢词——GPU 被抢时字幕只是滞后几秒，恢复后自动追上
- **窗口自适应**：悬浮窗边缘拖拽缩放，窗口越大自动显示越多历史句对；位置/大小/字号重启后记住
- **点词查词**：单击字幕里的任何德语词，弹窗显示原形/词性/中文释义/在本句中的含义（本地 LLM 查询）
- **鼠标穿透模式**：`Ctrl+Alt+M` 让字幕窗对鼠标完全隐形（点击穿过它落到下面的视频/游戏上），全屏看剧不挡操作
- **字幕存档**：每天一个文件存在 `transcripts/`（时间+原文+译文），方便回看和学德语
- **运行时切换**：`Ctrl+Alt+P` 暂停/继续（模型留在显存，秒恢复）、`Ctrl+Alt+L` 德语↔英语切换

## 🖥️ 系统要求

| | 推荐配置 | 最低配置（自动降级） |
|---|---|---|
| 系统 | Windows 10/11 64位 | 同左（**仅支持 Windows**，音频捕获用 WASAPI） |
| 显卡 | NVIDIA 8GB+ 显存（如 RTX 3060/4070） | 无 N 卡也能跑（CPU 模式，延迟明显变大） |
| 内存 | 16GB | 8GB |
| 硬盘 | 约 10GB（Python 依赖 + 识别模型 + 翻译模型） | 约 6GB |
| Python | 3.10 – 3.13（本机 3.13 实测） | 同左 |
| 其它 | [Ollama](https://ollama.com/)（本地跑翻译模型，安装脚本会引导安装） | 同左 |

## 🚀 安装（一键脚本）

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File install.ps1
# 国内网络加速：powershell -ExecutionPolicy Bypass -File install.ps1 -Mirror
```

脚本会自动：检查 Python → 检测显卡（没有 N 卡自动生成 CPU 降级配置）→
建 venv 装依赖 → 引导安装 Ollama 并拉取翻译模型 → 在桌面生成
**「德语直播实时字幕」快捷方式文件夹**（启动/停止/暂停 + 操作说明）。

之后每次使用：双击桌面的 `启动字幕.bat`，播放德语视频即可。
首次启动会自动下载 Whisper 识别模型（约 1.6GB），耐心等待。

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

## 🎛️ 使用

| 操作 | 方式 |
|---|---|
| 移动窗口 | 鼠标按住窗口任意位置拖动 |
| 缩放窗口 | 鼠标拖窗口**边缘/四角**（窗口越大显示的历史越多） |
| 查单词 | 鼠标**单击**字幕里的德语词（原地点一下=查词，按住拖=挪窗口） |
| 鼠标穿透 | `Ctrl+Alt+M` 开/关（穿透时字幕窗点不到，鼠标直达下层视频/游戏） |
| 暂停/继续 | 全局快捷键 `Ctrl+Alt+P`（游戏全屏时也有效） |
| 切换识别语言 | `Ctrl+Alt+L`（默认德语↔英语，`config.LANGUAGE_CYCLE` 可加） |
| 游戏模式降配 | `Ctrl+Alt+G` 开/关（识别频率减半+关草稿，GPU让给游戏，字幕慢1-2秒） |
| 回看本场字幕 | 点击悬浮窗 📜 按钮（可滚动） |
| 调参数 | 点击 ⚙️ 按钮（识别节奏/字号/句对条数等，实时生效） |
| 退出 | 点击 ❌ 按钮或双击 `停止字幕.bat` |

字幕颜色含义：**白色**=已确定德语；*灰色斜体*=还可能修正的德语尾部；
<i>浅蓝斜体</i>=草稿中文（先给你看个大概）；浅灰=正式中文翻译。

## ⚙️ 配置

所有参数集中在 [config.py](config.py)，每项都有注释。个人调参建议写在
`config_local.py`（不进 git，会覆盖 config.py 同名项）。常用项：

```python
WHISPER_MODEL = "large-v3-turbo"   # 显存不够改 "medium" / "small"
OLLAMA_MODEL = "qwen3.5:9b"        # 机器弱改 "qwen3.5:2b"
SOURCE_LANGUAGE = "de"             # 源语言
DRAFT_TRANSLATION = True           # 草稿中文（不想要就 False）
GLOSSARY = {...}                   # 德→中术语表，遇到翻错的专名往里加
```

## ❓ 常见问题

**启动报 `cublas64_12.dll` 找不到** — venv 里要装 `nvidia-cublas-cu12`
和 `nvidia-cudnn-cu12`（requirements.txt 已包含；重装 faster-whisper 后需重装）。

**字幕全是德语没有中文** — Ollama 没在运行或模型没拉。`ollama list` 检查，
`ollama pull qwen3.5:9b` 拉取。启动日志 `subtitle.log` 里会有明确提示。

**识别速度跟不上（日志里"GPU繁忙"频繁出现）** — ⚙️ 面板把"提交节奏"调大到
1.0 秒，或 `config_local.py` 里换小模型。

**没有 N 卡的电脑能用吗** — 能，install.ps1 会自动生成 CPU 降级配置
（small 模型 + qwen3.5:2b），但延迟会从 1-2 秒涨到 5-10 秒。

**抓不到声音 / 换了耳机没字幕** — 默认跟系统「默认播放设备」。
可在 ⚙️ 里填「设备名包含」（如 `FiiO`），或在 `config_local.py` 写
`LOOPBACK_DEVICE_NAME = "FiiO"`；约 5 秒内热切换。

**8GB 显存的卡（如 RTX 5060/4060）** — 默认配置能跑：Whisper（~2.4GB）+
qwen3.5:9b（~5.6GB）刚好贴着上限，Ollama 会自动把放不下的层挪到 CPU，
翻译可能慢 1-2 秒，草稿中文机制会先垫上。嫌慢可在 `config_local.py` 里
改 `OLLAMA_MODEL = "qwen3.5:4b"`。另注意 RTX 50 系不支持 int8（见上）。

## 🙏 致谢与许可

- 项目最初基于 [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle)（Apache-2.0），识别管线已完全重写
- 增量识别算法移植自 [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming)（MIT）
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) / [Qwen](https://github.com/QwenLM/Qwen) / [Ollama](https://ollama.com/) / [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

本项目采用 [Apache-2.0](LICENSE) 许可证。
