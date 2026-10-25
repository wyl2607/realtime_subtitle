# Spec 010 — 四端统一体验（Windows / Mac / Android / iOS）

状态：requirement（2026-10-21 用户确认：统一体验按下面的核心项；四条线并行，DeepSeek flash 写代码、协调者复核）

## Goal
四个平台给用户的**核心体验一致**：同一套路由规则、同一套句对替换规则、同一套状态文案和设计令牌、同一种配对方式、同一种历史存档。
系统能力造成的差异明确列出并接受，不为"看起来一样"去绕系统限制。

## 核心项（每一项都以仓库里的单一真相源为准）

| # | 核心项 | 单一真相源 | 判据 |
|---|---|---|---|
| C1 | 三级自动路由：云端 Groq → 电脑节点 → 本机，失败自动降级、恢复后在静音点切回 | `tests/fixtures/route_cases.json` | 各端都有一份**逐字节相同**的夹具副本，并有测试读它全部通过 |
| C2 | 远端句覆盖本机句（P5：覆盖 ≥50% 替换、多条远端句并集累计覆盖、已滚出屏幕不回滚） | `tests/fixtures/p5_cases.json` | 同上 |
| C3 | 状态文案：`本机` / `混合·<节点>` / `云端·Groq` / `云端 key 无效` / 云端限流冷却 / `<节点> 不可用，已用本机` | `design/tokens.json` 的 `text` 段 | 各端从生成的 DesignTokens 取文案，代码里不写死 |
| C4 | 视觉：颜色、字号、间距、动效 | `design/tokens.json` | 各端 DesignTokens 由生成器产出，`--check` 能发现漂移 |
| C5 | 配对：`rsnode://pair?...` 链接一次录入；token 只进系统安全存储（Keychain / Keystore / 只给当前用户+SYSTEM 的 ACL 文件） | `docs/protocol-v2.md` + spec 007 | 日志无正文、无 token |
| C6 | 历史存档：每天一个文件，时间 + 原文 + 中文 | Windows 版 `transcripts/` 格式 | 各端都能回看当天字幕 |
| C7 | 隐私：云端只在「填了 key 且开关开」时启用；节点只连 tailnet `100.64.0.0/10` 并钉 IP；音频只进内存 | spec 002 / `TailnetPin` | 各端都有对应测试 |

## 接受的平台差异（不算缺口）
- iOS 不能在其他 App 上方开悬浮窗，只能用**画中画**显示；系统录屏广播扩展内存约 50MB。
- 手机没有全局热键；点词查词、🤖AI 分析暂时只在桌面端（以后另立 spec）。
- macOS 抓系统声音要 BlackHole（Python 版）或 Process Tap（rslite）。

## 2026-10-21 差距（协调者盘点）

| 核心项 | Windows | Mac（Python/rslite） | Android rewrite/v1 | iOS spike |
|---|---|---|---|---|
| C1 三级路由 | ✅ | ✅ | ❌ 无云端档（节点→本机已有） | ❌ 无云端档、节点/本机手动二选一 |
| C2 P5 | ✅ | ✅ | LineStore 已移植，未读共享夹具 | ❌ |
| C3 文案 | 部分（缺 key 无效/冷却 token） | 部分（同左） | 读 tokens，缺云端文案 | ❌ 写死 |
| C4 令牌 | ✅ | ✅ | ✅（仓库内副本） | ❌ 无 DesignTokens |
| C5 配对 | ✅ `--client` | ✅ | ✅ 未真机 | ✅ 未真机 |
| C6 历史 | ✅ | ✅ | 今日字幕 | ❌ |

## 本轮分线（文件不重叠）
- **L-A android-cloud**（Android 仓库，基于 rewrite/v1）：`:core` 纯逻辑移植云端档（路由状态机、额度账本、分段器、Groq 客户端）+ 读共享夹具的测试。接 UI 下一轮做。
- **L-B ios-parity**（基于 spike/ios-broadcast）：iOS 共享路由逻辑 + 读夹具自测；「自动」模式下节点失败自动回本机；当天字幕存档。
- **L-C v320-closeout**（基于 feat/macos-native）：tokens 补 `云端 key 无效` / 限流冷却文案并接到 rslite 与 Python；FLEURS 经 rslite 的评测入口。
- **L-D parity-tokens**（基于 spike/ios-broadcast）：生成器产出 iOS 的 DesignTokens；跨端一致性检查脚本（夹具与 tokens 副本逐字节比对）。

## Done criteria（本 spec）
1. 四端各自的夹具测试全绿（pytest、`rslite --selftest`、`:core:test`、iOS selftest）。
2. `scripts/design/gen_tokens.py --check` 与一致性检查脚本全绿，覆盖 Swift / Python / iOS / Android 副本。
3. 状态文案在四端代码里没有写死的副本（grep 可证）。
4. 真机项单列给用户（不由模型声称通过）：Android 一加 13、iPhone、Windows 高缩放屏。

## 不做（本 spec）
App Store / Play 上架；移动端点词查词与 AI 分析；Linux。
