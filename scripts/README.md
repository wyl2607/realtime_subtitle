# 脚本

## Windows（`scripts/windows/`）

| 脚本 | 作用 |
|------|------|
| `install.ps1` | 一键安装（Python、venv、显存分档、Ollama、桌面快捷方式） |
| `download_subtitle.ps1` | 下载视频、生成双语 SRT、写学习笔记 |
| `start_subtitles.ps1` | 启动（单实例、等待 Ollama 就绪） |
| `stop_subtitles.ps1` | 先优雅退出，超时再强杀 |
| `pause_subtitles.ps1` | 暂停识别，不卸载模型 |
| `update_subtitles.ps1` | `git pull` + 同步依赖 |
| `uninstall.ps1` | 交互卸载（可选 `-CleanCache`） |

这些脚本会把仓库根目录解析成上两级，并在仓库根目录下运行。

根目录的 `install.ps1` 和 `download_subtitle.ps1` 是**兼容入口**，会转到本目录里的正式脚本。
