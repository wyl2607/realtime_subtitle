# rslite macOS App 安装说明

适用对象：从 GitHub Release 下载 `rslite-<版本>-macos-arm64.zip` 的 Apple Silicon 用户。

## 安装

1. 下载 `rslite-<版本>-macos-arm64.zip` 和同名 `.sha256` 文件。
2. 双击 zip 解压，得到 `rslite.app`。
3. 把 `rslite.app` 拖进“应用程序”。
4. 第一次启动时，不要直接双击。请在 Finder 里右键 `rslite.app`，选择“打开”，再在系统提示里点“打开”。
5. 启动后菜单栏会出现“字·本机”等状态项；需要退出时，从这个菜单里选“退出”。

## 权限

首次使用时，按实际输入模式授予这些权限：

- 麦克风：用于 `mic` 输入模式。
- 语音识别：用于本机 Speech 转写。
- 屏幕与系统音频录制：用于默认 `tap` 输入模式捕获系统播放声音。rslite 不保存屏幕画面。

如果没有弹出权限提示，或曾经点过拒绝，请到“系统设置 → 隐私与安全性”里分别检查“麦克风”“语音识别”“屏幕与系统音频录制”，允许 `rslite.app` 后重新启动。

## 更新时为什么要重新授权

当前 Release 使用 ad-hoc 签名。macOS 的隐私授权会绑定到应用身份和代码签名；ad-hoc 包每次重新打包后签名都会变化，所以更新 `rslite.app` 后，麦克风、语音识别、屏幕与系统音频录制可能需要重新授权。

这不是配置丢失。开发者账号和公证流程确定后，会再切到更稳定的签名与公证发布方式。

## 配置文件

rslite 的配置目录是：

```text
~/.config/rslite
```

常见文件：

- `settings.json`：识别来源、语言、运行模式、指定节点。
- `style.json`：字幕样式。
- `cloud.json`、`groq.key`、`quota-ledger.json`：云端识别开关、密钥和额度记录。
- `nodes.json`、`node-state.json`、`tokens/`：算力节点配置和状态。

## 卸载

1. 从菜单栏“字·…”菜单里选“退出”。
2. 删除“应用程序”里的 `rslite.app`。
3. 如需彻底清除配置，删除 `~/.config/rslite`。

只删除 App 不会删除配置；下次安装会继续沿用原设置。
