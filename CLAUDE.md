# CLAUDE.md

> 运行时代码在包 `realtime_subtitle/`（capture / asr / translate / ui）。入口仍是根目录 `main.py`。


> 目录分级见 [docs/STRUCTURE.md](docs/STRUCTURE.md)；Windows 脚本在 `scripts/windows/`。
 — 给接手这台电脑的 AI 助手（Claude Code 等）

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
- **磁盘空间预留 ~15GB**：venv 约 3-4GB + Whisper 模型 1-3GB + 翻译模型 2-6GB
  + **Ollama 程序本体约 2.8GB**（以前漏算了这项）。8GB 档实测合计 14.4GB，
  分散在四个位置、其中三个在仓库目录外，所以卸载要用 `uninstall.ps1`，
  光删仓库目录只能收回 venv 那部分。
- 驱动太旧的症状：程序启动时 cublas/cudnn 报错或 CUDA error。先
  `nvidia-smi` 看 CUDA Version，<12.0 就先升驱动，别急着折腾 Python 层。
- **中国大陆网络**：pip 走镜像加 `-Mirror` 参数；首次启动下载 Whisper 模型
  连不上 HuggingFace 的话，先设 `HF_ENDPOINT` 再启动：
  `[Environment]::SetEnvironmentVariable("HF_ENDPOINT","https://hf-mirror.com","User")`
  （对 huggingface_hub 生效；设完重开终端/重启程序）。Ollama 拉模型一般直连可用。
- 杀毒软件可能拦 pyaudiowpatch 的音频捕获或误报 venv 里的 exe——现象是
  装完启动无声音/进程被删，加白名单即可。

### 一键安装

**☠️ 克隆到纯英文路径**（`C:\realtime_subtitle` 这种）。中文 Windows 用户名
（`C:\Users\张三\...`）会让生成的启动 bat 直接损坏，根因见第 4 节第 4 条；
install.ps1 开头会拦住并让你换路径，但你先选对能省一趟。

前置三件套齐了之后（幂等，中断重跑即可）：

```powershell
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# 中国大陆网络：加 -Mirror 参数走清华 PyPI 镜像
```

install.ps1 最后一步会自检（import torch/PyQt5/pyaudiowpatch/soxr + 走一遍
`translator_queue._ensure_ml_deps()`）。自检不过就别急着让用户双击启动，
先按它打印的报错和第 4 节对。

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
  CUBLAS_STATUS_NOT_SUPPORTED 崩溃（4.6.2 起自动禁用 int8 回退）。
  requirements.txt 现在锁的是 **>=4.8.1**（下限被 dependabot 抬过，
  RTX 5060 上实测可用），别手动降版本。
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

### 版本号怎么改

**单一真相源是 `version.py` 的 `__version__`**，启动横幅、更新脚本、issue 模板
都读它。以前版本号写死在 main.py 横幅里（"实时字幕软件 v2.0"），既没有 tag
对得上、改了也没人知道，用户报 bug 只能贴 commit hash。

用语义化版本 `主.次.修`（当前 2.0.0，沿用横幅上一直写着的 v2）：

| 位 | 什么时候进位 | 例子 |
|---|---|---|
| **主** | 用户得动手才能继续用 | 配置项改名/删除、依赖大版本跳、装法变了 |
| **次** | 加了功能，老配置照跑 | 新增模式、新增脚本（如 uninstall.ps1）、新热键 |
| **修** | 只修 bug / 调文档 | 超时逻辑修正、README 更正、避坑清单补充 |

判据是**用户视角**，不是改动量：改了 800 行但用户什么都不用做 = 修补位；
改一行配置名把老 config_local.py 弄失效 = 主版本位。

发布步骤（合 PR 之后在 master 上做）：

```powershell
# 1. 改 version.py 的 __version__ 和 __version_date__
# 2. 提交
git commit -am "chore: 发布 v2.1.0"
# 3. 打 tag 并推送（tag 名带 v 前缀，和 __version__ 差一个 v）
git tag -a v2.1.0 -m "v2.1.0：一句话说清这版给用户带来什么"
git push origin master --tags
```

☠️ **`version.py` 必须保持纯常量、零 import**——`update_subtitles.ps1` 和
issue 模板都用 `Select-String` 正则读它（这样 venv 坏掉/还没建时也能读到版本），
往里加 import 会把那条路径弄坏。

用户是 `git pull` 到 master 而不是下 release 的，所以 tag 主要用途是：
让 issue 里的版本号能对上一段确定的代码，以及 `git log v2.0.0..HEAD` 一眼看清
这版改了什么。**版本号不进位也可以发**（修补更新），更新脚本会提示"版本号未变"。
### 出了问题怎么反馈（issue / PR）

先自查：`subtitle.err.log`（崩溃看这）和 `subtitle.log` 尾部 + 本文件第 4 节
避坑清单 + README 常见问题。确认是项目 bug（而不是本机环境/显存不够）后：

- **开 issue（首选）**：按 `.github/ISSUE_TEMPLATE/bug_report.md` 的格式把
  版本 commit、系统、显卡、config_local、两个日志尾部收集齐，然后
  `gh issue create -R wyl2607/realtime_subtitle --title "[bug] ..." --body-file 报告.md`
  （需要 GitHub 账号且 `gh auth login` 过；网页上开也一样）。
- **没有 GitHub 账号**：把同样内容整理成一段文字，交给用户让他直接发给作者
  （微信/QQ），内容齐了作者那边的 AI 一样能修。
- **自己已经修好了代码**想回馈：先装测试依赖
  `venv\Scripts\python -m pip install -r requirements-dev.txt`（pytest 不在
  requirements.txt 里，那份是给最终用户的），跑完 `venv\Scripts\python -m pytest`
  （全绿，条数以实际输出为准）再发 PR——`gh repo fork wyl2607/realtime_subtitle --remote=true`，
  开分支提交，push 到自己的 fork，`gh pr create`。改动尽量小、提交信息写清
  根因。不要 push 到 upstream（leik1000 是最初的模板仓库，早已分道扬镳）。
- 改代码前先双击"更新字幕.bat"拉到最新，避免在旧版上修已经修过的东西。

## 4. ☠️ 避坑清单（每一条都是真实踩过的）

安装/环境：

1. **PyQt5 必须在 torch 之后导入**。main.py 的 import 顺序是生死攸关的：先
   PyQt5 后 torch = `WinError 1114 (c10.dll)` 100% 复现。不要"整理 imports"。
2. **cublas64_12.dll 只认 PATH**。Windows 上 ctranslate2 按名字 LoadLibraryA
   加载，`os.add_dll_directory()` 无效。realtime_subtitle/translate/translator_queue.py 顶部把
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
   不会被卸载；优雅退出/停止脚本会通过 HTTP `keep_alive=0` 主动卸载释放
   显存（☠️ 别改回 `ollama stop` CLI——Ollama 服务没运行时它会自己拉起
   服务并无限期等待，停止脚本窗口永远关不掉，2026-07-20 实测）。
8. **抓不到声音**：跟的是系统「默认播放设备」的 loopback。用户换了耳机/音箱
   约 5 秒内自动热切换；蓝牙设备偶尔注册成通信设备导致抓不到，⚙️ 面板
   「设备名包含」填设备名子串即可。
9. **venv 里的 python.exe 是启动器存根**：subtitle.pid 记的是存根 PID，真正
   的程序是它的子进程（Job 机制会连带管理，脚本已处理，别自作主张改）。
10. **main.py 有单实例 Mutex**，双开会自动退出并弹提示框，这是特性不是 bug。
11. **别在字幕运行时 benchmark 其它 Ollama 模型**：互相把对方挤出显存，测出
    来的全是重加载时间，数据无效。
12. **音频重采样必须保持滤波器状态**。`realtime_subtitle/capture/audio_capture.py` 用
    `soxr.ResampleStream`，不是每块调一次无状态的 `soxr.resample()`——后者
    多相滤波器状态每次归零，等于每 42ms 注入一次瞬变。实测（96k→16k、
    4096帧/块、纯音频谱能量比）：一次性重采样 90.4dB，逐块无状态只有
    **34.3dB**。喂给 Whisper 的每一帧都会叠一层噪声底。
13. **任何"退出前卸载 Ollama 模型"的路径，都要先等在飞的请求落地**。翻译
    /查词/预热请求都带 `keep_alive="2h"`，只要有一个在 `_unload_our_models()`
    之后才返回，模型就被重新拉回显存留驻两小时。现在预热线程和查词线程
    各有一个有界 3 秒的等待（`shutdown()` 里），加新的 Ollama 调用路径时
    记得一起处理。
14. **模式系统只有一个写入口**：`main.py::SubtitleApp._apply_mode`。⚙️面板的
    四个按钮和 Ctrl+Alt+G 都转发到它；面板控件的显示一律靠
    `refresh_from_config()` 读回，不要再给面板加"自己 setValue 一遍"的旁路
    （2026-08-02 合并模式系统时删掉的就是那套）。

改代码（如果用户让你改功能）：

15. 改完跑测试：`venv\Scripts\python -m pytest`（137 项，以实际输出为准）。
    ☠️ **pytest 不在 requirements.txt 里**（那份是给最终用户装的，install.ps1
    不会装 pytest），新环境上第一次跑会报 `No module named pytest`，先装：
    `venv\Scripts\python -m pip install -r requirements-dev.txt`。test_hittest /
    test_resize_freedom / test_wordclick 是**独立脚本套件**（import 即开真窗口，
    pytest.ini 已把它们排除出收集，別删这个排除），用 `venv\Scripts\python
    test_hittest.py` 逐个跑。**测试进程 import realtime_subtitle.app 会被
    单实例 Mutex 直接 sys.exit**——import 之前设
    `os.environ["REALTIME_SUBTITLE_NO_SINGLETON"] = "1"`（参考 test_game_mode.py
    顶部）。以前是 monkeypatch `ctypes.windll.kernel32.CreateMutexW`，
    app.py 改用 `use_last_error=True` 的独立 WinDLL 句柄之后那种打桩已经失效
    （patch 的是 `ctypes.windll.kernel32`，模块拿的是另一个句柄），别照抄旧写法。UI 动画用例一律用 test_ui_polish.py 的 `_pump_until`
    等条件成立，别写"固定 pump 若干毫秒再断言"（那样在忙机器上会偶发挂，
    以前 fade 两个用例就是这么变成"重跑即绿"的假回归的）。
16. **Qt 测试必须持模块级 QApplication 引用**，否则被 GC 后建 QWidget 直接
    qFatal 秒退（退出码 127、无任何输出，症状像"pytest 静默死"）。参考
    test_settings_sync.py 的 `_app()` + `_APP` 写法。
17. 悬浮窗是无 QLayout 的手动 setGeometry 布局 + WM_NCHITTEST 原生命中测试，
    半透明窗口有大量反直觉行为（alpha=0 像素鼠标穿透、顶层窗口 setStyleSheet
    底色不上屏等）。动 UI 前先读 window_frame.py / window_chrome.py 的注释
    和 test_hittest.py。
18. 用户可见文案是中文；代码注释写"为什么"而不是"做什么"，沿用现有风格。
19. **桌面「操作说明.txt」不要直接改**：正文的单一真相源是
    `docs/zh/user-guide-template.txt`（install.ps1 用它生成，`{{INSTALL_DIR}}` 会被
    替换成安装目录）。改说明改模板，否则下次谁跑一次 install 就被覆盖回去。
20. **这两处看着像可优化点，实测都不是**（2026-08-02 量过，别再翻）：
    - `realtime_subtitle/asr/streaming_asr.py` 的 `vad_filter=True` 不是"每 0.5 秒重复跑一遍的冗余
      开销"。有语音的缓冲上它只差 ±10%（12 秒缓冲 360ms vs 328ms，文本一致）；
      12 秒纯静音缓冲上是 **17ms vs 255ms**，而且关掉后 Whisper 会吐经典德语
      幻觉 "Untertitelung des ZDF, 2020"。它是防幻觉主力，不是负担。
    - `_render` 的二分测高不是 UI 卡顿源：20 句对满屏 p50 **1.2ms**、max 1.6ms
      （每秒只调几次）。真卡顿要往别处找（GPU 抢占、翻译阻塞）。
    - **调小 `BUFFER_TRIM_SEC` 省不了编码开销**（2026-08-04 查证）。
      faster-whisper 每段进编码器前都 `pad_or_trim` 补到固定 30 秒
      （`transcribe.py:1180` + `feature_extractor.py` `nb_max_frames=3000`），
      所以 6 秒缓冲和 12 秒缓冲的**编码代价完全一样**，只有解码随 token 数变。
      "缓冲短一点识别就快一点"是错的直觉。概况行里的
      `缓冲短x.xxs(n)/长x.xxs(n)` 分桶就是给这条结论收实测数据的——
      两桶 p50 基本持平即证实，不要再去做那个 A/B。
    - **ASR 的 GPU 占空比约 50%，且基本不可压**：概况实测 116 次/60 秒
      × p50 0.26 秒 ≈ 30 秒，即一半墙钟。次数由 `CHUNK_SUBMIT_SECONDS=0.5`
      决定（每块一次），单次耗时由上一条锁死。要降只能拉长分块间隔，
      代价是德语上屏更晚——「⚡性能」模式（1.0）就是这个取舍，别改默认值。
    - **流式草稿那条 0.15 秒热路径不需要合并窗口**（2026-08-12 实测）。
      `on_partial → _update_draft` 一次要做两件事：`tv_window.update_draft()`
      （QTextEdit 文档布局）+ `_render()`（二分测高最多 5 轮 setHtml）。
      看着像"每 0.15 秒干两遍重活"，实测 200 次采样：

      | 场景 | p50 | p90 |
      |---|---|---|
      | 20 句对满屏 / 电视窗关 | 0.61ms | 0.66ms |
      | 20 句对满屏 / 电视窗开(TV字号64) | 0.76ms | 1.23ms |
      | 20 句对满屏 / 电视窗开(TV字号160，上限) | 0.75ms | 0.77ms |
      | 4 句对（⚡性能模式） | 0.16ms | 0.17ms |

      最坏 0.76ms 对 150ms 预算 = **0.5%**。加 QTimer 合并窗口只会增加复杂度
      和一档延迟，换不回任何东西，别做。
      两个副产物结论：**电视窗只贵 0.15ms**，而且 **TV 字号 64→160 没有差别**
      ——后者证实了 `_update_bottom_anchor` 里那个"稳态留白恒为 0 就不
      setFrameFormat"的优化是真生效的（否则 160px 下整篇重排会很明显）。
    - **`_render` 只构造要显示的句对，省下的是 0.05ms**（同日 A/B）。
      内存里攒满 `HISTORY_KEEP=50` 条、上屏上限 20 条的稳态下：全构造再切
      p50 0.65ms，只构造 20 条 p50 0.60ms。改动本身语义等价、留着没坏处，
      但**别把它当成性能手段去推广**——这条路径的绝对开销本来就可以忽略。
21. **☠️ 所有打 Ollama 的请求必须共用一个 `num_ctx`**（`config.OLLAMA_NUM_CTX`）。
    Ollama 的 runner 按 **(模型, 上下文长度)** 缓存：换一个 num_ctx 就等于换一个
    runner，会把 5.6GB 模型整个重装一遍。2026-08-04 实测（字幕程序在跑、翻译流
    持续占用）——查词曾写死 `num_ctx=2048` 而翻译是 4096，于是**每次点词都触发
    重载**：`load_duration` 6.9~8.7 秒、单次查词 10.4~12.5 秒；更糟的是
    **紧接着的那次字幕翻译还要再付 ~2.2 秒把 4096 的 runner 装回来**，
    所以这个 bug 不只拖慢查词，还拖慢主字幕链路。统一成 4096 之后
    `load_duration` 0.27 秒、单次查词 3.3~4.0 秒。加新的 Ollama 调用路径时
    别写 num_ctx 字面量，`test_lookup_worker_shares_num_ctx_with_translation`
    会盯着查词这一条。
22. **☠️ `OLLAMA_BASE_URL` 必须写 `http://127.0.0.1:11434`，不能写 `localhost`。**
    Ollama 只监听 IPv4（`netstat -ano | findstr 11434` 看得到只有
    `127.0.0.1:11434` 一条），而 Windows 上 `getaddrinfo("localhost")` 返回
    **`::1` 在前**、`127.0.0.1` 在后。IPv6 环回**不会快速失败**——实测要
    **2021ms** 才拒绝，之后才回退 IPv4。再加上流式响应 `done` 后 `break` +
    `close()` 让连接无法复用（试过排干剩余字节，没用），**每一句字幕都要重连
    一次、每次都付满这 2 秒**。2026-08-04 实测（GPU 空闲、模型已驻留）：

    | | localhost | 127.0.0.1 |
    |---|---|---|
    | 翻译 p50 | 2.88 秒 | **0.60 秒** |
    | 查词 p50 | 3.11 秒 | **0.87 秒** |

    这 2.04 秒和显卡、模型、prompt 一概无关，纯粹是 DNS。仓库里所有 .ps1
    脚本一直用的就是 127.0.0.1，只有 config.py 那一行是 localhost，所以
    "脚本很快、字幕很慢"看着像 GPU 问题。启动时有 `_warn_if_ipv6_first_host()`
    兜底告警（防 config_local.py 写回主机名），`test_ollama_base_url_is_ipv4_literal`
    盯着默认值。
23. **量 Ollama 延迟一定要看 `load_duration`**，别只看墙钟。上一条那个 bug
    藏了两个多月，就是因为之前只量了墙钟总时长、把 7 秒重载当成了"prompt
    处理+固定开销"，还据此去砍 `num_predict`（砍了没用，生成本来就只占 1 秒）。
    `/api/generate` 的响应里 `load_duration` / `prompt_eval_duration` /
    `eval_duration` 三个字段是分开的，一眼就能看出时间花在哪。
    **还要拿 `total_duration` 和客户端墙钟对一下**：上一条那个 2 秒的 DNS 税
    就是这么揪出来的——Ollama 自报 `total_duration` 只有 385ms，客户端墙钟却是
    2437ms，差值 2052ms 稳如磐石，一看就不在 GPU 上。判据：
    `wall - total_duration` 应该是个位数毫秒，明显大于它就说明卡在传输层。
24. **显示缩放（高DPI）只做了首次运行的默认值，没有全局开 Qt 缩放。**
    本项目所有字号都是像素单位（`setPixelSize` / 样式表 `font-size: Npx`），
    而 Qt5 的 `AA_EnableHighDpiScaling` 默认关闭。笔记本几乎都是 125%/150%
    缩放，直接跑字会小三分之一。**不要顺手去开那个全局开关**——悬浮窗是无
    QLayout 的手动 setGeometry + WM_NCHITTEST 原生命中测试（第 17 条），
    开缩放会改坐标空间、动到命中测试，得连 test_hittest 一起重做。
    现在的折中：`window_geometry.screen_scale_factor()` 读主屏逻辑 DPI，
    **只在首次运行**（还没有 window_state.json）按倍率放大默认字号和默认
    窗口尺寸，用户 Ctrl+滚轮调过之后一律以保存值为准。**按钮条/设置面板等
    chrome 的字号仍然没跟着缩放**，高DPI 屏上偏小是已知的、还没解决。
    同一处还修了另一个换机器才暴露的问题：config 里的 `WINDOW_X/Y` 是按
    开发机屏幕写死的绝对坐标，1366x768 的小笔记本上 y=750 整窗掉出屏幕，
    只能靠钳制拽回屏幕正中间；首次运行现在改走 `default_geometry()`
    按实际屏幕算贴底居中。
25. **☠️ "模型在显存里"≠"15 秒够用"，翻译超时不能只看 `_ollama_hot`**
    （issue #16，2026-08-09 装机时撞上）。`_ollama_hot` 的语义是"拿到过一次
    200"，但一次翻译能不能在 15 秒内回来，取决于**这一刻 GPU 排不排得上队**。
    首启现场：预热 43.9 秒就成功了、模型确实常驻，可 Whisper 到 155.8 秒才
    加载完，随后要集中消化这 156 秒攒下的音频积压，GPU 被 ASR 打满，翻译
    排在后面——15 秒必然不够。
    **真正难查的是它会来回震荡**：超时后代码把 `_ollama_hot=False` 让重试走
    90 秒（对的），可重试一成功 `_note_tx_result(True)` 立刻把它翻回 True，
    于是**下一句又从 15 秒重新开始、再超一次**。日志里连着几条
    「翻译超时(15秒)」不是同一次重试的回显，是每句各白烧了一次 15 秒；
    队列同时继续堆积触发 `_trim_tx_queue` 丢句，实测翻译 p50 冲到 60.8 秒。
    用户看到的现象是"装完第一次用，头一两分钟只有德语没中文"，
    正好命中 README 那条「字幕全是德语」FAQ，极易误诊成 Ollama 没跑。
    现在超时统一走 `_translate_timeout()`，除了冷热还看两个信号：
    **ASR 积压**（`_asr_backlog_n ≥ TRANSLATE_SLOW_BACKLOG_BLOCKS` 就用长超时）
    和**降级粘性**（`TRANSLATE_SLOW_STICKY_SEC` 内维持长超时，掐掉震荡）。
    `_asr_backlog_n` 是收件箱长度的**无锁 int 快照**——别改成让翻译线程去拿
    `_asr_lock`，那会引入 `_tx_lock`/`_asr_lock` 的锁序问题，而这里只需要
    一个近似值。加新的 Ollama 调用路径时超时一律问 `_translate_timeout()`，
    别再写 `if self._ollama_hot else` 那个二选一。

26. **☠️ 挪模块时最容易漏的是函数体里的延迟 import**（2026-08-10，包化重构
    当天）。把运行时代码挪进 `realtime_subtitle/` 包时，各测试文件**顶部**的
    import 都改了，但函数体里那 30 多处延迟 import 一个都没动：

    ```python
    def test_xxx():
        from translator_queue import WhisperQueueTranslator   # 已不在顶层
        import subtitle_window as sw
    ```

    本项目到处都是这种延迟 import，而且是有意为之（避免在导入期把
    torch/PyQt5 整条链拉起来、绕开单实例 Mutex），所以数量远多于普通项目。
    结果是 master 的 CI 连红 6 次：ubuntu 18 failed、windows 一堆
    `ModuleNotFoundError`。

    **搜索时别只 grep 顶格的 `^import` / `^from`**，要连带缩进的一起搜：

    ```powershell
    Select-String -Path *.py,tests\*.py -Pattern '^\s+(import|from) (translator_queue|subtitle_window|config|version)\b'
    ```

    同一类还有两处不走 import 的引用，一样会漏：
    `pathlib.Path(__file__).with_name("version.py")` 这种按相对位置找文件的
    （测试挪进 `tests/` 之后它指向 `tests\version.py`），以及 .ps1 脚本里的
    `python -c "import config; ..."`。

27. **☠️ pytest `exit 127 + 零输出` 不止一种成因**（2026-08-10）。第 16 条记的是
    "QApplication 被 GC → 建 QWidget 时 qFatal"，但**延迟 import 路径写错也是
    同样的现象**：

    ```
    ..............................FF
    ##[error]Process completed with exit code 127
    ```

    一行 traceback 都没有。当时真因是 `subtitle_window._ai_web_enabled()` 里
    写了重构前的 `from translator_queue import ...`，被 `_show_ai_analysis`
    调到时炸在 import 上。

    **区分办法**：`python -m pytest tests/xxx.py -x --tb=short` 单文件跑一遍，
    第一个真实失败的 traceback 就会出来（整套跑时它被后面的 qFatal 盖掉了）。
    没有 Qt 环境时，本机 `pip install PyQt5` + `QT_QPA_PLATFORM=offscreen`
    就能复现（torch 只要能 import，临时放个空的 `torch.py` 在 PYTHONPATH 里
    即可，不用真下几百 MB）。

28. **☠️ 运行时文件的路径一律走 `realtime_subtitle/paths.py`，别再用 `__file__` 推**
    （2026-08-11，上一条的直接后果，隔了一天才发现）。第 26 条只修了 import，
    **按 `__file__` 算落点的代码一处都没改**，于是包化之后：

    | 文件 | 实际落到 | .ps1 / README 说的 |
    |---|---|---|
    | `.paused` / `.stop` | `realtime_subtitle\capture\` | `<仓库根>\` |
    | `transcripts\` | `realtime_subtitle\translate\` | `<仓库根>\` |
    | `window_state.json` | `realtime_subtitle\ui\` | `<仓库根>\` |

    这类 bug **单看每一处都是对的**，只有把 Python 和 .ps1 两边对起来才暴露，
    所以症状极其难归因：

    - **「暂停继续字幕.bat」完全失效**——脚本在根目录建 `.paused`，程序在
      `capture\` 查，还照常打印"已暂停"。Ctrl+Alt+P 反而是好的（同一个常量）。
    - **停止脚本的优雅退出路径从此再没被触发过**——`.stop` 同理收不到，每次
      都走满 5 秒宽限再强杀。强杀掉的正是 `shutdown()` 里排在后面的
      `_save_lookup_cache()` 和 `_unload_our_models()`：**查词缓存每次退出都丢、
      显存卸载只剩 stop_subtitles.ps1 那条 HTTP 兜底**。第 13 条花大力气防的
      竞态，从另一个方向复活了。

    现在唯一真相源是 `realtime_subtitle/paths.py` 的 `REPO_ROOT` / `repo_path()`
    （零项目内 import，config.py 也用它）。`tests/test_runtime_paths.py` 盯着这件事，
    而且第一条用例是**静态扫描**——它拦的是整个类别（禁止
    `os.path.dirname(os.path.abspath(__file__))`），以后新增运行时文件照样会被拦下，
    不用记住今天这六个位置。加新的运行时文件时：写 `repo_path("xxx")`，
    并去 test_runtime_paths.py 的 parametrize 里补一行。

29. **☠️ `OLLAMA_BASE_URL` 现在是被强制校验的，不是"建议"。** 启动时
    `translator_queue._assert_local_ollama()` 会解析它、要求解析出来的地址
    **全部是环回地址**，否则直接抛异常拒绝启动（悬浮窗上会持久显示原因）。

    为什么要做到这一步：README 第一句写的是「不向任何云端发送音频或文本」，
    但在这条校验之前，保证这件事的只有 config.py 里那一行默认值——而
    `config_local.py` 是被 `exec_module` 加载的，而且**本文件第 2 节明确
    鼓励你去写它**。一个手滑的 `OLLAMA_BASE_URL` 就会把系统全部声音的转录
    （可能含语音通话）连同上下文一路 POST 到外网，而屏幕上和日志里不会有
    任何异样：翻译照常出中文。这是全项目唯一一条能静默违反自身隐私承诺的
    路径，所以它是硬失败而不是警告——起不来是看得见的，能被修。

    用户确实要连局域网里另一台机器的 Ollama：在 `config_local.py` 里写
    `ALLOW_REMOTE_OLLAMA = True` 显式声明。**不要**为了让程序跑起来而顺手
    加这一行，先确认用户知道自己在同意什么。

    同一条链路上还有第二道：`_ollama_identity_ok()` 在每次翻译前确认 11434
    后面确实是 Ollama（响应体里有 version 字段）。以前只在启动时查一次，
    而 Ollama 是会自动更新并重启的（第 6 条），重启窗口期里端口是空的、
    本机任何进程都能补位。加新的 Ollama 调用路径时不用自己重复这套，
    但也别绕过 `_translate_single_sentence` 直接发转录。

30. **`numpy` 下限必须停在 2.2，不要跟 dependabot 抬到最新。** 2026-08-12
    审计时 `requirements.txt` 写的是 `numpy>=2.5.1`，而 numpy 2.5 起
    `requires_python >=3.12`、连 cp310 轮子都没有。本机只有 3.13 所以装得上，
    但 `install.ps1` / README 仍接受 3.10–3.13——3.10 上 `pip install -r
    requirements.txt` 会直接失败。版本门槛：2.2.x ≥3.10，2.3/2.4 ≥3.11，
    2.5+ ≥3.12。3.12/3.13 会自己漂到 2.5.x，不用把下限抬上去。
    同一轮评估过 `--generate-hashes` 锁文件：一份锁钉不死 3.10–3.13 的
    wheel 矩阵，再叠加 CPU 档要滤掉 `nvidia-*` 行，hash 校验会碎。放弃，
    理由写在 `requirements.txt` 文件头。

## 5. 目录地图

```
main.py               入口：接线各模块、热键注册、单实例守卫（import 顺序敏感！）
realtime_subtitle/paths.py    运行时文件落点的唯一真相源（REPO_ROOT/repo_path，见第4节第28条）
realtime_subtitle/capture/audio_capture.py      WASAPI Loopback 采集 + 设备热切换
realtime_subtitle/asr/streaming_asr.py      local agreement 增量识别（词级前缀提交）
realtime_subtitle/translate/translator_queue.py   Whisper/Ollama 持有者：切句、翻译队列、草稿、术语表
realtime_subtitle/translate/lookup.py    点词查词 + 🤖AI分析（LookupMixin，被上面那个类 mixin 进去）
realtime_subtitle/ui/subtitle_window.py    悬浮窗主类（+ window_frame/window_chrome/subtitle_render/
                      window_geometry/settings_window/popups 拆分模块）
config.py             全部默认参数（仓库文件，别为单机改它）
config_local.py       本机覆盖（gitignore，install.ps1 生成，机器适配都写这）
scripts/windows/install.ps1  一键安装 + 硬件检测 + 桌面快捷方式（根目录 install.ps1 为兼容转发）
scripts/windows/update_subtitles.ps1  一键更新（git pull + 按需装依赖）
scripts/windows/uninstall.ps1  卸载（逐项问 Y/N，默认不删）；-CleanCache 只清下载残file
scripts/windows/{start,stop,pause}_subtitles.ps1  启动（PID 管理/Ollama 保活）/停止/暂停
tests/                所有 pytest 用例（三个独立 GUI 脚本套件见第 4 节第 15 条）
requirements-dev.txt  测试依赖（pytest），只有改代码的人要装
transcripts/          字幕存档（每天一个文件）
```

☠️ **包化之后 `import config` 不再可用**，一律写 `from realtime_subtitle import config`。
仓库里的 .ps1 脚本已经全改过了，写新命令/新文档时别照抄旧写法（第 4 节第 26 条）。
`config_local.py` 仍在**仓库根目录**（和 main.py 并排），不在包里面。

## 6. 装完的验收清单

1. `venv\Scripts\python -c "from realtime_subtitle import config; print(config.WHISPER_MODEL, config.OLLAMA_MODEL)"`
   输出与硬件档位相符。
2. `ollama list` 里有配置对应的模型。
3. 双击"启动字幕.bat"→ 几秒内屏幕下方出现悬浮窗（带"⏳正在加载"提示），
   10-30 秒后提示变"✅已就绪"（模型在后台加载，翻译模型同时并行预热）。
4. 放一段德语视频（YouTube 搜 "tagesschau"）：德语白字先上屏，中文 1-3 秒
   内跟上。
5. `nvidia-smi` 显存占用符合预期档位；`ollama ps` 显示 100% GPU（有 N 卡时）。
6. Ctrl+Alt+P 暂停/恢复正常；"停止字幕.bat"能干净退出（悬浮窗消失、显存释放）。

装完把第 2 节的微调原则留给用户一句话："嫌慢或嫌翻译质量差，让你的 AI 按
CLAUDE.md 第 2 节调 config_local.py。"
