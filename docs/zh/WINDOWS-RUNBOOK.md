# Windows 操作清单

**中文** · [English](../en/WINDOWS-RUNBOOK.md) · [Deutsch](../de/WINDOWS-RUNBOOK.md)

给朋友或另一台 Windows 电脑用。仅支持 Windows——音频捕获走 WASAPI Loopback。

## A. 新电脑第一次安装

```powershell
# 1) 必须克隆到纯英文路径
cd C:\
git clone https://github.com/wyl2607/realtime_subtitle.git
cd C:\realtime_subtitle

# 2) 安装（venv、显存分档、Ollama、桌面快捷方式）
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
# 国内网络：
# powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 -Mirror
```

> **为什么必须纯英文路径？** 生成的桌面 `.bat` 会把这个路径写死进去，而 `chcp 65001`
> 下 cmd 解析含中文的行会把下一行开头吃掉，启动脚本直接损坏。中文 Windows 用户名
> （`C:\Users\张三\...`）最容易踩。`install.ps1` 开头会拦住并让你换路径。

没有 Ollama 的话，安装脚本问到时选 `Y`（用 winget 装）。首次启动还会自动下载
Whisper 模型（1-3GB），属于正常现象，不是卡住了。

桌面文件夹 **德语直播实时字幕** 里最后是五个入口：

| 文件 | 干什么 |
|------|--------|
| `启动字幕.bat` | **平时只需要这一个。** 先拉最新版，然后启动 |
| `YouTube下载加字幕.bat` | 给视频事后配字幕（也能直接拖本地文件进去） |
| `停止字幕.bat` | 停止 |
| `暂停继续字幕.bat` | 暂停/恢复（等同 `Ctrl+Alt+P`） |
| `卸载字幕.bat` | 卸载，逐项询问，默认都是不删 |

## B. 已有安装 —— 更新到最新版

```powershell
cd C:\realtime_subtitle   # 换成你的实际路径
powershell -ExecutionPolicy Bypass -File scripts\windows\update_subtitles.ps1

# 只需跑一次，为了拿到新的五个桌面入口：
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

`install.ps1` 可重复跑：不会清掉 `config_local.py`、字幕存档、`downloads/`。
它只会删掉**已经退役的**桌面入口（`启动并更新字幕.bat`、`更新字幕.bat`、
`下载并加字幕.bat`）——它们的功能已经并进 `启动字幕.bat` 了。

从 v3.0.0 之前的版本（PyQt5）升上来的话，老的 Qt 包会留在 venv 里，因为
`pip install -r` 从不卸载东西。留着不影响使用，只占磁盘：

```powershell
venv\Scripts\python scripts\prune_venv.py --yes
```

## C. 日常使用

| 动作 | 怎么做 |
|------|--------|
| 启动（每次先更新） | 桌面 **启动字幕.bat**；更新失败也照常启动，真拉到新版且字幕在跑才会自动重启 |
| 停止 | **停止字幕.bat** |
| 暂停 | **暂停继续字幕.bat** 或 `Ctrl+Alt+P` |
| 下载并加双语字幕 | **YouTube下载加字幕.bat**；结果在 `downloads\<视频ID>\` |
| 切换语言对 | `Ctrl+Alt+L` |
| 鼠标穿透 | `Ctrl+Alt+M` |
| 性能模式（把显卡让给游戏） | `Ctrl+Alt+G` |
| 影院字幕条 | `Ctrl+Alt+C`——视频已经全屏、按钮被盖住时必须用热键 |
| 只更新不启动 | `scripts\windows\update_subtitles.ps1`（故意不放桌面入口） |

手动启动（排错）：

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -u main.py
# 下载视频并加双语字幕：
venv\Scripts\python download_subtitle.py "https://www.youtube.com/watch?v=..."
```

日志：`subtitle.log`、`subtitle.err.log`、`logs\`。

## D. 更新后冒烟检查

```powershell
cd C:\realtime_subtitle
venv\Scripts\python -c "from realtime_subtitle import config, version_string; print(version_string(), config.OLLAMA_MODEL)"
venv\Scripts\python -c "import torch; from realtime_subtitle.translate import translator_queue; translator_queue._ensure_ml_deps(); print('SMOKE_OK')"
venv\Scripts\python -m pytest tests\test_pipeline_helpers.py -q
```

## E. 长期有效的几条

| 项目 | 说明 |
|------|------|
| `config_local.py` | 永远在**仓库根目录**，不在包里面。机器相关的配置都写这里——**别改 `config.py`**，那是仓库文件，改了每次更新都冲突 |
| 根目录的 `*.ps1` | 2026-08 之前装的人留下的兼容转发壳。**别删**——他们的老快捷方式指向这些文件，其中包括更新脚本本身；删了等于断了他们自救的路 |
| 三个独立 GUI 测试 | `tests\test_hittest.py`、`test_resize_freedom.py`、`test_wordclick.py` 会开真窗口，要手动跑，且需要 `PYTHONPATH=.` |
| Ollama | 独立安装的服务，会自动更新，更新期间端口会短暂消失 |

## F. 常见失败

| 现象 | 处理 |
|------|------|
| `ModuleNotFoundError: realtime_subtitle` | 在仓库根目录跑；重新 pull；venv 必须是这个克隆里的 |
| `ModuleNotFoundError: PyQt5` | 代码比 venv 新——让更新脚本重装一次依赖（v3.0.0 已经换成 PyQt6） |
| 旧笔记里 `import config` 失败 | 改成 `from realtime_subtitle import config` |
| 只有原文、没有译文 | Ollama 没运行或模型没拉：`ollama list` 对照 `config.OLLAMA_MODEL` |
| 装完第一次用，头一两分钟没有中文 | 翻译模型在装载，同时识别在消化积压；会自己缓过来 |
| 快捷方式提示找不到文件 | 重跑 `scripts\windows\install.ps1` 重新生成 `.bat` |
| 安装路径含中文 | 把克隆挪到例如 `C:\realtime_subtitle`，重跑安装脚本 |
| 抓不到声音 | 跟的是系统**默认播放设备**，换耳机约 5 秒内自动切换。要固定某个设备，在 ⚙ 面板「设备名包含」里填名字的一部分 |

---

更深的内容——硬件分档、调参、以及这个项目已经踩过的全部坑——在
[CLAUDE.md](../../CLAUDE.md) 里。
