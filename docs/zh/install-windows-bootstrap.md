# Windows 引导安装（bootstrap）

不想先装 Git、手动 clone 再跑安装脚本的话，用这个**几十 KB 的引导器**：
它只负责把运行环境拉起来，然后把重活交给仓库里现成的
`scripts\windows\install.ps1`。**不打包 Python / torch / PyQt6**，所以发布物
只有下面两个小文件；真正的依赖（几 GB）由官方源按需下载。

> 仍然完全本地运行：引导器不发送任何音频或文本，只装软件、拉代码、跑本仓库
> 的安装脚本。隐私边界见 [README](../../README.md)。

## 一、下载两个文件

从 GitHub Release 附件里下载，放到**同一个文件夹**（例如新建 `下载\rs`）：

| 文件 | 作用 |
|------|------|
| `bootstrap.cmd` | 双击入口 |
| `bootstrap.ps1` | 引导逻辑（由 `bootstrap.cmd` 调用；**两个文件必须在一起**） |

> 需要 Windows 10 1809+ / Windows 11。系统自带的 Windows PowerShell 5.1 即可，
> 不需要额外装 PowerShell 7。**一般不需要管理员**；只有 Ollama 安装可能弹一次
> UAC 授权（允许即可）。

## 二、双击 `bootstrap.cmd`

脚本会按编号依次做五件事，每一步都打印中文进度：

1. **检查 `winget`**（Windows「应用安装程序」）。没有会告诉你如何去 Microsoft
   Store 安装，然后退出。
2. **用 winget 装 Git / Python 3.12 / Ollama**——任何一项已经装好就跳过；
   装完会刷新当前窗口的 PATH 再继续。
3. **取代码**：`git clone` 仓库到 `%USERPROFILE%\realtime_subtitle`
   （目录已存在则改跑 `git pull`）。
4. **调用 `scripts\windows\install.ps1`**：建 venv、装 Python 依赖、按显存生成
   `config_local.py`、拉翻译模型、在桌面生成「德语直播实时字幕」文件夹
   （含启动 / 下载加字幕 / 停止 / 暂停 / 卸载五个入口）。
5. **建快捷方式**：在**桌面**和**开始菜单**放一个「实时字幕」，指向
   `scripts\windows\start_and_update_subtitles.ps1`——以后双击它就会**先自动
   更新再启动**。桌面的「德语直播实时字幕」文件夹里也有同样的启动入口。

首次点「实时字幕」启动时，还会自动下载语音识别模型（1-3GB），属正常现象。

### 可选参数

不双击、手动调用时可以加参数：

```powershell
# 换分支 / 换安装目录（目录必须纯英文）
powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1 -Branch master -Dir C:\realtime_subtitle
# 自己已有 Ollama 或稍后再装：跳过第 2 步的 Ollama
powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1 -SkipOllama
# 只看会执行哪些命令，什么都不改
powershell -NoProfile -ExecutionPolicy Bypass -File bootstrap.ps1 -DryRun
```

## 三、常见失败

| 现象 | 处理 |
|------|------|
| 「没有找到 winget」 | 打开 Microsoft Store 搜「应用安装程序」(App Installer) 装上，再双击 `bootstrap.cmd` |
| Git / Python / Ollama 安装失败 | 按屏幕提示手动装：Git <https://git-scm.com/download/win>、Python <https://www.python.org/downloads/>（勾选 Add to PATH）、Ollama <https://ollama.com/download>，装完重跑 |
| Ollama 那步弹 UAC 后仍失败 | 允许管理员授权后重跑；或先 `-SkipOllama` 装好字幕，之后自己装 Ollama |
| 「安装目录里有非英文字符」 | 用 `-Dir C:\realtime_subtitle` 指定纯英文目录；中文用户名目录会把桌面 `.bat` 弄坏 |
| 目标目录已存在且不是本仓库 | 换空目录（`-Dir`）或确认后手动删除 |
| `git pull` 失败 | 多半网络/代理问题，或本地改过仓库文件冲突；改过就先 `git stash` 再重跑 |
| `install.ps1` 阶段失败 | 往上翻看它的报错：显卡驱动太旧升驱动；大陆网络改用 `scripts\windows\install.ps1 -Mirror`；杀毒软件拦截就加白名单 |
| 双击「实时字幕」提示找不到脚本 | 重跑一次 `bootstrap.cmd`（会 `git pull` 并重建快捷方式） |

安装完成后的一切（硬件分档、调参、踩过的坑）见
[Windows 操作清单](WINDOWS-RUNBOOK.md) 和仓库根目录的 [CLAUDE.md](../../CLAUDE.md)。
