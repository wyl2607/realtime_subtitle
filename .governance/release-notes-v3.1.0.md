v3.1.0：支持 Apple Silicon Mac；新增「算力节点」，可以把识别交给家里另一台电脑

给用户带来什么

- **Windows 用户：什么都不用做。** 功能和依赖都没变（新增的依赖全部限定在
  macOS 上才装），双击「启动字幕.bat」照常更新、照常使用。
- **新增 macOS（Apple Silicon，M1 及以后）支持**：同一套桌面字幕程序能在 Mac 上跑。
  - 识别走 MLX（Apple GPU），按内存自动选档：16GB 机器默认 large-v3-turbo，
    内存紧张时自动降精度、恢复后再升回去。
  - 翻译优先用 macOS 自带的系统翻译：德→中实测比本地 9b 模型更准
    （chrF 33.8 对 29.8），单句约 0.12 秒，不占 Ollama 内存；系统翻译不可用时
    自动退回 Ollama。要先在「系统设置 → 通用 → 语言与地区 → 翻译语言」里
    下载德语和中文（简体）。
  - 抓系统声音：BlackHole 虚拟声卡，或 macOS 14.2+ 的 Process Tap。
  - 全局快捷键与 Windows 相同（Control + Option）。
  - 装法：`bash scripts/macos/install.sh`，见 README「macOS」一节。
- **新增 rslite（Mac 原生极简字幕，实验性）**：系统识别 + 系统翻译的原生小程序，
  不需要 Python 和 Ollama，播放回放时整机功耗只比空闲多约 0.1W，适合不插电用。
  在仓库 `macos-native/` 下 `swift build -c release` 构建。
- **新增「算力节点」（实验性）**：把家里另一台 Apple Silicon Mac 或带 NVIDIA 显卡
  的 Windows 电脑装成节点，rslite 会自动选最快的那台来精修字幕：
  本机先出字幕，节点更准的结果随后替换上去；节点掉线时字幕不断、自动回到本机，
  节点恢复后自动接回。实测（同一段 67 秒德语）：
  - 纯本机 rslite：WER 7.9%
  - 混合档 + RTX 5060 Windows 节点：WER 0.9%（节点已热），识别速度约为实时的 50 倍
  - 节点中途掉线：立刻回到本机，约 12 秒后切到另一个节点，无重复、无丢句
  - 节点冷启动时第一句开头也不再丢失（会话建立前的音频先缓冲再补发）
  - 装法：Mac 节点 `bash scripts/node/install_node.sh <ssh 别名>`，
    Windows 节点 `bash scripts/node/install_node_windows.sh <ssh 别名>`（都在 Mac 上运行）。
  - 隐私边界：节点只监听 Tailscale 地址（100.64.0.0/10），token 鉴权、只经文件传递，
    音频只进内存不落盘，日志不写字幕正文。音频会离开本机、但只到你自己的节点机器。
  - Windows 桌面字幕程序接节点在下一个版本（v3.2.0）。

已知问题

- 节点精修结果比本机字幕晚约 4 秒到达（中位数）。
- rslite 和节点目前只在作者自己的设备上验证过（MacBook Air M2、Mac mini M4、
  RTX 5060 台式机）。
- 仍然没在高缩放屏（125%/150%）上验过 HiDPI 存档换算，见 docs/TODO.md 第 1 条。

升级

- Windows：双击「启动字幕.bat」即可，不需要任何手动操作。
- Mac：首次安装按 README「macOS」一节。从本版起 `start.sh` 启动前会自动更新
  （失败自动回滚，`--no-update` 可跳过）；已经装过的 Mac 需要先手动 `git pull` 一次。
