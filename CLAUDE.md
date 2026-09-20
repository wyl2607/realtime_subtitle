# CLAUDE.md

> 运行时代码在包 `realtime_subtitle/`（capture / asr / translate / ui / offline）。实时入口是根目录 `main.py`，离线视频入口是 `download_subtitle.py`。


> 目录分级见 [docs/STRUCTURE.md](docs/STRUCTURE.md)；Windows 脚本在 `scripts/windows/`。
 — 给接手这台电脑的 AI 助手（Claude Code 等）

这是一个**完全本地运行**的实时字幕系统：捕获 Windows 正在播放的声音（直播/视频/
语音聊天），Faster-Whisper 实时识别德语，Ollama 本地大模型翻译成中文，PyQt6
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

install.ps1 最后一步会自检（import torch/PyQt6/pyaudiowpatch/soxr + 走一遍
`translator_queue._ensure_ml_deps()`）。自检不过就别急着让用户双击启动，
先按它打印的报错和第 4 节对。

install.ps1 会：找 Python → nvidia-smi 检测显卡 → 建 venv 装依赖 → 按显存
生成 `config_local.py` 降级配置 → 启动 Ollama 并拉取配置对应的翻译模型 →
在桌面生成「德语直播实时字幕」文件夹（启动/下载并加字幕/停止/暂停/更新/卸载 bat + 说明）。

首次点"启动字幕.bat"还会自动下载 Whisper 模型（1-3GB），属于正常现象。

## 2. 硬件适配与模型选择（你最重要的工作）

**机制**：所有机器相关配置写 `config_local.py`（在 .gitignore 里，会覆盖
config.py 同名项）。**永远不要为了适配这台机器去改 config.py**——那是仓库
文件，改了会让以后 `git pull` 更新冲突。

### ☠️ AI 助手能往 config_local.py 写哪些键

`config_local.py` 是被 `exec_module` 加载的，**它就是任意代码执行**。本节又
在明确请你去写它，所以这里划一条线：**装机适配只需要下面这些键**，写这些
不用问；其它任何键（尤其是下面第二组）先跟用户说清楚再动。

| 可以直接写 | 用途 |
|---|---|
| `WHISPER_MODEL` / `WHISPER_DEVICE` / `WHISPER_COMPUTE_TYPE` | 显存分档 |
| `OLLAMA_MODEL` / `GAME_MODE_OLLAMA_MODEL` | 显存分档 |
| `CHUNK_SUBMIT_SECONDS` / `WHISPER_BEAM_SIZE` | 识别跟不上时降档 |
| `LANGUAGE_PAIRS` / `AUTO_DETECT_LANGUAGE` 及其滞回参数 | 用户明说了要哪几种语言时（同一个源语言可以配多条，如 `("zh","en")` 和 `("zh","de")`，面板会各出一个按钮，见第 4 节第 41 条） |
| `LOOPBACK_DEVICE_NAME` | 抓不到声音时指定设备 |

**这三个键会改变"数据往哪去"，绝不能为了"让它跑起来"顺手加**：

- `OLLAMA_BASE_URL` —— 改错一个字就把系统全部声音的转录送出本机。
- `ALLOW_REMOTE_OLLAMA` —— 它的唯一作用就是**关掉**上面那道校验。
  程序起不来时加这一行是最省事的"解法"，也正是最坏的那个。
- `AI_ANALYSIS_WEB_URL_TEMPLATE` —— 唯一的出网路径指向哪个站。
- `OFFLINE_COOKIES_FILE` / `OFFLINE_COOKIES_FROM_BROWSER` —— 离线下载用的
  **凭据**。要不要拿自己的登录态去下载受限内容是用户自己的决定，别为了
  "让某个链接能下"顺手加上；尤其 `FROM_BROWSER` 等于把整个浏览器登录态
  交给下载器。先说清楚再让用户点头。

不确定的时候：先把要改的键和理由说出来，让用户点头，别自己代劳。

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
  里，更新永远不会碰它们（包括 `downloads/`）。
- **更新失败**基本都是有人直接改了仓库文件。处理：`git stash` 后重试；根治：
  把改动挪进 config_local.py 或让上游合并。

### 版本号怎么改

**单一真相源是 `version.py` 的 `__version__`**，启动横幅、更新脚本、issue 模板
都读它。以前版本号写死在 main.py 横幅里（"实时字幕软件 v2.0"），既没有 tag
对得上、改了也没人知道，用户报 bug 只能贴 commit hash。

用语义化版本 `主.次.修`（当前 2.5.0）：

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

1. **torch 必须在 Qt 之前导入**——PyQt5 时代这是硬伤，Qt6 下降级成了「零成本
   的保险」，但**结论不变：别动那个顺序，也别"整理 imports"**。

   2026-09-20 迁移第 3 步实测（同机、torch 2.14.0+cpu，两个方向各起干净进程）：

   | 先导入 | 后导入 | 结果 |
   |---|---|---|
   | PyQt5 | torch | ❌ `WinError 1114`，c10.dll 初始化例程失败 |
   | torch | PyQt5 | ✅ |
   | PyQt6 | torch | ✅ |
   | torch | PyQt6 | ✅ |

   **换成 PyQt6 之后这个坑不再复现**（PyQt5 那两行是反向对照，证明测法本身
   有效、不是空转）。仍然保留 `app.py` 顶部那句显式 `import torch` 的理由：
   ctranslate2 本来就会无条件 import torch，把它提前到一个确定的位置一分钱
   不花；删掉它则等于拿一个只在「本机 + 这一组 PyQt6/Qt/torch 版本」上验过
   一次的结论，去换一行免费的保险。真要删，先在别的机器、别的版本组合上把
   两个方向都复现一遍。
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
    /查词请求带 `keep_alive="2h"`，启动预热用短租期 `STARTUP_WARM_KEEP_ALIVE`
    （60 秒）。只要有一个 2h 请求在 `_unload_our_models()` 之后才返回，模型
    就被重新拉回显存留驻两小时。加载中途退出时 `self.translator` 仍是 None，
    `stop()` 必须走 `release_startup_warm()`，不能只靠 shutdown。现在预热
    线程和查词线程各有一个有界 3 秒的等待，加新的 Ollama 调用路径时记得
    一起处理。
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
    - **中文的复读压缩不用做**（2026-08-13 查证）。`_squash_repeats` 按
      `.split()` 数词 + `" ".join` 重组，对中文确实是**死的**（整段恒等于
      1 个"词"）——第 32 条那一串漏改里它是唯一一个"代码层面是死的、但
      没死出后果"的。拿本机 5 天存档跑 n-gram 连续重复检测：1851 条原文里
      **只命中 1 条，还是德语，而且是真人在念一个综艺节目名**（"geil geil
      geil"），中文 315 条**零命中**。
      原因是词流入口的 `_collapse_word_runs(keep=3)` 已经在 token 级把复读
      掐掉了，而中文 faster-whisper 的 word 粒度接近逐字，所以那一层对中文
      本来就是生效的，`_squash_repeats` 拿到的东西早就干净了。
      真要动之前先按同样办法量一遍自己的存档，别照着"代码读起来不对"就改。
    - **调小 `BUFFER_TRIM_SEC` 省不了编码开销**（2026-08-04 查证）。
      faster-whisper 每段进编码器前都 `pad_or_trim` 补到固定 30 秒
      （`transcribe.py:1180` + `feature_extractor.py` `nb_max_frames=3000`），
      所以 6 秒缓冲和 12 秒缓冲的**编码代价完全一样**，只有解码随 token 数变。
      "缓冲短一点识别就快一点"是错的直觉。

      ☠️ **但原来那条判据是错的，别再照它下结论**（2026-08-17 更正）。原文写的是
      "概况行 `缓冲短/长` 两桶 p50 基本持平即证实，不要再去做那个 A/B"。
      跨 264 个概况窗口攒下来的实际数据是 **短 0.280s / 长 0.360s（+29%）**，
      两桶从来就不持平，87% 的窗口都是长桶更慢——照那条判据读，等于这条结论
      被自己的仪表推翻了。**而真去做 A/B 就会发现结论其实是对的，错的是判据。**

      分桶是**观察性**的：长缓冲的轮次同时也是"说话人在连续讲"的轮次，token 多、
      解码本来就久。缓冲长度和耗时相关，但不是因果。真正的干预实验（同一段
      150 秒德语音频回放、同一个已加载模型、三种配置交错跑各 3 次、
      基线三次极差 0.010s 作噪声下界）：

      | 配置 | p50 均值 | vs 基线 |
      |---|---|---|
      | `TRIM 12 / KEEP 8`（现状） | 0.321s | 基线 |
      | `TRIM 12 / KEEP 5` | 0.323s | **+0.8%（噪声内，零效应）** |
      | `TRIM 8 / KEEP 4` | 0.283s | **-11.9%** |

      两条可直接用的结论：
      1. **动 `BUFFER_KEEP_SEC` 完全没用**。两种设置下平均缓冲都是 8.3 秒——
         主裁剪 `_chunk_completed_segment` 在 segment 边界就把缓冲压下去了，
         `KEEP` 那条兜底路径很少真正生效，改它等于没改。
      2. 只有 `BUFFER_TRIM_SEC` 是真旋钮，12→8 换来 -11.9%。**但仍然不建议动**：
         它解决不了实际瓶颈（线上 p90 0.68s 中，与 Ollama 抢卡就占 +48%，
         见下一条），而代价是标点/断句大面积变动（同段音频逐词 diff：多数是
         `2`↔`zwei`、`Sachsen-Anhalt`↔`Sachsen -Anhalt` 这类churn，有变好的也有
         变坏的，净收益说不清）。

      教训比结论本身值钱：**分桶统计只能证伪"完全无关"，不能用来估计干预效果**。
      要回答"改这个常量会怎样"，只能真去改一次量一次。
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
    - **离线 checkpoint「每翻一条存一次整份 rows」不用改成节流**（2026-09-10 量过）。
      看着像 N² ——每条都把全部行序列化一遍——但常数极小。合成字幕实测
      （单次 `persist()` 中位数 / 文件大小 / 整段累计）：

      | 条数 | 目标数 | 单次保存 | 文件 | 整段累计 |
      |---|---|---|---|---|
      | 500 | 1 | 1.8ms | 119KB | 0.9s |
      | 500 | 2 | 1.7ms | 155KB | 0.9s |
      | 2000 | 1 | 3.6ms | 482KB | 7.2s |
      | 2000 | 2 | 4.0ms | 625KB | 8.1s |

      2000 条约等于一小时视频，翻译本身要几十分钟，这 8 秒是 **0.4%**。
      改成"每 5 条存一次"省不下有意义的时间，却要拿断点进度去换（异常强退最多
      重翻 5 条）。**别做这个优化。**
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

    **☠️「预热」也是一条 Ollama 调用路径**（2026-08-28 补，本条最容易漏的地方）。
    `_startup_warm_ollama` / `_warm_model_worker` 那两个空 prompt 请求曾经不带
    `num_ctx`，于是装出来的是**默认上下文长度**的 runner，首句翻译一请求就换
    runner、整个模型重装一遍——**预热白做，而它存在的唯一理由就是免掉这笔钱**。
    实测（qwen3.5:4b / Ollama 0.33.1）：预热(默认) → 翻译 `num_ctx=8192`
    `load_duration` **6.34 秒**；预热带上 `num_ctx` 之后同样这句 **0.00 秒**。

    这个 bug 藏得住是因为 **Ollama 当前的默认上下文长度恰好也是 4096**，和
    `OLLAMA_NUM_CTX` 撞上了。别指望这个巧合：用户按第 2 节改一下
    `OLLAMA_NUM_CTX`、或者 Ollama 哪次升级动了默认值，它就没了。而且
    **失败是静默的**——日志照样打印「🔥 预热完成」，只有首句白付 6 秒，
    然后正好撞上 `OLLAMA_TIMEOUT=15` 和第 25 条那套超时震荡，现象是
    「装完第一次用，头一两分钟只有德语没中文」，极易误诊成 Ollama 没跑。
    `test_warm_requests_share_num_ctx_with_translation` 盯着这两条路径，
    它**故意把 num_ctx 设成 4096 以外的值**，就是为了不让那个巧合把测试变空。
    唯一不需要 num_ctx 的是卸载请求（`keep_alive=0`）：卸载按模型名整个卸，
    不挑 runner。
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
24. **显示缩放（高DPI）：Qt6 起由 Qt 全权负责，我们那套手工缩放已经失效但留着。**
    ⚠️ 本条 2026-09-20 迁到 PyQt6 后改写过，原文写的是"没有全局开 Qt 缩放、
    不要顺手去开那个开关"——**Qt6 的 HiDPI 关不掉，那个取舍已经不存在了**。

    本项目所有字号都是像素单位（`setPixelSize` / 样式表 `font-size: Npx`），
    Qt5 时代 `AA_EnableHighDpiScaling` 默认关闭，笔记本几乎都是 125%/150%
    缩放，直接跑字会小三分之一；于是有了
    `window_geometry.screen_scale_factor()`：读主屏逻辑 DPI，**只在首次运行**
    （还没有 window_state.json）按倍率放大默认字号和默认窗口尺寸，用户
    Ctrl+滚轮调过之后一律以保存值为准。设置面板自身字体同一倍率
    （`panel_font_px`，100% 下 17px）。悬浮窗按钮条等 chrome 的字号没跟。

    Qt6 下这套**自动变成了 no-op 且这是对的**：缩放全进 devicePixelRatio，
    `logicalDotsPerInch()` 被钉在基线，于是 `screen_scale_factor()` 恒为 1.0，
    放大那份改由 Qt 出，总倍率不变。函数留着当兜底（别的平台/未来的 Qt 真报
    了非基线 logicalDPI 时它仍给出合理值），成本为零。细节和实测见第 43 条。
    ☠️ 别"顺手修"成除以 devicePixelRatio——那会把该有的 1.0 改成 0.67。
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
    torch/PyQt6 整条链拉起来、绕开单实例 Mutex），所以数量远多于普通项目。
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
    没有 Qt 环境时，本机 `pip install PyQt6` + `QT_QPA_PLATFORM=offscreen`
    就能复现（torch 只要能 import，临时放个空的 `torch.py` 在 PYTHONPATH 里
    即可，不用真下几百 MB）。

    **第三种成因：Qt 虚函数重写里抛异常**（2026-08-28 加 🎞 影院条时踩的）。
    `CinemaBar.changeEvent` 里写了 `QEvent.ScreenChangeInternal` —— 那是 Qt 的
    **内部**枚举，PyQt6 根本没暴露（`dir(QEvent)` 里一个带 screen 的都没有）。
    窗口一构造 `changeEvent` 就被调用 → AttributeError → **PyQt6 对虚函数重写
    里的未捕获异常是直接 `abort()` 整个进程**，不往上抛。现象和上面两种一模
    一样：跑到 90% 直接消失，无 FAILED、无 traceback、退出码 127。

    ☠️ **这一种上面那个"单文件 -x --tb=short"的区分办法不管用**——它根本不
    产生 traceback，单独构造那个窗口同样是秒退无输出。只能逐行 `print(flush=True)`
    二分到具体哪一句。教训：**在 Qt 虚函数重写（`changeEvent`/`eventFilter`/
    `showEvent`/`paintEvent`…）里，任何属性访问都要先确认它在 PyQt6 里真存在**。
    `test_ui_code_never_references_nonexistent_qevent_members` 现在源码扫描
    `realtime_subtitle/ui/*.py` 里所有 `QEvent.X`，写下去就红——用源码扫描而不是
    行为测试，是因为行为测试要真触发那条分支才炸，而它平时不触发。

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

    **☠️ 校验通过之后地址会被钉成 IP 字面量**（2026-08-13 补）。只校验不钉的话
    这道闸门**只在启动那一刻关了一次**：`requests` 是每个请求都重新解析主机名的，
    所以配置里写主机名时，DNS 记录（或 hosts 文件）中途改指向外网就能让转录
    静默出去，而屏幕上和日志里照样什么都看不出来。现在
    `_assert_local_ollama()` 会把解析结果（优先 IPv4）写进模块级
    `_pinned_ollama_url`，**所有请求一律走 `ollama_url()`**。
    加新的 Ollama 调用路径时别再写 `config.OLLAMA_BASE_URL`——那等于绕过闸门。
    顺带的好处：`localhost` 会被直接钉成 `127.0.0.1`，第 22 条那 2 秒 IPv6 税
    对写错配置的人也自动消失了（`_warn_if_ipv6_first_host` 从此只在
    `ALLOW_REMOTE_OLLAMA = True`、不钉地址的场景下才可能触发）。

    同一条链路上还有第三道：`_ollama_identity_ok()` 在每次翻译前确认 11434
    后面确实是 Ollama（响应体里有 version 字段）。以前只在启动时查一次，
    而 Ollama 是会自动更新并重启的（第 6 条），重启窗口期里端口是空的、
    本机任何进程都能补位。加新的 Ollama 调用路径时不用自己重复这套，
    但也别绕过 `_translate_single_sentence` 直接发转录。
    ☠️ 但要清楚它的**强度只到"端口被无关程序误占"**：判据是响应体里有
    `version` 字段，三行代码就能伪装。别把它当成能挡住本机恶意进程的安全
    边界（本机恶意进程本来也能读你的内存），它是 typo 防护，不是隔离。

30. **`numpy` 下限必须停在 2.2，不要跟 dependabot 抬到最新。** 2026-08-12
    审计时 `requirements.txt` 写的是 `numpy>=2.5.1`，而 numpy 2.5 起
    `requires_python >=3.12`、连 cp310 轮子都没有。本机只有 3.13 所以装得上，
    但 `install.ps1` / README 仍接受 3.10–3.13——3.10 上 `pip install -r
    requirements.txt` 会直接失败。版本门槛：2.2.x ≥3.10，2.3/2.4 ≥3.11，
    2.5+ ≥3.12。3.12/3.13 会自己漂到 2.5.x，不用把下限抬上去。
    同一轮评估过 `--generate-hashes` 锁文件：一份锁钉不死 3.10–3.13 的
    wheel 矩阵，再叠加 CPU 档要滤掉 `nvidia-*` 行，hash 校验会碎。放弃，
    理由写在 `requirements.txt` 文件头。

    **这件事现在有 CI 守卫**：`deps-resolve` job 拿 `pip install --dry-run
    --python-version X --platform win_amd64` 在 3.10/3.11/3.12/3.13 上各解析
    一遍 `requirements.txt`，任一失败就红。它存在的理由是——出事的时候 CI
    是**全绿**的：另外几个 job 装的都是自己写死的 `numpy>=1.24,<3` 之类，
    从来没有任何一个 job 装过 `requirements.txt` 本身。改依赖钉法时先在本地
    跑一遍那段循环，别等 PR 变红。

31. **☠️ 自动语言切换（`AUTO_DETECT_LANGUAGE`）默认关，别顺手打开。** 它能用
    （2026-08-12 用 Windows TTS 造中/德音频端到端验过：de(1.00)×3 → 切中→德，
    zh 之后再讲德语 → 切回德→中，两个方向都对），但有两笔固定成本：

    - **换语言时有一段垃圾字幕**。滞回要攒够 `LANGUAGE_SWITCH_STREAK` 次检测
      才切，那段时间新语言的音频还在用旧语言参数解码。实测德语切回中文的
      18 秒窗口里字幕长这样：`China und China, China, China...` / `和国.和国.`。
      窗口 = `STREAK × LANGUAGE_DETECT_INTERVAL`，默认 3×4=12 秒。
      单次检测实测 **180ms**（几乎不随缓冲长度变——30 秒 pad_or_trim，第 20 条），
      所以缩短间隔是最划算的旋钮：3 秒 → 窗口 9 秒、额外 GPU 约 6%。
    - **误切的代价远大于晚切**。切换会 `clear_context()` 丢掉识别缓冲，而新
      语言还会经 `initial_prompt` 自我强化（第 4 节记着"英文被误认能锁死近
      3 分钟"）。所以 `LanguageVote` 的判据一律往保守调：置信度不够不算数、
      不在 `LANGUAGE_PAIRS` 里的语言不算数、中间断一次连击清零、切完 20 秒
      冷却。用户抱怨"乱切"时**先调大 STREAK**，那个最有效。

    还没做但想过的：一旦投票开始攒连击就暂停出字幕，把垃圾窗口变成空白窗口。
    好处是不再显示废话、也不浪费 Ollama；坏处是投票没走完的话会白丢几秒真字幕。
    要做之前先想清楚这个取舍。

32. **中文/日文这类无空格语言在管线里是"另一套规则"，不是"多一个语言码"。**
    加中→德时踩到的（每一条都是实测）：
    - 切句：全角 `。！？` 不在 `[.!?…]` 里，且原正则的 `(?=\s|$)` 对中文
      永远不成立 → **一句都切不出来**。半角那半条仍要求空白，否则"3.5 元"会断。
    - 残句兜底 `MAX_PENDING_WORDS` 用 `.split()` 数词，中文整段恒等于 1 →
      第二道闸同样是死的，中文只能靠 `IDLE_FLUSH_SEC` 冲。改用 `MAX_PENDING_CHARS`。
    - `_boundary_is_real` 那三条否决（德语缩写/日期序数/"下一个词小写=没说完"）
      对中文一条都不成立；`islower()` 对汉字恒为 False，等于那条规则一直在瞎蒙对。
    - faster-whisper 的词级时间戳给每个词都带前导空格，拉丁语系需要它，中文
      照抄就成了「另外,软件方面的 更 新同 样值得 关注。」。`_ts_words` 按语言剥。
    - 拼接残句/合并批次时的 `" ".join` 同理，无空格语言要用空串。
    统一入口是 `translator_queue._no_space_language()`，语言集合在
    `config.NO_SPACE_LANGUAGES`。加韩语/泰语往那里补，别再散着判。

    **☠️ 2026-08-13 补：当时这条只改了一半，另外五处漏到第二轮才发现。**
    症状全都是"单看每一处都对，只有把源语言换成 zh 再走一遍才暴露"：
    - `_maybe_draft` 的 `len(text.split()) < DRAFT_MIN_WORDS` → `1 < 3` 永真，
      **草稿翻译对中文一次都没触发过**。已改走 `_draft_too_short()`。
    - `_live_text` 的 `" ".join` → 中文 live 行插空格（`_append_committed`
      改了、它没改，同一句话在 live 行和历史行里长得不一样）。
    - `_prompt_language_mismatch` 对 zh 直接 `return False` → 中文源语言下
      "上下文被错误语言污染就弃用"整道保护是关着的，正是第 31 条那个
      `China und China` 现场该起作用的地方。
    - `HALLUCINATION_BLACKLIST` 一条中文都没有，且 60 字符的长度门对中文
      等于放宽 2.5 倍。已另设 `HALLUCINATION_MAX_CHARS_CJK`。
    - `TRANSLATE_BATCH_MAX_CHARS=300` 同理——**字符不是等价单位**，300 个
      汉字 ≈ 750 个德语字符的信息量。已另设 `TRANSLATE_BATCH_MAX_CHARS_CJK`。

    **规律**：凡是出现 `.split()` / `" ".join` / 按字符数定的阈值，都要问一句
    "换成中文还成立吗"。反过来，`_squash_repeats` 是唯一一个"代码是死的但
    没死出后果"的，别顺手改（理由见第 20 条）。

34. **☠️ `transcripts/` 和 `downloads/` 是私有数据，内容一个字都不进仓库。**
    这份存档抓的是**系统全部声音**（可能含语音通话），config.py 里已经反复
    警告过它的敏感性——但一直没人写下"所以它也不能进 commit"，而第 2 节又
    明确鼓励 AI 助手直接读改本机文件。2026-08-13 就是这么踩的：为了给幻觉
    黑名单和长度门找依据去 grep 存档，然后顺手把几条原文当测试 fixture 写进
    了 `tests/`、代码注释和提交信息，推送时才被拦下来。

    界限很简单：
    - **可以**用它做统计来支撑决策——条数、长度分布、词频、n-gram 命中率。
      注释里写聚合数字是好事（本文件到处都是这种实测依据）。
    - **不可以**把原文抄进任何会被推上去的地方：测试样本、代码注释、
      提交信息、PR 描述、issue。要中文/德文测试样本就自己现写句子。
    - 注明数据来源时写"开发机本地存档实测（315 条：p50 11 字…）"，
      不写样本内容。

    `downloads/` 还可能包含用户下载的完整视频、原文字幕、双语字幕和学习笔记，
    同样不能提交或复制到测试夹具。不同于 `transcripts/`，它不一定含系统其他声音，
    但仍属于用户个人学习资料，默认只留在本机。

    同一类还有 `subtitle.log`（`SHOW_PERFORMANCE=True` 时会打识别原文和译文）
    和 `lookup_cache.json`。贴 issue 前扫一眼，README 的 FAQ 让你贴日志尾部，
    那也是同一个坑。

33. **`GLOSSARY` 和感叹词表是「德→中」的，两端都要判。** 感叹词表原来只判
    `SOURCE_LANGUAGE != "de"`，漏了目标语言那一半——中→德时 "Ja." 会命中词典
    把"是"直接上屏，而这次要的是德语输出。同理点词查词：中→德时德语在**译文
    行**上，按 `SOURCE_LANGUAGE`(zh) 去查会让 prompt 变成"你是中文汉词典。
    简明解释中文单词 Kameraqualität"。现在按被点词的字符集判（`lookup_language_for`）。

35. **☠️ 语言这件事有两条"单一入口"，都别绕。**（2026-09-10 审核 B01/B03）
    - **配置怎么解析**：`realtime_subtitle/language_policy.py`。UI 面板和识别
      线程都问它。以前两边各写一份，同一份 config_local.py（`LANGUAGE_PAIRS`
      为空 + 老的 `LANGUAGE_CYCLE`）下后台有两个语言对、面板一个按钮都没有，
      恢复上次选的源语言时 allowed 集合也是空的——**用户从面板改不动语言，
      而且没有任何报错**。它不许 import Qt/requests/translator_queue
      （面板要在 QApplication 之前读它，识别线程也不能因此把 PyQt6 拉进
      导入链，见第 1 条），`test_language_policy.py` 用 AST 扫 import 盯着。
    - **切换请求走哪条路**：一律 `request_switch_language`，
      "要不要真切"只能在 `_apply_pending_lang_switch` 里判。
      ☠️ **不要在 UI 层写 `if src == config.SOURCE_LANGUAGE: return`**：
      config 只在后台的 ASR 批边界之后才变，「德语→点英语→再点德语」里
      第二次点击那一刻 config 还是 de，于是被当成"没变化"扔掉，最终停在
      英语——用户最后一次选择静默失效。语言和来源（manual/auto/rescue）
      是一个请求整体，在同一把 `_asr_lock` 内一次写完、一次取完清空——
      ☠️ `_apply_pending_lang_switch` 跑在锁外且很慢（clear_context），
      **它不许读写 `_pending_lang_source`**：这期间入队的是下一条请求，
      在那里清一次等于清掉别人的来源（一条 auto 被消费成 manual）；
      给那行加锁也不对，它本来就没资格动别人的请求。
      `_apply_pending_lang_switch` 的返回值是"真切了吗"，只有真切了才允许
      丢掉那一批切换前的音频。`test_language_switch_requests.py` 盯着这些。

36. **☠️ 译文内容永远不是控制信号，它最多只能产出"建议人工复核"。**
    （2026-09-10 审核 B02，两轮才修对）

    `offline.py` 第一版判据是子串黑名单（"法律法规"/"政治敏感"/"无法完成"/
    "不能生成"）：一句完全合法的译文「我们必须遵守法律法规。」被判成模型拒答
    → 重试 → 换模型 → 仍然失败，**整段视频停在那一条上**，而模型一直在
    正常干活。

    第二版换成"锚在开头的整句拒答模式 + 长度上限"，**仍然是错的**：
    原文 "Es tut mir leid, ich kann dir nicht helfen." 的正确译文就是
    「抱歉，我不能帮你。」，和模型拒答的字面**可以完全相同**，改成 fullmatch
    也消除不了这个歧义。日常对白里这种句子很常见。

    现在 `_looks_like_refusal()` 只用来给那一行打 `needs_review` 标记 + 打一行
    提示，**不参与失败判定、不触发重试、不换模型**。失败判据全部是结构化的：
    空响应 / 服务 error / `done=False` / 被 `length` 截断（都在
    `_ollama_request` 里）。取舍是明写的：**宁可留一条可能是拒答的怪译文，
    也不能让整片失败**。别再加更大的黑名单，也别引入第二个模型来判拒答——
    代价和收益完全不对等。`tests/test_offline.py` 的参数化用例盯着这五组。

37. **☠️ 离线缓存的身份必须描述"产生它的那次请求"。**（2026-09-10 审核 B04/B05）
    - **ASR 指纹按"用户请求的模式"算，不按识别结果算**。`auto` 那次传给
      Whisper 的是 `language=None`、没有 seed prompt；强制 `de` 那次传的是
      `language="de"` + de 的 seed prompt——**两次是不同的解码**。以前拿检测
      结果当指纹，等于让 auto 的产物冒充"用过 de 参数"，用户明确指定 de 之后
      拿到的还是上次自动识别的老结果。checkpoint 里 `requested_source_language`
      和 `detected_source_language` 分开存，谁也不许冒充谁。
    - **checkpoint schema 是 v2**；v1 没有 requested 字段，无从判断能不能复用，
      一律作废重识别一次。只作废缓存，不动已下载的媒体。
    - **坏缓存要作废，不许悄悄夹紧时间把它"修好"**。`load_checkpoint` 现在
      要求每条 `0 ≤ start < end` 且起点不倒退——以前只查"是不是有限数"，
      于是 start=9/end=-1 照收，导出的 SRT 是反向时间轴（播放器里字幕整段
      消失，日志里一切正常）。☠️ 但**不要禁止字幕重叠**：上一条没消失下一条
      已开始是合理的，一律禁止会把好缓存判成坏的。
    - **翻译缓存按 (source,target) 分开存**（checkpoint 的 `translations` 表），
      ASR 结果跨目标共享。切目标语言不重跑 ASR，切回去也不重翻。
      **字幕模式不进任何缓存身份**——它只是渲染，双语切单语不许触发模型。
      ☠️ `translate_segments.persist()` 每次落盘必须**同时**更新 `rows` 和当前
      语言的 `translations[src-tgt]`。只写 rows 的话，磁盘上"行里有译文、表里
      是空的"，下次 `restore_translations` 以空表为准把已翻好的行清掉重翻——
      **断点等于白存，而且只在失败/中断路径上暴露**，正常跑完那次看不出来。
    - **只跑这次任务需要的阶段**：`subtitle_mode="source"` 不调翻译服务
      （用户只要原文，不该被 Ollama 挂掉阻断）；没有译文就不写 target/bilingual
      那两份，返回值里也不指向不存在的文件。`source + summary` 会尝试翻译
      （笔记要原文译文成对），但翻译失败只跳过笔记，不撤销已完成的原文字幕。
    - 导出名带语言标签（`{id}.{src}-{tgt}.bilingual.srt` 等），英文和德文成果
      各存各的。三种模式每次一起导出，用户改主意不必重跑。
      ⚠️ 这次改名之后，`downloads/` 里旧的 `<id>_bilingual.srt` 不会再被更新，
      也不会被删——旧文件留在原地，新的用新名字。

38. **☠️ 离线管线分成"导入"和"处理"两层，边界不许漏。**（2026-09-10 批次 3）
    - `plan_media()` / `resolve_media()` 是**唯一碰网站的地方**：yt-dlp、登录、
      限流重试、平台规则全在这一层。`process_media()` 拿到的是一份已经在本地的
      `MediaSource`，之后 ASR / 翻译 / 导出里**不许出现"如果是小红书就……"**。
      本地文件导入走的就是同一条 `process_media`，只是不下载。
    - **本地文件不伪装成网站链接**：`extractor="local"`，身份是**整个文件内容**
      的 SHA-256（`local_media_fingerprint`），不是文件名。文件**就地引用，
      不复制不改名**。
      ☠️ 别改回采样（只读首尾各 1MB）——那正是 v1 的 bug：1–2MiB 的文件后半段
      根本没参与哈希，更大的文件中间整段没参与，保持大小改盲区字节就得到同一个
      ID，于是**内容变了却复用旧字幕**。代价是真要读一遍文件（大文件是秒级），
      比静默给错字幕便宜得多。指纹带 `v2_` 前缀，v1 的采样 ID 不会被继承。
      也别拿 mtime 顶替内容校验：复制/解压会改它，覆盖写可能不改。
    - **来源路径不参与本地身份判定**：`checkpoint_asr_usable` 对本地任务不比
      `source_url`（a.mp4 改名 b.mp4 不是另一个视频，不该重跑 ASR、更不该
      连带丢掉多目标翻译表）。`origin` 只是"从哪来的"，会被更新。
    - **元信息里的"未知"不等于"没有"**：`acodec` 缺字段或为 `None` 是未知，
      只有**每一路** format 都给出明确无音频证据才在下载前判成图文帖；有一个
      未知就留给下载后的 ffprobe。`f.get("acodec") or "none"` 这种写法会把
      三态压成两态，正常视频会被误杀。
    - **下载回来的身份必须和上锁时的身份一致**：传了 `info` 只省掉
      `download_video` 里那次显式只读解析，下载本身仍会解析，返回的 id/extractor
      可能不同。不校验就会"锁着 A 目录、往 B 目录写"（B 不存在时是裸的
      FileNotFoundError，存在时更糟——没持它的锁就动它）。不一致就带原因停下。
    - ☠️ **下载必须在任务锁之内**。锁的位置取决于任务目录，任务目录又要先识别
      才知道，所以顺序是：冻结选项 → 只读识别（`plan_media`，不下载）→ 上锁 →
      `plan.fetch()` 下载 → `process_media(..., lock_held=True)`。整个任务只上
      一次锁，中途不放开。重构时最容易把下载挪到锁外面，
      `test_download_happens_inside_the_job_lock` 盯着这件事。
    - **导入失败绝不能走到 ASR**：合集/播放列表、图文帖（所有 format 的
      `acodec` 都是 none）、没有音轨的文件，都在导入阶段带原因失败。
    - `TaskOptions` 是 frozen 的：这些值会进缓存指纹和导出名，中途被改一下就会
      出现"指纹按 A 算、文件按 B 写"的错位。

39. **☠️ 分享文案里抠链接：`https?://\S+` 是错的。**（2026-09-10 批次 4）
    中文标点**不是空白字符**，所以
    「打开【小红书】App查看！ http://xhslink.com/a/AbC123，快去看」
    会被抠成 `http://xhslink.com/a/AbC123，快去看`——整句后半段跟着进了 URL。
    规则只有一份：`offline.extract_share_urls()`，按"URL 合法字符"正向匹配，
    撞到 CJK/全角标点就停；PowerShell 通过
    `download_subtitle.py <文案> --list-urls` 调它，**别在 .ps1 里再写一套正则**。
    两个必须保住的细节：查询串里的 `xsec_token=...` 要完整保留（分享链接的
    一部分，掉了就打不开）；URL 自带的成对括号不能当句末标点剥掉。

    **本机装的 yt-dlp 对小红书的支持范围**（`XiaoHongShuIE._VALID_URL`，
    2026.08.19）：只认 `www.xiaohongshu.com/explore/<id>` 和
    `/discovery/item/<id>`。**短链 `xhslink.com` 不在里面**，要靠 generic
    extractor 跟跳转——这一条**没有真实链接验证过**，别对外说"支持小红书"。
    `test_offline_platform.py` 把这个支持范围钉住了，改 yt-dlp 版本后它会提醒你。

40. **平台失败要给出路，不要甩英文栈。**（2026-09-10 批次 4）
    登录/私密/地区/限流/不支持/失效这几类都翻成中文并**指向本地导入**——
    本地文件不联网、不需要登录，是权限问题唯一确定可用的出路。原始英文报错
    仍然附在后面（贴 issue 要用）。加新的失败类型往 `_DOWNLOAD_HINTS` 里加，
    别在调用点各写各的。

41. **☠️ 实时链路里源语言和目标语言是两个独立的值。**（2026-09-10 批次 5，审核 F03）
    `LANGUAGE_PAIRS` 允许同一个源语言配多条（`zh→en` 和 `zh→de`），所以：
    - **面板按钮的键是 `(source, target)` 整对**，不是源语言。按源语言索引时
      后一个按钮会把前一个从字典里挤掉，面板上只剩一个、而且切不了目标语言。
    - **切换请求带完整的 `(source, target, origin)`**；只传源语言的话后台只能
      `target_for()` 查到第一条，第二个按钮永远切不上。
    - **tuning 两个键都要存**（`SOURCE_LANGUAGE` + `TARGET_LANGUAGE`），恢复时
      按整对校验；配置改过、这一对不存在了才退回该源语言的默认目标。

    **自动检测只改源语言，不覆盖用户显式选过的目标**（`language_policy.resolve_target`）。
    "是不是显式的"不另外存状态位——存了很容易和 config 走散——判据就是
    "和 `target_for(source)` 的默认值不一样"。但显式目标也得讲得通：中文下
    选了德语、随后自动检测切到德语时，保持德语就成了德→德（模型把原句抄
    一遍，见 `_apply_pending_lang_switch`），这种情况退回默认。

42. **☠️ 跨语言的旧事件要在**最后消费的地方**拦，不能在发出前拦。**
    （2026-09-10 批次 5，审核 O04）
    翻译 worker 是「拿 `_tx_lock` 查代数 → 放锁 → 回调 UI」。查完到真正回调
    之间锁是放开的，中间可以插进一次语言切换，于是旧语言的句对落在新语言的
    画面上。发出前再查一次代数**关不掉这个窗口**——判据必须跟着事件走。
    现在 `_lang_revision` 只在真正生效的切换时 +1，随 `on_pair`/`on_draft`
    一起发出（`_emit_pair`/`_emit_draft`），Qt 槽 `_add_pair`/`_update_draft`
    用 `_is_stale_language()` 拒收代数更旧的。
    靠的是 **Qt 队列连接保序**：同一线程发出的事件按发出顺序到主线程，所以
    "语言已切换"先到、旧句对随后到 → 被拒；反过来（切换前发出的最后一条
    旧语言字幕先到）应当照常显示，别矫枉过正。加新的 UI 事件时一起带上代数。

43. **☠️ HiDPI：会在 Qt6 下坏掉的是存档，不是缩放公式、更不是命中测试。**
    （2026-09-20 查证，PyQt6 迁移第 2 步）

    第 17/24 条读起来像是"一旦开了缩放，手算像素和 WM_NCHITTEST 全要重做"。
    逐个查过之后**两件都不成立**：

    - `screen_scale_factor()` 在 Qt6 下**自己就归 1.0**，不会双重缩放。
      Qt 接管缩放后把 `logicalDotsPerInch` 报在基线不动，缩放那份由 Qt 出，
      总倍率不变。**别"顺手修"成除以 devicePixelRatio**——那会把该有的 1.0
      改成 0.67。`tests/test_hidpi_scaling.py` 钉住了这条不变式。
      ⚠️ 钉的是不变式不是数字：**基线 logicalDPI 并非到处都是 96**（本机 96，
      GitHub 的 Windows runner 是 100），写死数字会让 CI 在没人改代码的日子
      变红——第一版就是这么红的。同理别断言 `DPR == 1.0`，那是"开发机屏幕
      是 100%"，不是本项目的性质。

      ☠️ **第 2 步写这条时机器上还只有 PyQt5，上面是拿「PyQt5 + 打开
      AA_EnableHighDpiScaling」模拟出来的推断。** 第 3 步真装上 PyQt6 之后
      重量了一遍，结论成立，而且比推断更强（PyQt6 6.11 / Qt 6.11）：

      | 环境 | logicalDPI | DPR | `screen_scale_factor()` |
      |---|---|---|---|
      | 无环境变量 | 96 | 1.0 | 1.00 |
      | `QT_SCALE_FACTOR=1.5` | 96 | 1.5 | 1.00 |
      | `QT_FONT_DPI=144` | 96 | 1.5 | 1.00 |

      **连 `QT_FONT_DPI` 都落进 DPR**——Qt5 时代它是进 logicalDPI 的。也就是
      Qt6/Windows 上根本没有让 logicalDPI 偏离基线的路子，`screen_scale_factor()`
      实质上恒等于 1.0。它现在是个留着兜底的 no-op，不是活逻辑（第 24 条）。
    - `nativeEvent` 的命中测试**与 Qt 坐标空间无关**：坐标来自 `lParam`、
      窗口矩形来自 `GetWindowRect`，两边都是 Win32 屏幕物理像素，全程没有
      一个数来自 Qt。唯一副作用是 `RESIZE_MARGIN`/`BTN_RESERVE` 是物理像素
      常量，高缩放屏上手感偏窄——调参问题，且 Qt5 下本来就如此。

    真正会出事的是 `window_state.json`：Qt5 存的是**物理**像素，Qt6 会把同样
    的数字当**逻辑**像素用。150% 的笔记本上窗口和字一起涨 50%，而用户完全
    不知道发生了什么（日志里没有任何异常）。所以存档现在记 `coord_dpr`，
    加载时和当前 DPR 比对并按比例换算（`window_geometry.rescale_state_for_dpr`）。
    ☠️ 换算用**显式键名单**，别遍历整个 dict：`tuning` 里绝大多数值
    （`ENERGY_THRESHOLD_SPEECH`、`CHUNK_SUBMIT_SECONDS`…）不是像素，一起乘会
    把用户的调参毁掉。x/y 可以是负数（多屏时左边/上方那块屏），不能和尺寸
    一样 `max(1, …)`。新增像素单位的持久化项时记得加进那份名单。

44. **☠️ 本项目只支持 PyQt6，不要再写 PyQt5。**（2026-09-20 迁移第 3 步落地）

    三步走到此结束：第 1 步全限定枚举 + `exec()`（`aa5ebc6`），第 2 步 HiDPI
    与存档坐标空间（`340c0d3`/`2bb97f2`），第 3 步翻 import 并第一次真把
    PyQt6 装进来。`requirements.txt` 钉 `PyQt6>=6.6.0,<7.0.0`，验证用的是
    6.11.0 / Qt 6.11.2。

    **翻 import 之外真正要动手的只有四处**（其余 56 处 `from PyQt5` 是纯文本
    替换，源码 28 / 测试 28）：

    | 位置 | Qt6 的变化 |
    |---|---|
    | `ui/tv_window.py` | `QShortcut` 从 QtWidgets 搬到 QtGui（`QAction` 同理，本项目没用到） |
    | `ui/window_frame.py` | `QMouseEvent.globalPos()/x()/y()` **被删**，只剩 QPointF 版 `globalPosition()`；`pos()` 侥幸还在 |
    | `tests/test_hidpi_scaling.py` | `AA_EnableHighDpiScaling` 连枚举成员都没了，原来那套模拟失去前提，改成直接量真 Qt6 |
    | `deps_fingerprint` / CI / install.ps1 | 包名 `pyqt5-sip` → `pyqt6-sip`、钉版、自检 import |

    ☠️ **`globalPosition()` 返回 QPointF，必须 `.toPoint()`**。高缩放屏上它带
    小数，而 `frameGeometry()`/`move()`/`manhattanLength()` 一路都是整数 QPoint
    的世界，混着算会静默退化成浮点坐标——不报错，只是拖窗慢慢飘。

    ☠️ **第 1 步那道枚举守卫有个洞，是这次才兑现的**：它只看 `Q…` 开头的前缀，
    **实例上取短枚举名一个都抓不到**。`popups.py` 里的
    `cursor.movePosition(cursor.End)`（`cursor` 不以 Q 开头）就这么一路绿着过了
    整个第 1 步，换上 PyQt6 当场 7 个用例一起红。
    `test_no_short_enums_on_instances` 补上了这一类——判据比第一道松（只认成员
    名，不知道 `cursor` 是什么类型），会有理论上的误报，真撞上时改名或留一条
    带理由的豁免，别删用例。

    **老用户怎么升级**：`requirements.txt` 变了 → 更新脚本会装上 PyQt6，但
    `pip install -r` 从不卸载东西，PyQt5 会一直留在 venv 里（见第 30 条上面
    那段注释）。清掉它：更新时加 `-Prune`，或
    `venv\Scripts\python scripts\prune_venv.py --yes`——本机实测正好识别出
    PyQt5 / PyQt5-Qt5 / PyQt5_sip 三个，没误伤别的包。留着也不影响运行，
    只是白占 100 多 MB。

    **这次验到哪一步**（别把没验的当验过了）：544 项 pytest 全绿、ruff 全绿、
    三个独立 GUI 脚本（含 WM_NCHITTEST 那套）退出码全 0、install.ps1 的自检
    代码走通——而且**以上都是在 venv 里已经彻底没有 PyQt5 的前提下跑的**。
    真机也起过一次完整程序：悬浮窗置顶/半透明/几何恢复正常，德语识别 +
    中文翻译双语上屏，`.stop` 优雅退出并卸载了 Ollama 模型。
    ⚠️ **没验的**：没在真正的高缩放屏（125%/150%）上跑过一次，第 43 条那张表
    是 `QT_SCALE_FACTOR` 模拟的；`rescale_state_for_dpr` 对老存档的换算也只有
    单元测试——真机上走到的一直是 `saved_dpr == current_dpr` 的短路分支，
    **换算代码在生产环境里一次都没执行过**。怎么验写在
    [docs/TODO.md](docs/TODO.md) 第 1 条，验完回来把这段删掉。

## 5. 目录地图

```
main.py               入口：接线各模块、热键注册、单实例守卫（import 顺序敏感！）
download_subtitle.py  离线视频处理入口：下载 → 识别 → 双语 SRT → 学习笔记
realtime_subtitle/paths.py    运行时文件落点的唯一真相源（REPO_ROOT/repo_path，见第4节第28条）
realtime_subtitle/language_policy.py  语言对配置的唯一解析入口（UI+识别线程共用，零 Qt 依赖，见第4节第35条）
realtime_subtitle/offline.py  离线批处理实现；导入层(plan_media/resolve_media，唯一碰网站)
                      与处理层(process_media，只认本地媒体)分开，见第4节第38条
realtime_subtitle/capture/audio_capture.py      WASAPI Loopback 采集 + 设备热切换
realtime_subtitle/asr/streaming_asr.py      local agreement 增量识别（词级前缀提交）
realtime_subtitle/translate/translator_queue.py   Whisper/Ollama 持有者：切句、翻译队列、草稿、术语表
realtime_subtitle/translate/lookup.py    点词查词 + 🤖AI分析（LookupMixin）
realtime_subtitle/translate/transcript.py     字幕存档 + 保留期清理（TranscriptMixin）
realtime_subtitle/translate/runtime_stats.py  分钟级性能概况（StatsMixin）
                      ☠️ 这三个 mixin 都不自己 __init__，字段由
                      WhisperQueueTranslator.__init__ 建；契约写在各自模块 docstring 里
realtime_subtitle/ui/subtitle_window.py    悬浮窗主类（+ window_frame/window_chrome/subtitle_render/
                      window_geometry/settings_window/popups 拆分模块）
config.py             全部默认参数（仓库文件，别为单机改它）
config_local.py       本机覆盖（gitignore，install.ps1 生成，机器适配都写这）
scripts/windows/install.ps1  一键安装 + 硬件检测 + 桌面快捷方式（根目录 install.ps1 为兼容转发）
scripts/windows/download_subtitle.ps1  输入视频地址，调用离线批处理并保留窗口显示结果
scripts/windows/update_subtitles.ps1  一键更新（git pull + 按需装依赖）
scripts/windows/uninstall.ps1  卸载（逐项问 Y/N，默认不删）；-CleanCache 只清下载残文件
scripts/windows/{start,stop,pause}_subtitles.ps1  启动（PID 管理/Ollama 保活）/停止/暂停
scripts/windows/start_and_update_subtitles.ps1  纯编排：update →（代码真变了才）stop → start；
                      三步都开子进程跑，理由（exit 语义 + pull 换掉脚本自身）写在文件头
tests/                所有 pytest 用例（三个独立 GUI 脚本套件见第 4 节第 15 条）
requirements-dev.txt  测试依赖（pytest），只有改代码的人要装
transcripts/          字幕存档（每天一个文件）
downloads/            离线视频、原文/双语 SRT 和学习笔记（gitignore，可能很大）
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
