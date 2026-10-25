# Spec 008 — 安卓 App 重写（原生、低功耗、三端同风格）

状态：requirement（2026-10-11 用户批准全面重写）
仓库：`wyl2607/realtime_subtitle_android`（本机 `~/projects/realtime_subtitle_android`），在分支 `rewrite/v1` 上做，验收后替换 main。

## 用户决定（2026-10-11）
- 批准全面重写安卓 App，在本 Mac 开发，一加 13（CPH2653，Android 16 / SDK 36，Snapdragon 8 Elite SM8750）主测。
- **只保留功耗最低、速度最快的一个本地版本**，不再提供多档模型。
- **Win / Mac / 安卓三端同一风格开发**，尽可能原生。
- iOS 以后做，沿用同一套结构。

## 背景（现状与实测）
- 现有 App v0.4.0：Kotlin + Compose，约 9.5k 行；四档 Whisper（base/small/medium/turbo）+ 草稿双模型 + 测速/降档/A-B 工具，ASR 占 CPU ~90%，主页 HomeScreen 1488 行。
- 2026-10-11 20:37 已验证「电脑算力」可用：手机 → Tailscale → Windows 节点，段长 0.7–8s、识别 0.3–0.5s、翻译 0.1–0.35s，全屏页正常出字。暴露的问题：主页「今日字幕」不收节点句；状态显示配对名而非「混合·节点」；ready 超时曾为 3s（已改 15s）。
- 手机上有 Android System Intelligence（`com.google.android.as`，提供 `AiAiSpeechRecognitionService`）、Google 语音服务、AICore、GMS——系统端侧识别 + ML Kit 端侧翻译这条「最原生」路线有条件，但德语离线包是否可用未知。

## 原则
1. **电脑算力优先**：节点可用时手机只采音 + 显示（功耗最低、质量最高）；不可用时自动回本机。与 rslite 同一路由语义：仅本机 / 自动 / 指定节点，默认仅本机（与 Mac 一致；用户配对后可改）。
2. **本机只留一个引擎**，由 R0 实测决定：
   - **A（首选，最原生）**：`SpeechRecognizer.createOnDeviceSpeechRecognizer` + `EXTRA_SEGMENTED_SESSION` 连续识别（API 33+ 支持 `EXTRA_AUDIO_SOURCE` 喂内录音频）+ ML Kit 端侧翻译 de→zh。相当于 Mac 用 Apple Speech + Apple Translation。
   - **B（兜底）**：沿用已验证的 sherpa-onnx Whisper **small int8**（一加 13 rtf≈0.22）+ Opus-MT。只装 small 一个模型。
   - 判据：德语离线可用、连续 30 分钟不断流、CPU/功耗低于 B、字准与译文质量不明显差于 B（同一段 tagesschau 对比）。
3. **三端同风格**：`design/tokens.json` 生成 Kotlin `DesignTokens.kt`（扩展 `scripts/design/gen_tokens.py`，同步测试照 Swift/Python）；字幕卡片（深色底、原文 #F0F0F0 / 译文 #FFE894、旧句变淡、瀑布流、顶部渐隐）、状态词（本机 / 混合·节点名 / 云端·Groq）、「未翻译 ⟳」、设置分页（字幕样式 / 识别与语言 / 算力节点）与 Mac/Windows 一致。
4. **逻辑复用、界面重写**：协议 v2、TailnetPin、路由、P5 按时间替换（LineStore）照 rslite Swift 实现移植；10-11 已写的 NodeClient/TailnetPin/配对存储直接搬进新结构。
5. **原生**：Kotlin + Jetpack Compose + 系统服务；不引入跨平台框架。

## 架构（Gradle 模块）
| 模块 | 内容 | 依赖 |
|---|---|---|
| `:core` | 纯 Kotlin（JVM 可测）：协议 v2 编解码、NodeClient（OkHttp）、TailnetPin、Router（节点打分/迁移，对齐 `NodeRouter.swift` 的未指定/指定/本机语义）、LineStore（句对、P5 替换、失败状态、历史上限 60）、SentenceCommitter、DesignTokens（生成） | OkHttp、coroutines |
| `:engine` | `LocalEngine` 接口 + 选定的唯一实现（A 或 B）；音频源：麦克风、内录（MediaProjection AudioPlaybackCapture） | Android SDK |
| `:app` | 前台服务、悬浮窗（可拖、四向缩放、拉高即瀑布流、滚轮/手势回看）、全屏翻译、设置三页、配对（扫码 + `rsnode://pair` 深链 + 粘贴）、今日字幕/历史（含节点句） | Compose |

删除：base/medium/turbo 模型与切换、草稿双模型、AccelQuickBench / RtfLagGuard / NNAPI 实验、混合音频模式（从未人手实测）、快设以外的冗余入口。保留：快设磁贴一键启停、通知栏停止。

## 配对与安全
- 配对串 `rsnode://pair?v=1&id=&host=&port=&tok=&name=`，`name` 定义为**节点显示名**；扫码（CameraX + ML Kit 条码，或系统相机打开深链）→ 预填 → 用户点保存 → 校验 100.64.0.0/10 + `/v1/info` 核对 node_id → Keystore AES-GCM 存储、备份排除。
- 节点连接：钉 IP、`Proxy.NO_PROXY`、Bearer 只在头里、日志无 token 无正文。ready 超时 15s；1013/1005 处理照现有实现。
- 状态行：「混合·<节点名>」/「本机」/「<节点名> 不可用，已用本机」，与 rslite 文案一致。

## 分阶段
| 阶段 | 内容 | 验收 |
|---|---|---|
| R0 | 真机探针：A 路线可用性（`checkRecognitionSupport(de-DE)`、离线包下载、内录音频喂入、连续 30 分钟、CPU/电流）+ 与 B 同段音频对比 | 写 `docs/rewrite/R0-engine-probe.md`，定 A 或 B |
| R1 | 新骨架：三模块、DesignTokens.kt 生成与同步测试、`:core` 移植（协议/NodeClient/TailnetPin/Router/LineStore）+ JVM 单测 | 单测全绿；Router/LineStore 用例与 Swift selftest 对齐 |
| R2 | `:engine` 唯一本地引擎 + 音频源（麦克风/内录） | 真机 30 分钟稳定，CPU 低于旧版 small@4t |
| R3 | `:app` 界面：悬浮窗（含瀑布流）、全屏、设置三页、配对扫码、今日字幕 | 截图与 Mac 版并排比对风格一致 |
| R4 | 真机验收：电脑算力 ↔ 本机切换不丢句、断 Wi-Fi 回本机、节点恢复接回、功耗对比（`dumpsys batterystats` 30 分钟） | 写入 STATUS.md，替换 main |

## 不做（本 spec）
iOS（另立 spec，复用同结构）；Opus 压缩上行；云端 Groq（手机端先不接）；收费开关（等用户定商业策略）。

## 排期（2026-10-11 晚，Mac/Win 收尾后安卓按此顺序推进）

| 序 | 任务 | 状态 / 负责 | 完成标准 |
|---|---|---|---|
| 1 | R0 引擎探针 | ✅ 定 A（系统端侧识别），rewrite/v1 a8ea06f | — |
| 2 | R1 骨架 + :core + Kotlin 令牌 | ✅ rewrite/v1 b6c43f4 | 241 测全绿 |
| 3 | R2 :engine | ✅ rewrite/v1 ffc05f8 | 真机 30 分钟稳定待 R4 测 |
| 4 | R3a 界面（lane/r3-ui）、R3b 悬浮窗（lane/r3-overlay） | 已交付，待协调者审+合 | gradle 单测绿 |
| 5 | R3c 会话/前台服务接线（lane/r3-session） | grok 在跑 | 引擎/节点/界面/悬浮窗串通 |
| 6 | 集成：合 4+5 到 rewrite/v1，装一加 13 | 协调者 | 能出字（本机 + 节点两条路） |
| 7 | 旧版遗留问题修复（并入 6 的验收）：「今日字幕」收节点句；状态行显示「混合·节点名」；配对串 name 语义定为节点名；繁体字混入 → 输出统一转简体 | 派 lane | 逐条真机截图 |
| 8 | R4 真机验收：节点↔本机切换不丢句、断 Wi-Fi 回本机、节点恢复接回、30 分钟 batterystats 功耗对比 | 协调者 | 写 STATUS.md |
| 9 | ML Kit 德→中下载对照（手机热点 + 干净安装 + 官方 sample）；不行则维持 Opus-MT | 协调者 | 结论入 R0 文档 |
| 10 | 扫码配对（spec 007 二维码，Mac/Win 端出码） | 派 lane | 手机扫码即配对 |
| 11 | rewrite/v1 替换 main，发内测 APK（GitHub Pre-release，私有仓） | 用户确认后 | — |
| 后 | iOS（复用 rslite Swift 核心） | 以后 | — |
