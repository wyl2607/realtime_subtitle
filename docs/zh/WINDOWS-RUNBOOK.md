# Windows 操作清单（装好之后）

给朋友或另一台 Windows 电脑用。仓库已经在 GitHub `master` 上。

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

如果没有 Ollama，安装脚本问到时选 `Y`（用 winget 装）。

桌面文件夹：**德语直播实时字幕**
- 启动字幕.bat / 下载并加字幕.bat / 停止 / 暂停继续 / 更新 / 卸载 / 操作说明.txt

## B. 已有安装 —— 更新到最新版

旧目录要 **先更新，再刷新快捷方式**。

```powershell
cd C:\realtime_subtitle   # 换成你的实际路径

git pull

powershell -ExecutionPolicy Bypass -File scripts\windows\update_subtitles.ps1

# 刷新桌面 .bat 路径（指向 scripts\windows\*.ps1）
powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1
```

`install.ps1` 可重复跑：不会清掉 `config_local.py`、字幕存档、`downloads/`。

然后：

1. **停止字幕.bat**（如果正在跑）
2. **启动字幕.bat**

## C. 日常使用

| 动作 | 怎么做 |
|------|--------|
| 启动 | 桌面 **启动字幕.bat** |
| 停止 | **停止字幕.bat** |
| 暂停 | **暂停继续字幕.bat** 或 `Ctrl+Alt+P` |
| 下载并加双语字幕 | **下载并加字幕.bat**；结果在 `downloads\<视频ID>\` |
| 影院字幕条 | `Ctrl+Alt+C`（视频已经全屏时用热键） |
| 以后更新 | **更新字幕.bat** |

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

## E. 仍需留意

| 项目 | 说明 |
|------|------|
| 桌面快捷方式路径 | 必须调用 `scripts\windows\...ps1`（还是旧路径就重跑安装） |
| `config_local.py` | 永远在**仓库根目录**（不在包里面） |
| 独立 GUI 测试 | `tests\test_hittest.py` 等仍需手动（会开真窗口） |

## F. 常见失败

| 现象 | 处理 |
|------|------|
| `ModuleNotFoundError: realtime_subtitle` | 在仓库根目录跑；重新 pull；venv 必须是这个克隆里的 |
| 旧笔记里 `import config` 失败 | 改成 `from realtime_subtitle import config` |
| 没有中文 / 没有译文 | 启动 Ollama；`ollama list` 对照 `config.OLLAMA_MODEL` |
| 快捷方式提示找不到文件 | 重跑 `scripts\windows\install.ps1` 重新生成 `.bat` |
| 安装路径含中文 | 把克隆挪到例如 `C:\realtime_subtitle` |
