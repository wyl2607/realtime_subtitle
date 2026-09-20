# 实时字幕翻译系统

**Windows 全离线实时字幕**：捕获系统声音 → 本地语音识别 → 本地翻译 → 置顶悬浮窗双语显示。

识别与翻译均在本机完成，不向任何云端发送音频或文本。当前版本 **v2.5.0**。

> **给朋友用（三步）**
> 1. 克隆到**纯英文路径**（推荐 `C:\realtime_subtitle`，中文用户名目录会把启动脚本弄坏）
> 2. 运行安装：`powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1`（国内网络加 `-Mirror`）
> 3. 桌面文件夹「德语直播实时字幕」里双击「启动字幕.bat」，再打开任意德/英/中文视频

> **这一条是代码强制的，不只是文档承诺**：启动时会解析 `OLLAMA_BASE_URL`，只要解析出的
> 地址不全是环回地址就拒绝启动——否则 `config_local.py` 里改错一个字，就能把你的转录
> 静默送出本机，而字幕照常出中文、屏幕上和日志里都看不出异常。确实要用另一台机器上的
> Ollama：在 `config_local.py` 里写 `ALLOW_REMOTE_OLLAMA = True`。

> **唯一的例外，而且只有你点它才会发生**：弹窗里的「🌐 问更强的AI」按钮会把最近几分钟
> 识别出的原文（≤300 字）拼成一句提问，用系统浏览器打开网页版 AI（默认 grok.com，
> `config.AI_ANALYSIS_WEB_URL_TEMPLATE` 可改）。不点就不发，而且点了之后还会先弹一个框，
> 把**要发出去的内容原样亮给你看**（发到哪个域名、共多少字、前 300 字），确认了才发；
> 嫌烦可以 `AI_WEB_CONFIRM = False` 关掉。把那个模板设成空串即可让按钮彻底消失。
> 注意本程序抓的是**系统全部声音**，可能包含语音通话内容。

[English](README.en.md) · [Deutsch](README.de.md) · [目录结构](docs/STRUCTURE.md) · [Windows 操作清单](docs/zh/WINDOWS-RUNBOOK.md)

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
- **中德双向**：`Ctrl+Alt+L` 循环的是**语言对**，放中文视频就出德语字幕。
  自动切换（`AUTO_DETECT_LANGUAGE`，**默认关**）连续 3 次检测到同一新语言才切；
  切换真正发生前有约 12 秒的垃圾字幕——那段音频还在用旧语言的参数解码。
  嫌乱切就调大 `LANGUAGE_SWITCH_STREAK`，嫌垃圾窗口长就调小 `LANGUAGE_DETECT_INTERVAL`。
- **鼠标穿透**：`Ctrl+Alt+M` 点击穿过字幕落到视频/游戏上
- **🎞 影院字幕条**：叠在全屏视频底部的透明字幕（`Ctrl+Alt+C`），永远鼠标穿透，不抢播放器点击
- **📺 电视全屏**：在另一块屏用大字不透明窗显示（Esc 退出）
- **🤖 AI 分析**：本地总结最近几分钟；🌐 问更强的 AI 会先弹出确认框
- **字幕存档**：按天写入 `transcripts/`，默认永久保留。是明文，且本程序抓的是**系统全部声音**——想自动清理就在 `config_local.py` 里设 `TRANSCRIPT_KEEP_DAYS = 30`，完全不想记录就 `SAVE_TRANSCRIPT = False`
- **下载并制作双语字幕**：桌面「YouTube下载加字幕.bat」或 `scripts\windows\download_subtitle.ps1` 会下载最高 4K 视频、提取语音、生成 SRT，并用本地 Ollama 翻译；中文源语言输出“中文 + 德语”，德语/英语/其他源语言输出“原文 + 中文”，同时生成中文学习笔记
- **热键**：暂停、切语言、性能模式、影院字幕条（见「使用」）

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

安装脚本会检查路径与 Python、检测显存（或 CPU 降级）、创建 venv、装依赖、引导 Ollama，并在桌面生成「德语直播实时字幕」快捷方式文件夹（含 YouTube 下载加字幕）。

若未安装 Ollama，脚本会询问是否用 **`winget install --id Ollama.Ollama -e`** 自动安装（装完在常见路径查找 exe，不依赖当前 PATH 刷新）。拒绝则打开官网下载页。

首次启动会下载 Whisper 模型（约 1.6GB）。

**交给 AI 装**：克隆本仓库并按 [CLAUDE.md](CLAUDE.md) 的硬件分档与避坑清单操作。

## 更新与卸载

| 动作 | 方式 |
|---|---|
| 启动（每次先更新） | 桌面「启动字幕.bat」——先拉最新版再启动；更新失败（断网/冲突）也照常启动 |
| 只更新不启动 | `scripts\windows\update_subtitles.ps1`（桌面上不再单独放这个入口） |
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
| 切换语言对 | `Ctrl+Alt+L` 循环（德→中 / 中→德 / 英→中），或在 ⚙️ 面板直接点某一对；改 `config.LANGUAGE_PAIRS` 可增删。同一个源语言可以配多条（如中→英、中→德），面板会各出一个按钮，选择会被记住 |
| 性能模式 | `Ctrl+Alt+G` |
| 影院字幕条 | `Ctrl+Alt+C` 或 🎞（视频已经全屏时必须用热键） |
| 电视全屏 | 📺 |
| AI 分析 | 🤖 |
| 回看本场 | 📜 |
| 调参 | ⚙️（节奏、字号、影院字幕条样式当场生效） |
| 退出 | ❌ 或停止快捷方式 |

### 下载视频并制作双语字幕

双击桌面上的「YouTube下载加字幕.bat」即可：如果剪贴板里有视频链接，程序会自动读取（有多个链接时会让你选一个，不会替你随机决定）；没有有效链接时会出现输入提示。**也可以直接把本地视频/音频文件拖进那个输入框**——本地文件不经过任何下载器，走的是同一条识别/翻译/导出链路，身份按文件内容算（改名不算新视频，改了内容才算）。合集/播放列表、图文帖、没有音轨的链接会在**开始识别之前**被拦下并说明原因。随后依次问两个问题——**字幕形式**（1 双语，默认；2 单语只要译文）和**译文语言**（1 英语，中文视频的默认；2 德语），直接回车就是默认值。程序默认下载最高 2160p（4K）画面，使用本地 Faster-Whisper 识别，并逐条调用本地 Ollama 翻译，避免长上下文造成字幕重复。

输出在 `downloads\<视频ID>\`，文件名带语言标签，**换目标语言不会覆盖上一份**：

- `<视频ID>.<源>-<目标>.bilingual.srt`：原文 + 译文，可直接导入播放器
- `<视频ID>.<源>-<目标>.target.srt`：只有译文（单语）
- `<视频ID>.<源>.source.srt`：只有原文，便于单独复习
- `<视频ID>.<源>-<目标>.learning.md`：中文内容概述、对话脉络、重点表达和学习方法

三种字幕每次都会一起导出，所以双语和单语之间改主意不用重跑。默认译文语言：**中文视频 → 英语**，其他语言 → 中文；显式指定的目标语言不会被自动识别结果覆盖。同一个视频换目标语言时不会重新识别，已经翻好的那一份也保留着，切回去是免费的。

> 实时字幕的中文→德语（`LANGUAGE_PAIRS`）没有跟着改，那是德语学习用途。

**分享文案可以整段粘贴**：程序会从里面抠出链接（中文标点不会被当成链接的一部分），多个链接时让你选。跑完会打印一行阶段耗时，例如 `阶段耗时：抽音频 3.1s，识别 412.0s，翻译 1180.4s；合计 1595.5s（视频 31.2 分钟，实时倍率 0.85x）`——复用缓存的阶段会标注出来，不会把"没跑"显示成"很快"。

**下载失败怎么办**：需要登录、私密内容、地区限制、限流、链接失效这几类都会给中文说明，并提示同一条出路——先用别的方式把视频存到本地，再把文件拖进输入框（本地导入不联网、不需要登录）。确实需要登录态时，在 `config_local.py` 里显式配置：

```python
OFFLINE_COOKIES_FILE = r"D:\私密\cookies.txt"    # Netscape 格式，二选一
OFFLINE_COOKIES_FROM_BROWSER = "chrome"           # 或 ("chrome", "Default")
```

默认两者都不设，程序不会自己去翻浏览器。凭据只交给下载器，不会写进日志、任务元数据或断点文件。

> 小红书：`yt-dlp 2026.08.19` 认 `www.xiaohongshu.com/explore/<id>` 和 `/discovery/item/<id>`；**短链 `xhslink.com` 不在它的匹配规则里**，能否走通取决于跳转跟随，本项目没有用真实链接验证过。

命令行也可以使用：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\windows\download_subtitle.ps1 -Url "视频地址"
# 本地文件（-Url 也收路径，或用 -Path 别名）：
powershell -ExecutionPolicy Bypass -File scripts\windows\download_subtitle.ps1 -Path "D:\视频\clip.mp4"
# 直接指定，不走交互提问：
powershell -ExecutionPolicy Bypass -File scripts\windows\download_subtitle.ps1 -Url "视频地址" -TargetLanguage de -SubtitleMode target
# 不生成学习笔记：加 -NoSummary
```

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
| `main.py` | 入口（`python -u main.py`） |
| `realtime_subtitle/` | 包：`capture/` `asr/` `translate/` `ui/` `offline.py` `app.py` |
| `scripts/windows/` | 安装、启动、停止、暂停、更新、卸载、下载加字幕 |
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
| 下载或加字幕失败 | 确认已安装 `ffmpeg`，首次运行先启动 Ollama；重新安装依赖以补齐 `yt-dlp` |

日志：`subtitle.log` / `subtitle.err.log`，以及 `logs/` 轮转。

## 致谢与许可

- 最初基于 [leik1000/realtime_subtitle](https://github.com/leik1000/realtime_subtitle)（Apache-2.0），识别管线已重写
- 增量识别思路来自 [ufal/whisper_streaming](https://github.com/ufal/whisper_streaming)（MIT）
- [Faster-Whisper](https://github.com/SYSTRAN/faster-whisper) · [Qwen](https://github.com/QwenLM/Qwen) · [Ollama](https://ollama.com/) · [pyaudiowpatch](https://github.com/s0d3s/PyAudioWPatch)

本项目采用 [Apache-2.0](LICENSE) 许可证。
