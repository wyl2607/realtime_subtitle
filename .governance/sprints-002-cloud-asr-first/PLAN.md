# PLAN — sprints-002-cloud-asr-first

## 目标
v3.2.0：填了 Groq key 的 Mac（rslite）和 Windows 桌面客户端都能**云端优先**识别，云端不可用时按「自己的节点 → 本机」自动降级、恢复后自动切回；Windows 桌面客户端第一次能接自己的算力节点。

## 接续点
- Sprint 001（remote-offload）已关闭；003/004 已合并；v3.1.0 已发布（084c615）。
- 可直接复用：rslite 的 NodeClient / NodeRouter / MigrationGate / LineStore（003 版 P5），以及 TK-008 的 ready 前缓冲；节点侧 `realtime_subtitle/node/segmenter.py`（Segmenter + SileroVad）。
- 悬挂风险（来自 draft §4）：rslite 没有 VAD，volatile 结果时间粗（TK-009）→ 放在 TK-001 探针；Windows 样本时钟连续性 → TK-005 的出口条件。

## 范围
- 包含：RFC 中 P6' / P7' / P9 / P10 的两端实现、`--client` 下发、文档、v3.2.0 版本号、Mac + Windows 真机验收。
- 明确不做：Cloudflare 等其他云端；云端翻译；Groq 付费档或多 key；Tailscale 自动发现节点；节点机本机共享节点进程的模型；Windows 手动粘贴 token 的 UI。

## 车道（并行 lanes）

| lane | TKs | write_scope 摘要 | 执行 |
|------|-----|-----------------|------|
| L-Mac | TK-001 → TK-003 → TK-004 | `macos-native/Sources/rslite/**`（TK-001 只写 `macos-native/Sources/rsbench/**`） | codex（真机部分由 Coordinator 跑） |
| L-Fix | TK-002 | `tests/fixtures/**`、`tests/test_p9_*.py`、`macos-native/Sources/rslite/Provider.swift` | codex |
| L-Py  | TK-005 → TK-006 | `realtime_subtitle/{asr,translate,ui/subtitle_*}` → `realtime_subtitle/remote/**` | codex / 免费 lane + 审 |
| L-Win | TK-007 | `realtime_subtitle/{ui/settings_window,app}.py`、`scripts/node/**`、`scripts/windows/node_setup.ps1` | codex（安全部分 Coordinator 自留审） |
| L-Doc | TK-008 | README*、CLAUDE.md、docs/**、version.py | inkling / 免费 lane + 审；验收由 Coordinator 做 |

第一批 ready：**TK-001 / TK-002 / TK-005**（写入范围互不覆盖）。

## Checklist（与 TK done 状态保持一致）
- [x] TK-001 探针：rslite 词时间分段精度/延迟 + Groq 429 样本
- [x] TK-002 P9 夹具 + 云端状态机 + Provider 抽象（双端读夹具）
- [x] TK-003 rslite 云端：CloudClient + QuotaLedger + key/开关
- [ ] TK-004 rslite CloudSegmenter + 分级路由接入 HybridEngine
- [ ] TK-005 Windows 客户端句子带时间 + 样本时钟连续性
- [ ] TK-006 Python remote/：node_client + router + line_store + cloud_client + quota
- [ ] TK-007 Windows 面板 + 接线 + `--client` 下发
- [ ] TK-008 文档 + v3.2.0 + Mac/Windows 端到端验收

## 退出条件
- 所有 TK done，RFC Done criteria 1–15 有真机证据（记录在 TK-008 卡片）
- 所有 CR closed（无 pending finding）
- Experience 子 Agent 已抽提候选规则
- 无未解释的失败模式
