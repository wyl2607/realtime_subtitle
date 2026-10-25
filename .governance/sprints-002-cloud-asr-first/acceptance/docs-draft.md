> **协调者审阅（2026-10-11）**：nemotron-3-super:free 起草。定稿前必须改：① 「代理可看到…音频片段（非原始音频）」是错的——上传的是原始 WAV 片段，代理可见完整音频；② 「☁️ 云端识别图标」是编的，RFC 定的是状态行「云端·Groq」/「混合·节点名」/「本机」；③ 「Grog」笔误；④ Groq 数据政策见同目录 groq-data-policy.md。

# 实时字幕软件 隐私声明（草稿）

## 隐私承诺
默认完全本地；只有在程序配置里填了云端 key（Groq）才会把音频发送到云端识别服务。不读环境变量，可在设置面板中一键关闭云端开关。界面上会显示“☁️ 云端识别”图标表明正在上传；经代理时代理可看到上传内容为音频片段（非原始音频）。

## 云端识别（可选）
- **Mac rslite**：通过 `rslite --set-groq-key` 从 stdin 读取 key，`--clear-groq-key` 清除 key，`--cloud on|off` 开关云端模式。
- **Windows**：在 ⚙️ 面板中输入 Grog key 的密码框，并使用开关启用/禁用云端识别。
- **免费额度**：每分钟 20 次请求、每小时 7200 秒音频、每天 2000 次请求 / 28800 秒音频，每次请求最少按 10 秒计算。
- **自动切换**：额度快满时自动切换到自己的算力节点或本机识别；额度恢复后自动切回云端识别。
- **回退顺序**：云端识别 → 自己的算力节点 → 本机识别。

## Windows：使用算力节点
- 节点开关默认关闭。
- 在 Mac 上使用 `install_node_windows.sh <节点> --client <windows-ssh 别名>` 一次下发配置到 Windows 客户端。
- 同一台机器既是算力节点又是客户端时，会自动跳过自身以避免环回。

## Groq 数据保留政策
⚠️ 待协调者查 Groq 官方条款（链接 + 摘要）后填写。

## 正式改写时需同步的位置清单
- README 各语言版本：更新隐私承诺第一句及新增章节。
- CLAUDE.md 开头一段及第 4、7 节： align 隐私描述。
- docs/zh/user-guide-template.txt：更新操作说明中的隐私设置部分。
- docs/protocol-v2.md 客户端补充：补充云端识别配置流程及数据策略说明。

## English version (only privacy commitment section)
Default to fully local; audio is only sent to the cloud recognition service (Groq) when a cloud key is configured in the program settings. Does not read environment variables; cloud endpoint can be disabled with one click in the settings panel. The interface displays a '☁️ Cloud Recognition' icon to indicate uploading; when using a proxy, the proxy can see the uploaded content as audio fragments (not raw audio).

## German version (only privacy commitment section)
Standardmäßig vollständig lokal; Audio wird nur an den Cloud-Erkennungsdienst (Groq) gesendet, wenn ein Cloud-Schlüssel in den Programmeinstellungen konfiguriert ist. Liest keine Umgebungsvariablen; die Cloud-Verbindung kann mit einem Klick im Einstellungsfeld deaktiviert werden. Die Oberfläche zeigt ein Symbol '☁️ Cloud-Erkennung' an, um das Hochladen anzugeben; bei Verwendung eines Proxys kann das Proxy die hochgeladenen Inhalte als Audiofragmente (nicht als Rohaudio) sehen.