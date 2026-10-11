# TASKS — sprints-002-cloud-asr-first

> 状态枚举：`planned | executing | reviewing | fixing | recheck | done | blocked | split`
> split 用于父 TK：自身不执行，由其子 TK 完成。
> 每次 phase 切换由 Coordinator 同步本表。

| id | title | status | blocked_by | write_scope | reviewer_lanes | notes |
|----|-------|--------|------------|-------------|----------------|-------|
| TK-001 | 探针：rslite 词时间分段 + Groq 429 样本 | done | — | macos-native/Sources/rsbench/**, .governance/sprints-002-cloud-asr-first/probes/** | default | 真机测量由 Coordinator 跑；结论决定 TK-004 走词时间分段还是 A3' |
| TK-002 | P9 夹具 + 云端状态机 + Provider 抽象 | done | — | tests/fixtures/**, tests/test_p9_*.py, realtime_subtitle/remote/routing_rules.py, macos-native/Sources/rslite/Provider.swift, macos-native/Sources/rslite/main.swift(--selftest 段) | architecture | Python 侧纯函数状态机 + Swift 同构；两端读同一份夹具 |
| TK-003 | rslite 云端：CloudClient + QuotaLedger + key/开关 | done | TK-002 | macos-native/Sources/rslite/{CloudClient,QuotaLedger,CloudConfig}.swift, main.swift(参数段) | default,security | 429 细节用 TK-001 样本校正（可后补） |
| TK-004 | rslite CloudSegmenter + 分级路由接入 HybridEngine | done | TK-001, TK-002, TK-003 | macos-native/Sources/rslite/{CloudSegmenter,NodeRouter,HybridEngine,Overlay}.swift | architecture | 最大风险 TK；可能 split |
| TK-005 | Windows 客户端句子带时间 + 样本时钟连续性 | done | — | realtime_subtitle/asr/streaming_asr.py, realtime_subtitle/translate/translator_queue.py, realtime_subtitle/ui/subtitle_window.py, realtime_subtitle/ui/subtitle_render.py, tests/test_timed_sentences*.py | default | 出口条件：clear_context/暂停/设备热切换三种时钟连续测试 |
| TK-006 | Python remote/：node_client + router + line_store + cloud_client + quota | done | TK-002, TK-005 | realtime_subtitle/remote/** (除 routing_rules.py), requirements.txt, tests/test_remote_*.py | default,security | 子 TK：TK-006A（agy）/ TK-006B（grok）；TK-005 只在 TK-007 接线时需要，故提前开工 |
| TK-006A | remote/ node_client + router + line_store | done | TK-002 | realtime_subtitle/remote/{node_client,router,line_store}.py, requirements.txt, tests/test_remote_{node_client,router,line_store}.py | default,security | agy gemini-3.1-pro |
| TK-006B | remote/ cloud_client + quota + cloud_segmenter | done | TK-002, TK-001(429 结论) | realtime_subtitle/remote/{cloud_client,quota,cloud_segmenter}.py, tests/test_remote_{cloud_client,quota,cloud_segmenter}.py | default,security | grok |
| TK-007 | Windows 面板 + 接线 + --client 下发 | done | TK-006 | realtime_subtitle/ui/settings_window.py, realtime_subtitle/app.py, scripts/node/**, scripts/windows/node_setup.ps1, tests/test_settings_*.py, tests/test_install_node*.py | security | key 写入/ACL/下发 token 由 Coordinator 自审 |
| TK-008 | 文档 + v3.2.0 + 端到端验收 | executing | TK-004, TK-007 | README*.md, CLAUDE.md, docs/**, version.py, .governance/sprints-002-cloud-asr-first/acceptance/** | default,security | 文档含隐私承诺改写（需 security lens）；验收 Coordinator 自留 |
| TK-009 | 节点翻译失败三缺口（info.translator_ok / 客户端本机补译 / 状态文案） | executing | — | realtime_subtitle/node/{worker,info,gateway}.py, realtime_subtitle/remote/{router,node_client}.py, realtime_subtitle/app.py, docs/protocol-v2.md, macos-native/Sources/rslite/{NodeClient,NodeRouter,HybridEngine,Overlay,Pipeline}.swift, tests | default | 真机直播验收发现（Win 节点 Ollama 未起 → 每句 translate_failed）；A=Python(codex) / B=Swift(grok) |

## 字段说明
- **write_scope**：路径前缀白名单，Executor 仅能写其中文件
- **reviewer_lanes**：`default` / `architecture` / `security` / `perf` 任意组合，决定派几个 Reviewer
- **blocked_by**：上游依赖的 TK id，列表
- **notes**：split 时记录子 TK id；blocked 时记录原因
