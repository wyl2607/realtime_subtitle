# macOS 使用说明（实时字幕 / 下载加字幕）

本文件是 **macOS 这一侧的单一真相源**：安装脚本会把它原样复制成桌面
「德语实时字幕」文件夹里的《操作说明.md》，README 只链接到这里。
Windows 那侧的对应文件是 `docs/zh/user-guide-template.txt`——
两边内容刻意保持对称，改一边记得看另一边是不是也该改。

## 系统要求

| 项目 | 要求 | 说明 |
|---|---|---|
| 系统 | **macOS 14 或更新** | 音频采集走 ScreenCaptureKit（系统音频的官方接口），更低的版本拿不到音频流 |
| 芯片 | Apple Silicon（M 系列）最舒服 | Intel 也能跑，只是识别明显慢（没有 NPU/Metal 加速给 ctranslate2 用） |
| 磁盘 | 预留 **约 10GB** | venv 约 2-3GB + 语音识别模型 1-3GB + 翻译模型 2-6GB |
| 装好的依赖 | ffmpeg、Ollama、uv、Python 3.12 | `install.sh` 会自动装，不用自己动手 |
| 网络 | 首次启动要下载模型 | 之后完全本地跑，字幕文字不出本机 |

命令行里跑安装（**不要**用 `brew install` 装这些，脚本自己管）：

```bash
bash scripts/macos/install.sh            # 常规
bash scripts/macos/install.sh --mirror   # 中国大陆网络走清华 PyPI 镜像
```

脚本是幂等的：中断了、装到一半改了主意，直接重跑即可。
已经存在的 `config_local.py` 一律**保留**（想让它重新生成就先删掉它）。

## ☠️ 首次启动前必须做的一件事：给音频权限

macOS 采集系统声音需要用户授权，**脚本给不了**（那是系统 TCC 的事）。
不授权的症状是：悬浮窗正常起来、日志也在写，但一个字都不出。

1. 打开「系统设置 → 隐私与安全性 → 屏幕与系统音频录制」
2. 打开你启动它的那个程序的开关——一般是「终端 / Terminal」
   （在 VS Code 里启动就给 VS Code，用别的启动器就给那个启动器）
3. 列表里没有它？**先双击一次「启动字幕.command」**，系统弹框点过之后它才会出现
4. 授权后**重启字幕**（停一次再启一次），再放一段德语音视频试

## 五个启动器（桌面「德语实时字幕」文件夹里）

| 启动器 | 干什么 |
|---|---|
| `启动字幕.command` | ★ 平时只需要这一个：先拉一次最新代码 → 确认 Ollama 服务和翻译模型 → 后台启动字幕程序 |
| `YouTube下载加字幕.command` | 给视频"事后"配字幕：读剪贴板或粘贴地址，也能把本地视频/音频直接拖进输入框 |
| `停止字幕.command` | 优雅退出字幕程序（一般 1-2 秒；5 秒没退完只强制结束那一个进程） |
| `暂停继续字幕.command` | 暂停/继续识别翻译（悬浮窗还在，但不再做识别） |
| `卸载字幕.command` | 逐项问你要不要删：venv／识别模型／翻译模型／桌面启动器，默认全"不删" |

行为约定（和 Windows 侧一致）：

- 启动成功 → 终端窗口约 3 秒后**自动关闭**（Terminal.app；iTerm/VS Code 内置
  终端下窗口会留着，⌘W 关掉就行）。出错 → 窗口保留，方便看报错。
- `暂停继续字幕.command` 双击一次暂停、再一次继续；也可以直接按 `Ctrl+Alt+P`。
- `停止字幕.command` 还会把 Ollama 里常驻的翻译模型卸载掉，内存立刻回来。
  它只卸本项目用到的模型，别人在跑的大模型不受影响。
- `卸载字幕.command` 想只清垃圾不卸载，就单独跑
  `bash scripts/macos/uninstall.sh --clean-cache`（删下载中断的残文件）。

## 不用启动器时，命令行等价写法

```bash
# 启动 / 只启动（不更新） / 停止 / 暂停切换
bash scripts/macos/start.sh              # = 更新 + 启动
bash scripts/macos/stop.sh
bash scripts/macos/pause.sh

# 同名的长名也在（跟 Windows 的 .ps1 一一对应，改代码时对照着看方便）
bash scripts/macos/start_and_update_subtitles.sh
bash scripts/macos/start_subtitles.sh    # 只启动，不更新
bash scripts/macos/stop_subtitles.sh
bash scripts/macos/pause_subtitles.sh

# 只更新，不启动（改代码时用这个）
bash scripts/macos/update_subtitles.sh
bash scripts/macos/update_subtitles.sh --prune   # 顺手卸掉清单外的包

# 下载加字幕
bash scripts/macos/download_subtitle.sh
bash scripts/macos/download_subtitle.sh "视频地址"
bash scripts/macos/download_subtitle.sh "/本地路径/video.mp4"
bash scripts/macos/download_subtitle.sh --target-language de "视频地址"
```

一次调用就能在**任意目录**跑，因为脚本自己按自己的位置找仓库根
（`scripts/macos/../..`），不需要 `cd` 到仓库里。

## 日常怎么用

1. 双击「启动字幕.command」，看到 `已启动 (PID xxx)` 后窗口自动关闭。
   悬浮窗几秒内出现，识别/翻译模型在后台继续加载十几秒（有进度提示）。
2. 播放德语（也支持英语/中文源语）音视频，悬浮窗底部先出灰色原文，
   说完整句后译文成对显示在上方。
3. 快捷键（游戏里也能按）：
   - `Ctrl+Alt+P` 暂停/继续
   - `Ctrl+Alt+L` 切换语言对
   - `Ctrl+Alt+M` 鼠标穿透开/关
   - `Ctrl+Alt+G` 一键切「⚡性能」模式（把 CPU/内存让给游戏，字幕慢 1-2 秒）
   - `Ctrl+Alt+C` 影院字幕条开/关（全屏视频时用）
4. 鼠标单击字幕里的单词 → 弹小窗查词（德语学习神器）。
5. 字幕自动存档在仓库的 `transcripts/`，每天一个文件。

### 下载加字幕的交互

双击后默认读剪贴板；剪贴板里没有有效链接才问你地址。粘一整段分享文案也行
（里面多个链接会让你选一个）。然后问两个问题，**直接回车就是默认值**：

- 字幕形式：`1` 双语（默认）/ `2` 单语（只要译文）
- 译文语言：`1` 英语（中文视频默认）/ `2` 德语

产物在 `downloads/<视频ID>/`：

- `<视频ID>.<源>-<目标>.bilingual.srt` — 原文 + 译文
- `<视频ID>.<源>-<目标>.target.srt` — 只有译文
- `<视频ID>.<源>.source.srt` — 只有原文

三种每次一起生成，换语言不会覆盖上一份；另有一份中文学习笔记。

## 常见问题（macOS 特有）

- **悬浮窗起来了但没字幕**：九成是上面那条音频权限没给；再看
  `subtitle.err.log` 尾部确认。
- **双击 `.command` 说"无法验证开发者"/被拒绝**：从浏览器下载的 zip 解出来的
  文件带隔离标记。跑一次
  `xattr -dr com.apple.quarantine "$HOME/Desktop/德语实时字幕" "$PWD"`，或重新
  clone 一份仓库再跑 `install.sh`。
- **只有原文没有译文**：`ollama list` 看翻译模型在不在，`ollama ps` 看它是不是
  100% 在 GPU 上；日志里搜"翻译超时"。
- **启动时喊"等了一分多钟 Ollama 服务还没就绪"**：手动 `ollama serve` 看报错；
  Ollama 自动更新时端口会短暂消失，等更新完再点一次启动即可。
- **第一句特别慢**：翻译模型第一次要从磁盘装进内存，开机后第一次可能要半分钟；
  这段时间原文照常显示。
- **改完 `config_local.py` 没生效**：改完要重启字幕（⌘面板里的项除外）。
- **桌面启动器指向的仓库被移动/改名了**：跑一次 `bash scripts/macos/install.sh`
  重新生成（启动器旁边那个 `.repo-root` 记着旧路径）。
- **想让字幕更快/更准**：改 `config_local.py` 里的 `WHISPER_MODEL`、
  `OLLAMA_MODEL`（**别**改 `config.py`，那是仓库文件，更新会冲突）。
  macOS 上 `WHISPER_DEVICE` 只能是 `cpu`、`WHISPER_COMPUTE_TYPE` 只能是 `int8`
  （ctranslate2 没有 Metal 后端）。

## 给 AI 助手的备注

- 机器相关的配置一律写 `config_local.py`，**永远不要**为了让某台机器跑通去改
  `config.py`。
- macOS 上可以放心写进 `config_local.py` 的键：
  `WHISPER_MODEL` / `OLLAMA_MODEL` / `GAME_MODE_OLLAMA_MODEL` /
  `CHUNK_SUBMIT_SECONDS` / `WHISPER_BEAM_SIZE` / `LANGUAGE_PAIRS` /
  `AUTO_DETECT_LANGUAGE` / `LOOPBACK_DEVICE_NAME`。
- **绝对不要**为了"让它跑起来"顺手改 `OLLAMA_BASE_URL`（改错就把全部系统声音
  的转录送出本机）、`ALLOW_REMOTE_OLLAMA`（它的唯一作用是关掉本地校验）、
  `AI_ANALYSIS_WEB_URL_TEMPLATE`、`OFFLINE_COOKIES_FILE`。这几个要先跟用户说清楚。
- 技术细节见仓库根目录的 `CLAUDE.md`，里面的避坑清单（尤其是"别按名字杀进程"
  那几条）对 macOS 脚本同样成立。