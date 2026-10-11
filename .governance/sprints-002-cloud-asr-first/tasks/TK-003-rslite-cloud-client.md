# TK-003 — rslite 云端：CloudClient + QuotaLedger + key/开关

> 字段契约对齐 codex-context-standard。所有接口以 `../RFC.md` 为准，**不得自行修改契约**。

## Goal
rslite 能用配置文件里的 Groq key 按段请求识别、做本地记账与冷却、支持命令行写 key 与开关，满足 RFC 安全决策 1–4。

## Context
- RFC「P6'」「云端请求」「安全决策」；状态机用 TK-002 的 `Provider.swift` / CloudState。
- 本 TK 不接入 HybridEngine（那是 TK-004），只交付可单测的客户端与记账。

## Relationship to Existing Systems (RC-002)
- 共存：A 版单机行为、rslite 本机 B、节点服务端（协议 v2）都不变；Sprint 001 的 P1–P8 不改。
- 衔接：完成后由 Coordinator 更新 TASKS.md / CURSOR.md / metrics；涉及用户可见行为的由 TK-008 写进文档。

## Constraints
- write_scope:
  - macos-native/Sources/rslite/CloudClient.swift、QuotaLedger.swift、CloudConfig.swift
  - macos-native/Sources/rslite/main.swift 的参数解析段（`--set-groq-key` / `--clear-groq-key` / `--cloud on|off`）与 --selftest 新增用例
- 禁止修改：write_scope 以外的任何文件（需要改就停下，写进结果里交给 Coordinator）。
- 不跳过 hooks；不 commit、不 push、不做破坏性 git 操作；不碰线上节点（mini2 / yzwdmsnlmm）。
- 注释用中文，写清「为什么」；不加 RFC 以外的功能、配置旋钮或回退逻辑。
- 遵守仓库 CLAUDE.md 第 4 节（Windows/Python）与第 7 节（macOS）的避坑条目；接口以 `../RFC.md` 为准，不得自行改契约。
- 不读环境变量 `GROQ_API_KEY`；测试里一律用假 key，绝不出现真 key。

## Done criteria
- [ ] key：`~/.config/rslite/groq.key` 0600，stdin 读一行、校验形状（`gsk_` 前缀 + 长度）、原子写；不读任何环境变量
- [ ] 开关：`cloud.json` `{"enabled": bool}`，有 key 时默认 true
- [ ] CloudClient：multipart WAV 16k mono s16le、`verbose_json`、连接 5s / 总 10s；返回 segments 映射到 a0 时间轴；**取消在飞请求**接口（开关关时用）
- [ ] TLS：不实现任何放行证书的 URLSession delegate；走系统代理
- [ ] QuotaLedger：`billed_s=max(10,dur)`；1 分/1 时/24 时窗口；90% 进冷却、<80% 恢复；原子写持久化、>24h 丢弃；用 `x-ratelimit-remaining-requests` 校正
- [ ] 日志只记 x_groq.id/时长/状态码/原因/错误类型；selftest：注入假 key 触发 401/429/5xx/超时，断言日志与错误描述里不含该 key
- [ ] selftest 覆盖计费下限、窗口滚动、滞回、持久化重启不清零；swift build 绿

## 验证命令
```bash
cd macos-native && swift build -c release && .build/release/rslite --selftest
```

## 执行建议
codex（high）；Coordinator 做 security 复审 + 用真 key 跑一次真请求。

## 状态历史
- 2026-10-11 CST: planned（/sdd-rfc）
- 2026-10-11 17:05 CST: executing（批次 2，单 TK）
