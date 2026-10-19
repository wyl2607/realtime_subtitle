# v3.2.0 真机操作清单（需要人在机器前）

> 2026-10-19 协调者整理。能远程做的都已做完，见 `release-checklist-v3.2.0.md`。下面每一项做完，在对话里回一句「X 做完了，看到的是……」，协调者经 ssh 收日志、判结果。
> ⚠️ 测 Windows 时别同时开 Mac 上的 A 版字幕（CLAUDE.md §7 第 21 条）。

## 一、Windows（yzwdmsnlmm，Win11 RTX5060）

### 0. 准备（一次）

1. 先关掉桌面上的正式版字幕：双击「德语直播实时字幕」文件夹里的「停止字幕.bat」。
   （正式版 `C:\realtime_subtitle` 还在 master 上，不动它；这次测的是测试副本 `%USERPROFILE%\rs-git`，已切到 feat/macos-native 最新版，`config_local.py` 已从正式版拷过来。）
2. 开一个 PowerShell 窗口，启动测试副本：
   ```powershell
   cd $env:USERPROFILE\rs-git
   & $env:USERPROFILE\rs-test\.venv\Scripts\python.exe main.py
   ```
   日志写到 `%USERPROFILE%\rs-git\subtitle.log`。

### 1. Done 12：节点开关默认关、关着时不连 8791

1. 程序起来后，先**不要动任何开关**，点 ⚙️ 打开设置面板。
2. 看「使用算力节点」：应该是**没勾**（nodes.json 已经下发过，所以它能点，但默认是关的）。
3. 浏览器放一段德语视频（YouTube 搜 tagesschau）30 秒。
4. 另开一个 PowerShell：`netstat -ano | findstr 8791`
   - 判据：可以有 `LISTENING`（那是本机节点自己在监听），但**不能有任何 `ESTABLISHED`**。
5. 回复：开关状态 + netstat 输出。

### 2. Done 11：本机当节点时跳过自己

1. 在面板里勾上「使用算力节点」。
2. 继续放德语视频 1 分钟。
3. 判据：悬浮窗状态行一直是「本机」（不会出现「混合·yzwdmsnlmm」）；本机字幕照常出。
4. 回复「11 做完」，协调者经 ssh 读 `subtitle.log` 里跳过自己的那一行。

### 3. Done 14：单机冒烟

1. 关掉「使用算力节点」。放德语视频：德语白字先上屏，中文 1–3 秒内跟上。
2. Ctrl+Alt+P 暂停 → 字幕停；再按一次 → 恢复。
3. 另开 PowerShell 停止：
   ```powershell
   cd $env:USERPROFILE\rs-git
   powershell -ExecutionPolicy Bypass -File scripts\windows\stop_subtitles.ps1
   ```
   判据：悬浮窗几秒内消失；`ollama ps` 显示没有模型在显存里。
4. 回复：三步各自是否正常。

### 4. 高 DPI（10-11 留下的）

1. 设置 → 系统 → 屏幕 → 缩放改成 150%。
2. 按第 0 步重新启动测试副本，放视频，等出现灰色的「未翻译 ⟳」时点它，应该会重新翻译那一句。
3. 判据：按钮点得中；灰色文字看得清；窗口和字号没有突然变大一半。
4. 测完把缩放改回原值，回复结果（有截图更好，截图里别带私聊）。

### 5. Done 9 / 10：混合档与停节点（等 Done 6 结束、你点头之后）

Windows 客户端现在只有它自己这一个节点（会被跳过），要测混合档得把 mini2 的条目也下发过去。下发命令 `bash scripts/node/install_node.sh mini2 --client yzwdmsnlmm` 会**顺带重装 mini2 节点**，所以要等 mini2 上的 1 小时长测（Done 6）跑完，并且你同意动 mini2。之后：
1. 启动测试副本，勾「使用算力节点」，放德语视频。状态行应为「混合·mini2」；德语不重复、首字不比本机慢。
2. 协调者在第 20 秒左右远程停掉 mini2 节点 → 字幕不断，状态行回到「本机」。
3. 协调者重新启动 mini2 节点 → 60 秒内回到「混合·mini2」。
（Done 9 原计划里的 `--test-playback` 参数并不存在，只能这样真机目测。）

## 二、Mac（MBA）

### 6. 先还原 10-18 的误写（一直挂着的手动待办）

```bash
cd ~/projects/rs-mac-native && macos-native/.build/release/rslite --clear-groq-key && macos-native/.build/release/rslite --cloud on
```

### 7. Done 1：有 key 时悬浮窗显示「云端」

无界面回放已经验过（首个模式状态就是「模式：云端·Groq」）。要看悬浮窗，需要把真 key 写进**你自己**的 rslite 配置——用不用由你决定：
```bash
cd ~/projects/rs-mac-native && (source ~/.config/groq/env; printf %s "$GROQ_API_KEY") | macos-native/.build/release/rslite --set-groq-key
macos-native/.build/release/rslite --source tap
```
判据：放德语音频，悬浮窗状态行显示「云端·Groq」。

### 8. Done 4：断网回退

1. 第 7 步的 rslite 开着、在出字时，关掉 Wi-Fi 约 20 秒。
2. 判据：字幕不断，状态行退到「本机」（断网时节点也连不上）。
3. 打开 Wi-Fi：静音点之后切回「云端·Groq」，没有重复句、没有丢句。
