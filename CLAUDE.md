# CLAUDE.md — 给接手这台电脑的 AI 助手（Claude Code 等）

这是一个**完全本地运行**的实时字幕系统：捕获 Windows 正在播放的声音（直播/视频/
语音聊天），Faster-Whisper 实时识别德语，Ollama 本地大模型翻译成中文，PyQt5
置顶悬浮窗双语显示。不向任何云端发送音频或文本。**仅支持 Windows**（音频捕获
用 WASAPI Loopback）。

如果用户让你"帮我装上/配好这个字幕"，你的任务是：**按本文件把系统装好、
按这台电脑的硬件把模型档位配对、验证能跑，并把坑绕开**。本文件就是为此写的，
优先级高于你自己的猜测。

## 1. 安装流程

### 依赖清单（先逐项检查，缺什么装什么）

| 依赖 | 干什么用 | 怎么装 | 怎么验证 |
|---|---|---|---|
| Python 3.10–3.13 | 运行主程序 | `winget install --id Python.Python.3.13 -e` | `py -3.13 --version` |
| Git | clone 仓库 + 一键更新 | `winget install --id Git.Git -e` | `git --version` |
| Ollama | 本地翻译模型运行时 | `winget install --id Ollama.Ollama -e` | `ollama --version` |
| NVIDIA 驱动 | CUDA 12 运行库要求驱动较新 | GeForce 官网/GeForce Experience 更新 | `nvidia-smi` 右上角 CUDA Version **≥ 12.0** |
| Python 依赖包 | requirements.txt | **install.ps1 自动装进 venv**，不用手动 | 装完脚本无红字 |
| Whisper 识别模型 | 语音识别（1-3GB） | **首次启动自动从 HuggingFace 下载** | 首启等几分钟即可 |
| 翻译模型 | 德→中翻译 | **install.ps1 自动 `ollama pull`** | `ollama list` |

注意事项（都踩过或可预见）：

- winget 装完 Python/Git 后**要开新终端**才认识新命令（PATH 刷新）；`python`
  命令可能被 Microsoft Store 别名劫持，用 `py` 验证——install.ps1 两种都会找。
- **磁盘空间预留 ~12GB**：venv 约 4GB + Whisper 模型 1-3GB + 翻译模型 2-6GB。
- 驱动太旧的症状：程序启动时 cublas/cudnn 报错或 CUDA error。先
  `nvidia-smi` 看 CUDA Version，<12.0 就先升驱动，别急着折腾 Python 层。
- **中国大陆网络**：pip 走镜像加 `-Mirror` 参数；首次启动下载 Whisper 模型
  连不上 HuggingFace 的话，先设 `HF_ENDPOINT` 再启动：
  `[Environment]::SetEnvironmentVariable("HF_ENDPOINT","https://hf-mirror.com","User")`
  （对 huggingface_hub 生效；设完重开终端/重启程序）。Ollama 拉模型一般直连可用。
- 杀毒软件可能拦 pyaudiowpatch 的音频捕获或误报 venv 里的 exe——现象是
  装完启动无声音/进程被删，加白名单即可。

### 一键安装

前置三件套齐了之后（幂等，中断重跑即可）：

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File install.ps1
# 中国大陆网络：加 -Mirror 参数走清华 PyPI 镜像
```

install.ps1 会：找 Python → nvidia-smi 检测显卡 → 建 venv 装依赖 → 按显存
生成 `config_local.py` 降级配置 → 启动 Ollama 并拉取配置对应的翻译模型 →
在桌面生成「德语直播实时字幕」文件夹（启动/停止/暂停/更新四个 bat + 说明）。

首次点"启动字幕.bat"还会自动下载 Whisper 模型（1-3GB），属于正常现象。

## 2. 硬件适配与模型选择（你最重要的工作）

**机制**：所有机器相关配置写 `config_local.py`（在 .gitignore 里，会覆盖
config.py 同名项）。**永远不要为了适配这台机器去改 config.py**——那是仓库
文件，改了会让以后 `git pull` 更新冲突。

install.ps1 按显存自动生成的默认档位：

| 硬件 | WHISPER_MODEL / COMPUTE | OLLAMA_MODEL | 说明 |
|---|---|---|---|
| 无 NVIDIA 卡（含 AMD/Intel） | small / cpu / int8 | qwen3.5:2b | 延迟约 5-10 秒；Ollama 自己或许还能用上非 N 卡的 GPU |
| 显存 ~4GB | small / cuda / int8 | qwen3.5:2b | |
| 显存 ~6GB | large-v3-turbo / int8 | qwen3.5:2b | 余量 >2.5GB 可升 4b |
| 显存 ~8GB（主流游戏卡） | large-v3-turbo / float16 | qwen3.5:4b | 9b 会贴上限、层挪 CPU 变慢 |
| 显存 ≥10GB | large-v3-turbo / float16 | qwen3.5:9b | 参考机：RTX 4070 12GB |

微调原则（用户抱怨时按这个调）：

- **总账要算三方**：Whisper + 翻译模型 + 桌面/浏览器本身（约 1-1.5GB）都在同
  一块卡上。看视频场景浏览器硬解还要再占一点。爆显存的表现不是崩溃，而是
  Ollama 把层挪到 CPU、翻译从 <1 秒变 3-5 秒。
- **实测方法**：跑起来后 `nvidia-smi` 看真实占用；`ollama ps` 看翻译模型是否
  100% GPU（出现 "xx%/xx% CPU/GPU" 就是放不下了，降一档）。
- **识别质量优先序**：large-v3-turbo（int8 也行）≫ medium > small。德语场景
  turbo 的准确率提升非常明显，显存够 ~2GB 就尽量用它。
- **RTX 50 系（Blackwell）**：ctranslate2 <4.6.2 的 int8 路径会直接
  CUBLAS_STATUS_NOT_SUPPORTED 崩溃。requirements.txt 已锁 >=4.6.2（会自动
  禁用 int8 回退），别手动降版本。
- **模型名会过时**：qwen 系列迭代很快，如果 `ollama pull` 报模型不存在，去
  https://ollama.com/library 找当前的同级替代（选 instruct 类、体积相近的），
  写进 config_local.py 即可，代码不用改。
- **识别跟不上**（日志频繁"GPU繁忙"/字幕滞后）：config_local.py 里
  `CHUNK_SUBMIT_SECONDS = 1.0`（默认 0.5）最有效，其次 `WHISPER_BEAM_SIZE = 1`。
- 改完 config_local.py 需要重启字幕生效（⚙️ 面板里的项除外）。

## 3. 更新机制

- **拿更新**：双击桌面"更新字幕.bat"（= `update_subtitles.ps1`）：
  `git pull --ff-only` + requirements 变了才重装依赖 + 提示是否需要重跑
  install.ps1。config_local.py / window_state.json / transcripts/ 都不在 git
  里，更新永远不会碰它们。
- **更新失败**基本都是有人直接改了仓库文件。处理：`git stash` 后重试；根治：
  把改动挪进 config_local.py 或让上游合并。
### 出了问题怎么反馈（issue / PR）

先自查：`subtitle.err.log`（崩溃看这）和 `subtitle.log` 尾部 + 本文件第 4 节
避坑清单 + README 常见问题。确认是项目 bug（而不是本机环境/显存不够）后：

- **开 issue（首选）**：按 `.github/ISSUE_TEMPLATE/bug_report.md` 的格式把
  版本 commit、系统、显卡、config_local、两个日志尾部收集齐，然后
  `gh issue create -R wyl2607/realtime_subtitle --title "[bug] ..." --body-file 报告.md`
  （需要 GitHub 账号且 `gh auth login` 过；网页上开也一样）。
- **没有 GitHub 账号**：把同样内容整理成一段文字，交给用户让他直接发给作者
  （微信/QQ），内容齐了作者那边的 AI 一样能修。
- **自己已经修好了代码**想回馈：跑完 `venv\Scripts\python -m pytest`（全绿，
  条数以实际输出为准）再发 PR——`gh repo fork wyl2607/realtime_subtitle --remote=true`，
  开分支提交，push 到自己的 fork，`gh pr create`。改动尽量小、提交信息写清
  根因。不要 push 到 upstream（leik1000 是最初的模板仓库，早已分道扬镳）。
- 改代码前先双击"更新字幕.bat"拉到最新，避免在旧版上修已经修过的东西。

## 4. ☠️ 避坑清单（每一条都是真实踩过的）

安装/环境：

1. **PyQt5 必须在 torch 之后导入**。main.py 的 import 顺序是生死攸关的：先
   PyQt5 后 torch = `WinError 1114 (c10.dll)` 100% 复现。不要"整理 imports"。
2. **cublas64_12.dll 只认 PATH**。Windows 上 ctranslate2 按名字 LoadLibraryA
   加载，`os.add_dll_directory()` 无效。translator_queue.py 顶部把
   `nvidia.cublas` pip 包的 bin 目录拼进 `os.environ["PATH"]`——这段代码
   看着像 hack，删了程序就起不来。重装 faster-whisper/ctranslate2 后若报
   找不到 dll，重装 `nvidia-cublas-cu12 nvidia-cudnn-cu12`。
3. **torch 装 CPU 版就够**。项目识别走 ctranslate2 自带的 CUDA，torch 只是
   被 ctranslate2 无条件 import。别"好心"换装几个 GB 的 CUDA 版 torch。
4. **所有 .bat 必须纯 ASCII**。chcp 65001 下 cmd 解析含中文的行会把下一行
   开头吃掉（`if errorlevel` 被啃成 `orlevel`）。中文提示一律写在 ps1 里。
   bat 里 sleep 用 `ping -n N 127.0.0.1 >nul`——timeout.exe 在 stdin 被
   重定向时直接报错。
5. **所有含中文的 .ps1 必须 UTF-8 带 BOM**，否则 Windows PowerShell 5.1 按
   ANSI 读、中文全花。另外 PowerShell 5.1 不认 `&&`，用分号或分行。
6. **Ollama 是独立安装的服务**，它自动更新时端口会短暂消失。启动脚本已有
   60 秒等就绪轮询；如果安装时 Ollama 起不来，等它更新完重跑即可。

运行时：

7. **字幕全德语没中文** = Ollama 没跑或模型没拉。`ollama list` 查，
   subtitle.log 里有明确提示。翻译请求带 `keep_alive="2h"`，正常使用中模型
   不会被卸载；停止脚本会主动 `ollama stop` 释放显存。
8. **抓不到声音**：跟的是系统「默认播放设备」的 loopback。用户换了耳机/音箱
   约 5 秒内自动热切换；蓝牙设备偶尔注册成通信设备导致抓不到，⚙️ 面板
   「设备名包含」填设备名子串即可。
9. **venv 里的 python.exe 是启动器存根**：subtitle.pid 记的是存根 PID，真正
   的程序是它的子进程（Job 机制会连带管理，脚本已处理，别自作主张改）。
10. **main.py 有单实例 Mutex**，双开会自动退出并弹提示框，这是特性不是 bug。
11. **别在字幕运行时 benchmark 其它 Ollama 模型**：互相把对方挤出显存，测出
    来的全是重加载时间，数据无效。

改代码（如果用户让你改功能）：

12. 改完跑测试：`venv\Scripts\python -m pytest`（50+ 项，以实际输出为准）。test_hittest /
    test_resize_freedom / test_wordclick 是**独立脚本套件**（import 即开真窗口，
    pytest.ini 已把它们排除出收集，別删这个排除），用 `venv\Scripts\python
    test_hittest.py` 逐个跑。test_ui_polish 的两个 fade 用例对动画计时敏感，
    全量跑偶发挂、单独重跑即绿，别当回归追。**测试进程 import main.py 会被
    单实例 Mutex 直接 sys.exit**——参考 test_game_mode.py 顶部先打桩
    CreateMutexW 的写法。
13. **Qt 测试必须持模块级 QApplication 引用**，否则被 GC 后建 QWidget 直接
    qFatal 秒退（退出码 127、无任何输出，症状像"pytest 静默死"）。参考
    test_settings_sync.py 的 `_app()` + `_APP` 写法。
14. 悬浮窗是无 QLayout 的手动 setGeometry 布局 + WM_NCHITTEST 原生命中测试，
    半透明窗口有大量反直觉行为（alpha=0 像素鼠标穿透、顶层窗口 setStyleSheet
    底色不上屏等）。动 UI 前先读 window_frame.py / window_chrome.py 的注释
    和 test_hittest.py。
15. 用户可见文案是中文；代码注释写"为什么"而不是"做什么"，沿用现有风格。

## 5. 目录地图

```
main.py               入口：接线各模块、热键注册、单实例守卫（import 顺序敏感！）
audio_capture.py      WASAPI Loopback 采集 + 设备热切换
streaming_asr.py      local agreement 增量识别（词级前缀提交）
translator_queue.py   Whisper/Ollama 持有者：切句、翻译队列、草稿、查词、术语表
subtitle_window.py    悬浮窗主类（+ window_frame/window_chrome/subtitle_render/
                      window_geometry/settings_window/popups 拆分模块）
config.py             全部默认参数（仓库文件，别为单机改它）
config_local.py       本机覆盖（gitignore，install.ps1 生成，机器适配都写这）
install.ps1           一键安装 + 硬件检测 + 桌面快捷方式
update_subtitles.ps1  一键更新（git pull + 按需装依赖）
start/stop/pause_subtitles.ps1   启动（PID 管理/Ollama 保活）/停止/暂停
transcripts/          字幕存档（每天一个文件）
```

## 6. 装完的验收清单

1. `venv\Scripts\python -c "import config; print(config.WHISPER_MODEL, config.OLLAMA_MODEL)"`
   输出与硬件档位相符。
2. `ollama list` 里有配置对应的模型。
3. 双击"启动字幕.bat"→ 半分钟内屏幕下方出现悬浮窗。
4. 放一段德语视频（YouTube 搜 "tagesschau"）：德语白字先上屏，中文 1-3 秒
   内跟上。
5. `nvidia-smi` 显存占用符合预期档位；`ollama ps` 显示 100% GPU（有 N 卡时）。
6. Ctrl+Alt+P 暂停/恢复正常；"停止字幕.bat"能干净退出（悬浮窗消失、显存释放）。

装完把第 2 节的微调原则留给用户一句话："嫌慢或嫌翻译质量差，让你的 AI 按
CLAUDE.md 第 2 节调 config_local.py。"
