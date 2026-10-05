# 中文说明

仓库首页就是中文完整文档，请直接阅读 **[README.md](README.md)**。

[English](README.en.md) · [Deutsch](README.de.md) · [Windows 操作清单](docs/zh/WINDOWS-RUNBOOK.md)

## macOS（Apple Silicon）

macOS 版仅支持 Apple Silicon（M1 及以后），Intel Mac 不支持。识别走 MLX（Apple GPU），翻译走本地 Ollama，音频与文本都在本机处理。完整说明见 [README.md 的 macOS（Apple Silicon）章节](README.md#macosapple-silicon)。

```bash
brew install uv ollama
git clone https://github.com/wyl2607/realtime_subtitle.git
cd realtime_subtitle
bash scripts/macos/install.sh
# 中国大陆网络加 --mirror；硬盘紧张加 --lean
bash scripts/macos/start.sh
bash scripts/macos/stop.sh
```

抓系统声音需要 `brew install blackhole-2ch`，再在「音频 MIDI 设置」创建「多输出设备」（扬声器 + BlackHole 2ch）并切换系统输出；不装时会退回默认麦克风并提示。快捷键同 Windows：`Ctrl+Alt+P/L/M/G/C`（物理 Control + Option 键）。
