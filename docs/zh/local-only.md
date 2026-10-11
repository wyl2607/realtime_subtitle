# 纯本机使用指南

本项目**默认完全本地运行**。只要不主动填写云端 Key、不配置算力节点，系统所有语音识别与翻译均在你的本机离线完成。

## 隐私承诺

> 默认情况下，识别与翻译均在完全本地运行，不向任何云端发送音频或文本。音频离开本机只有两条可选路径，且均须主动显式配置：
> 1. **自己的算力节点**：发送到你自己 Tailscale 内网中的机器（`100.64.0.0/10`），不经过第三方；
> 2. **云端 Groq（第三方）**：只有在程序配置里填了 Groq key 才会把音频发送到云端识别服务。不读环境变量（避免因系统变量意外开启上传）；可在设置面板或命令行一键关掉云端开关。

## 如何确认当前处于纯本机模式

- **悬浮窗状态行**：明确显示为「本机」（非「云端·Groq」或「混合·节点名」）。
- **Windows 客户端**：点击 ⚙️ 打开设置面板，在「识别来源」区域中确认：
  - 「使用算力节点」复选框为**关闭**；
  - 「云端识别」复选框为**关闭**；
  - 下方显示「当前路由：本机」。
- **macOS 原生版（rslite）**：默认就是仅本机。`settings.json` 的字段是 `source`、`src`、`dst`、`mode`、`pinnedNode`。缺 `mode`，或 `mode` 不是 `auto`、`local`、`hybrid` 时，按 `local`。`pinnedNode` 缺失、null、空白或类型不对，都视为不指定。
  - 菜单栏状态项（标题为「字·」加当前模式，例如「字·本机」）里的「设置…」（⌘,）打开「rslite 设置」。四个分页是「字幕样式」「云端识别」「识别与语言」「算力节点」。在「识别与语言」→「运行模式」确认为「仅本机（默认）」。
  - 命令行备选：`rslite --mode local`。只写 `--node ID` 而 mode 仍是 local 时不连节点。

## 如何从云端/节点退回纯本机模式

- **Windows 客户端**：
  1. 点击 ⚙️ 设置面板，在 Groq key 输入框旁点击「清除 key」按钮；
  2. 取消勾选「云端识别」复选框；
  3. 取消勾选「使用算力节点」复选框。
- **macOS 原生版（rslite）**：
  1. 菜单栏状态项里的「设置…」（⌘,）→「识别与语言」→「运行模式」选择「仅本机（默认）」，点「保存」。这一项写入 `mode` 为 `local`，并且不指定 `pinnedNode`。`mode` 为 `local` 时不启动节点路由，也不建立云端调度。
  2. 保存成功显示「已写入 settings.json。重启 rslite 后生效。」「识别与语言」页写着「重启 rslite 后生效」。
  3. 命令行备选：`rslite --mode local`。usage 原文是 `usage: rslite [--source tap|mic|file:PATH] [--src de-DE] [--dst zh-Hans] [--headless] [--mode auto|local|hybrid] [--node ID] [--selftest] [--set-groq-key|--clear-groq-key|--cloud on|off]`。`--mode` 只能是 auto、local 或 hybrid。只写 `--node ID` 而 mode 仍是 local 时不连节点。命令行上的 `--mode` 和 `--node` 只覆盖这一次启动，不写回 `settings.json`。

## 纯本机下的性能预期

纯本机性能完全取决于当前电脑的硬件配置：
- **Windows / NVIDIA 电脑**：参考 [CLAUDE.md §2 硬件适配与模型选择](../../CLAUDE.md#2-硬件适配与模型选择) 档位表。主流游戏卡（RTX 8GB+ 显存）下识别与单句翻译耗时通常在 1 秒以内；无独显或显存较小时会自动降级至 small 模型与 2b 翻译模型。
- **macOS（Apple Silicon）**：参考 [CLAUDE.md §7 macOS 档位与实测](../../CLAUDE.md#7-macosapple-silicon)。在 M2 16GB 实测中，MLX Whisper turbo 识别单次耗时约 2.4 秒（现有架构下限），Apple 系统翻译单句约 0.12 秒，Ollama 4b 翻译约 4.3 秒，整体字幕刷新节奏约为 2–3 秒一次。
