# Sprint 002 真机验收方案（Mac 云端优先 + Windows 节点客户端）

> **协调者核对（2026-10-11 13:42，执行前必读，优先于正文）**：agy 起草，命令已逐条对过仓库。以下是正文中的错误和缺口：
> 1. **Done 9 的 `python -m realtime_subtitle.app --test-playback ... --mode` 是编的**，Windows 程序没有这个参数。Windows 混合档只能真机放音频人工计时，评分方式明天现场定。
> 2. Done 8 的文件名写错了：实际是 `README.md` / `README.en.md` / `README.de.md`（还有指向桩 `README.zh.md`），不是 `README.zh-CN.md` / `README.de-DE.md`。
> 3. Done 7 的 `swift test` 删掉：本机没有 Xcode，XCTest 跑不了。Swift 侧以 `rslite --selftest` 为准。
> 4. Done 13：token 文件名是 `tokens\<节点 ssh 别名>.token`，不是 `<node_id>`；ACL 是当前用户 + SYSTEM（复用 Lock-Down），不是「仅当前用户」。只有面板写的 `groq.key` 是仅当前用户。
> 5. **Done 6 放 mini2 的前置不成立**：mini2 上没有 `~/projects/rs-mac-native` 和 `rs-mac-native-data`，`rslite` 也不在 PATH，`1hour_stream.wav` 不存在。执行前要先 scp release 版 rslite 和拼好的 1 小时素材，命令改成绝对路径；key 用 `ssh mini2 '<绝对路径>/rslite --set-groq-key' < 本机临时文件`，用完删除。另外 mini2 同时是 Tier1 节点，长测期间不要在 mini2 上做 Done 10 停节点。
> 6. Done 4 场景 3 会**覆盖真实的 `~/.config/rslite/quota-ledger.json`**，先备份到 `.bak`，测完还原（字段名 `events[].ts/billed_s`、`request_corrections` 已核对，正确）。
> 7. 已核对无误：rslite 参数（`--source tap|mic|file:PATH --headless --mode auto|local|hybrid --set-groq-key --clear-groq-key --cloud on|off --selftest`）、`scripts/bench/hybrid_score.py` 参数、`probes/groq_429_probe.py --wav --out`、`concat5.wav` / `refs.jsonl` / `wav/` 路径、launchd 标签 `com.realtimesubtitle.node`。
> 8. 场景 2 用探针打 429 会消耗真实额度（约 24 次请求），放在 Done 6 长测之前或之后，不要在长测窗口里跑，否则污染额度核对。


> 依据文件：
> - 契约与决策：`.governance/sprints-002-cloud-asr-first/RFC.md`（Done criteria 1–15、P7' 状态机、安全决策）与 `PLAN.md`
> - 实测依据：`probes/segmentation.md`（A3' rms 0.005 结论）与 `probes/groq-429.md`（429 样本与令牌桶结论）
> - 需求规格：`.governance/specs/002-cloud-asr-first/requirement.md` §4 与 `.governance/specs/005-windows-client-nodes/requirement.md` §4
> - 避坑守则：`CLAUDE.md` §7 第 20/21 条（rslite 单实例、A/B 严禁同开）
>
> 核心原则：
> 1. **零密钥泄露**：Groq key 一律写「由协调者临时放入、测完删除」，命令与日志绝不出现 key 明文。
> 2. **零 sudo 故障注入**：错 key 走临时 key 文件；429 走高频短并发连发；额度满走构造账本文件；断网优先标注「需人工关闭 Wi-Fi / 拔网线（需用户在场）」，不编造不存在的 CLI 参数。
> 3. **单实例与防争抢**：同一台机器不重叠运行 rslite GUI/tap 实例；测 rslite 时严禁同时跑 Python A 版。

---

## 1. 验收环境与设备分配

| 设备代码 | 机器配置与身份 | 角色与网络 | 承担验收任务 |
|---|---|---|---|
| **MBA** | MacBook Air M2 (16GB), macOS 27 (Sequoia)<br>开发机 / 本机 | 客户端（rslite GUI / headless）<br>Tailscale 本机，系统/Surge 代理访问 Groq API | **Done 1–5, 7, 8, 13(发起), 15**<br>Mac 端核心功能、质量门禁、故障注入、WER/延迟 |
| **mini2** | Mac mini M4, macOS 27<br>IP: `100.105.163.59` | 独立 rslite 客户端 / Tier 1 备用节点<br>常驻 `com.realtimesubtitle.node` (gateway:8791) | **Done 6 (独占 1 小时额度长测)**<br>同时作为 Done 4/5/10 的远程节点服务 |
| **yzwdmsnlmm** | Windows 11, RTX 5060 (8GB)<br>Tailscale 节点机 + 客户端机 | Windows 客户端（Python/PyQt6）<br>兼 Windows 节点 (004 已部署 gateway:8791) | **Done 9–12, 13(接收), 14**<br>Windows 混合档、跳过自己、下发免配、Windows 测试 |

---

## 2. Done Criteria 1–15 逐条验收执行方案

### 002：云端识别优先（Mac rslite）

#### Done 1：有 key 时 rslite 默认走 Groq，界面显示「云端」

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**MBA 本机**（需图形桌面会话以渲染 Overlay 悬浮窗）。<br>2. 停止 Python A 版字幕：`bash "$HOME/projects/rs-mac-native/scripts/macos/stop.sh"`。<br>3. 确认无残余 rslite：`pgrep -lf rslite \|\| echo "clean"`。<br>4. Groq key 由协调者临时写入，确认开关为开。 |
| **精确步骤** | 1. 编译 release 二进制：<br>`(cd macos-native && swift build -c release)`<br>2. 临时写入 Groq key（stdin 传入，协调者操作）：<br>`macos-native/.build/release/rslite --set-groq-key`<br>3. 确认开关开启：<br>`macos-native/.build/release/rslite --cloud on`<br>4. 启动 rslite GUI 模式（默认 source 为 tap 或指定 mic）：<br>`macos-native/.build/release/rslite --source tap`<br>5. 观察悬浮窗状态栏文案；同时检查 stderr 路由日志确认 Tier 0 选中。<br>6. 退出进程，清理 key：<br>`macos-native/.build/release/rslite --clear-groq-key` |
| **判据** | 1. 悬浮窗状态行清晰显示「云端·Groq」（或「云端」）标识，非「混合」或「本机」。<br>2. stderr / 日志记录路由选择为 Tier 0 `cloud:groq`，状态为 `available`。 |
| **留存证据** | 1. 截图/描述：`acceptance/crit01_overlay_cloud_status.png`（悬浮窗状态标识截图）。<br>2. 启动路由日志：`acceptance/crit01_route_startup.log`。 |
| **预计耗时** | 3 分钟 |

---

#### Done 2：准确率（FLEURS 60 条 WER ≤ 4.5%；67 秒回放 WER ≤ 2.63%）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**MBA 本机**。<br>2. 测试素材存在：`~/projects/rs-mac-native-data/concat5.wav`（67s，5 句，含 1s 句间静音）与 `~/projects/rs-mac-native-data/refs.jsonl`，FLEURS 60 wav 文件目录 `~/projects/rs-mac-native-data/wav/`。<br>3. 协调者临时写入有效 Groq key：`rslite --set-groq-key`。<br>4. 确保 mini2 节点与网络均在线，A 版已停止。 |
| **精确步骤** | **Part A：67 秒回放 (concat5.wav)**<br>1. 运行 rslite headless 模式回放：<br>`macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > acceptance/crit02_concat5_events.jsonl 2> acceptance/crit02_concat5.stderr.log`<br>2. 执行打分：<br>`~/projects/rs-mac-native/venv/bin/python scripts/bench/hybrid_score.py acceptance/crit02_concat5_events.jsonl --refs $HOME/projects/rs-mac-native-data/refs.jsonl --n 5 --json > acceptance/crit02_concat5_score.json`<br><br>**Part B：FLEURS 60 条切段评测**<br>1. 运行 FLEURS 60 批处理回放转写（调用 rslite headless 处理 60 句音频）：<br>`~/projects/rs-mac-native/venv/bin/python scripts/bench/asr_bench.py --parquet $HOME/projects/rs-mac-native-data/fleurs_de_80.parquet --n 60 --out acceptance/crit02_fleurs60_hyp.jsonl`（或对 `wav/0000.wav`~`0059.wav` 执行切段识别收集）。<br>2. 计算 60 条总 WER 并输出报告到 `acceptance/crit02_fleurs60_score.json`。<br>3. 测完清除 key：`macos-native/.build/release/rslite --clear-groq-key`。 |
| **判据** | 1. FLEURS 60 条：最终 WER **≤ 4.5%**（RFC 原文）。<br>2. 67 秒回放（concat5）：最终文本 WER **≤ 2.63%**（Sprint 001 混合档最终 WER，即 003 复测基线 2.63%）。 |
| **留存证据** | 1. 67s 评分报告：`acceptance/crit02_concat5_score.json`<br>2. FLEURS 60 评分报告：`acceptance/crit02_fleurs60_score.json` |
| **预计耗时** | 10 分钟 |

---

#### Done 3：延迟（段尾 → 识别上屏中位数 ≤ 2s，不含翻译）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**MBA 本机**。<br>2. 依赖 Done 2 中已产生的 `acceptance/crit02_concat5_events.jsonl`（或单跑一轮 67s 回放）。<br>3. 网络处于正常代理环境，Groq API 正常响应。 |
| **精确步骤** | 1. 从事件流提取精修结果延迟（final 到达墙钟 `t` 减去客户端样本时钟段尾 `t1`）：<br>`~/projects/rs-mac-native/venv/bin/python -c 'import json, statistics; events = [json.loads(l) for l in open("acceptance/crit02_concat5_events.jsonl") if l.strip().startswith("{")]; lats = [e["t"] - e["t1"] for e in events if e.get("ev") == "final" and e.get("t1") is not None and ("[" in e.get("text", "") or e.get("id") is None)]; print(json.dumps({"count": len(lats), "latencies": lats, "median_s": round(statistics.median(lats), 3) if lats else None}, indent=2))' > acceptance/crit03_latency.json`<br>2. 查看提取结果中的 `median_s`。 |
| **判据** | 段尾（VAD/切段器判定的段尾时刻）到识别结果到达上屏（headless final 事件输出），中位数 **≤ 2.0s**（不含翻译，RFC 原文）。 |
| **留存证据** | `acceptance/crit03_latency.json`（包含全部段落延迟列表与中位数统计）。 |
| **预计耗时** | 2 分钟 |

---

#### Done 4：回退（6 种场景，字幕不断、不重复、不丢句，日志写明原因）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**MBA 本机**（客户端），**mini2**（作为 Tier 1 备用节点在 `:8791` 正常运行）。<br>2. 停止 Python A 版，确保单实例无冲突。<br>3. 协调者临时放入 Groq key（除错 key 步骤外）。 |
| **精确步骤与<br>故障注入** | **场景 1：错 key**（无 sudo）<br>- 写入假 key：`echo "gsk_invalid_test_key_0123456789abcdef" \| macos-native/.build/release/rslite --set-groq-key`<br>- 启动回放：`macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > acceptance/crit04_badkey_events.jsonl 2> acceptance/crit04_badkey.log`<br>- 检验：Groq 返回 401，云端状态机切入 `disabled_invalid_key`，降级至 mini2 节点，日志记录 `mark_key_invalid`。<br><br>**场景 2：模拟 429 限流**（无 sudo）<br>- 协调者放入真 key。<br>- 触发 429 连发注入（参考 `probes/groq_429_probe.py`：短时间 24 发并发 1s 音频打满 RPM 20）：<br>`~/projects/rs-mac-native/venv/bin/python .governance/sprints-002-cloud-asr-first/probes/groq_429_probe.py --wav $HOME/projects/rs-mac-native-data/concat5.wav --out /tmp/inject_429.json`<br>- 紧接着启动 rslite 回放：`macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > acceptance/crit04_429_events.jsonl 2> acceptance/crit04_429.log`<br>- 检验：遇到 429 进入 `cooldown_limit` 冷却，降级至 mini2 节点，日志记录 `rate_limit_cooldown retry_after=...`。<br><br>**场景 3：额度满**（无 sudo，构造账本文件）<br>- 构造满额度账本覆盖 `~/.config/rslite/quota-ledger.json`：<br>`echo '{"events":[{"ts":'$(date +%s)',"billed_s":6500.0}],"request_corrections":[]}' > ~/.config/rslite/quota-ledger.json && chmod 0600 ~/.config/rslite/quota-ledger.json`<br>（6500s / 7200s = 90.28% ≥ 90% 阈值）。<br>- 启动回放：`macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > acceptance/crit04_quota_events.jsonl 2> acceptance/crit04_quota.log`<br>- 检验：用量 ≥ 90% 触发 `cooldown_quota`，降级至 mini2，日志记录 `quota_cooldown`。测试后删除伪造账本。<br><br>**场景 4：关开关**（无 sudo）<br>- 放入有效 key，执行：`macos-native/.build/release/rslite --cloud off`<br>- 启动回放：`macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > acceptance/crit04_switchoff_events.jsonl 2> acceptance/crit04_switchoff.log`<br>- 检验：状态为 `disabled`，立即取消在飞请求，降级至 mini2，日志记录开关关闭。<br><br>**场景 5：拔网（人工操作，需用户在场）**<br>- 开启云端，启动回放。在第 15 秒由**在场用户手动关闭 MBA Wi-Fi**。<br>- 5 秒后网络超时，连续失败触发退避降级至本机 B（因连不上节点与云端）。<br>- 回放结束后，用户手动重新开启 Wi-Fi。<br>- 保存日志与事件至 `acceptance/crit04_offline.log`。<br><br>**场景 6：mini2 节点也离线（落到本机 B）**（无 sudo）<br>- 放入假 key（使云端不可用），同时临时重命名 MBA 上的节点配置：<br>`mv ~/.config/rslite/nodes.json ~/.config/rslite/nodes.json.bak`<br>- 启动回放：`macos-native/.build/release/rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > acceptance/crit04_nodedown_events.jsonl 2> acceptance/crit04_nodedown.log`<br>- 检验：云端不可用且无可用节点，路由降级到 Tier 2 本机 Apple 识别，字幕正常输出。<br>- 还原配置：`mv ~/.config/rslite/nodes.json.bak ~/.config/rslite/nodes.json`。 |
| **判据** | 1. 6 种场景回放全过程**字幕不断、不重复、不丢句**（运行 `hybrid_score.py` 检查 `problem_counts` 均为 0）。<br>2. stderr 日志明确记录切换原因：`mark_key_invalid`, `rate_limit_cooldown`, `quota_cooldown`, `disabled_switch_off`, `consecutive_failure_backoff`, `no_available_node`。 |
| **留存证据** | 1. `acceptance/crit04_fallback_badkey.log`<br>2. `acceptance/crit04_fallback_429.log`<br>3. `acceptance/crit04_fallback_quota.log`<br>4. `acceptance/crit04_fallback_switchoff.log`<br>5. `acceptance/crit04_fallback_offline.log`<br>6. `acceptance/crit04_fallback_node_down.log` |
| **预计耗时** | 15 分钟 |

---

#### Done 5：恢复（故障消失后在下一个静音点自动切回 Groq）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**MBA 本机**，mini2 节点正常在线。<br>2. 协调者临时写入有效 Groq key。<br>3. 停止 Python A 版。 |
| **精确步骤** | 1. 构造一个短时故障：例如设置 `cloud.json` 为 `{"enabled": false}`。<br>2. 启动长音频回放（concat5.wav）：此时初始降级在 mini2 节点。<br>3. 在音频第 15 秒（第一句结束后），通过命令行恢复开关：<br>`macos-native/.build/release/rslite --cloud on`<br>4. 观察状态机进入 `probe` 试探状态；下一个段边界试探 Groq 成功；<br>5. 监控在随后的静音点（静音 ≥ 0.6s），MigrationGate 判定切回 Tier 0 Groq。<br>6. 输出事件与日志分析切换点的时间戳与静音间隙。 |
| **判据** | 1. 故障消除后，在**下一个静音点**（静音 ≥ 0.6s）自动切回 Groq。<br>2. 切换前后字幕时间轴平滑衔接，无丢句，无重复句，日志记录静音点切换详情。 |
| **留存证据** | `acceptance/crit05_recovery_switchback.log`（包含试探成功与静音点切回日志）。 |
| **预计耗时** | 5 分钟 |

---

#### Done 6：额度（连续 1 小时长测，不触发 429，记账与响应头一致，音频秒偏差 ≤ 5%）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**mini2 独立运行**（专占一台，与 MBA / Windows 完全并行）。<br>2. 协调者在 mini2 上临时配置有效 Groq key：<br>`ssh mini2 'mkdir -p ~/.config/rslite && rslite --set-groq-key'`<br>3. 准备 1 小时连续德语音频素材（可将 FLEURS 多次拼接或循环回放，时长 3600s）。<br>4. mini2 上停止其它干扰任务。 |
| **精确步骤** | 1. 记录测试开始前 Groq 响应头的基线（发一次探测段取初始 `x-ratelimit-remaining-requests`，记为 `R_start`）。<br>2. 启动 rslite 客户端 headless 连续回放 60 分钟：<br>`ssh mini2 "caffeinate -i macos-native/.build/release/rslite --source file:~/projects/rs-mac-native-data/1hour_stream.wav --headless > /tmp/rslite_1hour.jsonl 2> /tmp/rslite_1hour.stderr.log"`<br>3. 运行满 60 分钟后进程正常退出。<br>4. 收集测试产物并核对：<br>   - 从日志中提取结束时的 `x-ratelimit-remaining-requests`（记为 `R_end`），计算消耗 `Delta_R = R_start - R_end`；<br>   - 从 `~/.config/rslite/quota-ledger.json` 中读取本地记账请求数 `N_local`；<br>   - 计算音频秒记账总和 `S_local = sum(event.billed_s)`；<br>   - 若 Groq 控制台提供小时级用量，比对偏差 `\|S_local - S_console\| / S_console <= 5%`；若控制台无逐小时细分，按 requirement §4 退回核对方式：断言本地记账严格满足 `S_local == sum(max(10, dur_i))`，并在报告中写明。 |
| **判据** | 1. 全程 60 分钟内**0 次**被动 HTTP 429 发生。<br>2. 请求次数记账与响应头完全一致：`N_local == Delta_R`。<br>3. 音频秒数偏差 **≤ 5%**（或退回核对公式计算严格成立）。 |
| **留存证据** | 1. 1 小时账本副本：`acceptance/crit06_1hour_ledger.json`<br>2. 统计与核对报告：`acceptance/crit06_1hour_summary.txt`<br>3. 运行日志摘要：`acceptance/crit06_1hour_tail.log` |
| **预计耗时** | 60 分钟（独占 mini2，不阻断其它测试） |

---

#### Done 7：质量门禁（pytest / swift build / ruff 全绿，覆盖切段、计费下限、记账、回退、开关、TLS/key 不泄露）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**MBA 本机**。<br>2. Python 环境 `~/projects/rs-mac-native/venv` 可用；Swift 工具链可用。 |
| **精确步骤** | 1. 运行 Python 单元测试（使用独立临时缓存）：<br>`~/projects/rs-mac-native/venv/bin/python -m pytest -q -p no:cacheprovider --basetemp=/private/tmp/rs-accept-pytest`<br>2. 运行 Python linter：<br>`~/projects/rs-mac-native/venv/bin/ruff check .`<br>3. 编译 Swift release 产品：<br>`(cd macos-native && swift build -c release)`<br>4. 运行 Swift 自检套件（覆盖 CloudConfig, QuotaLedger, CloudClient, NodeRouter, HybridEngine）：<br>`macos-native/.build/release/rslite --selftest`<br>5. 运行 Swift Package 单元测试：<br>`(cd macos-native && swift test)`<br>6. 检查单测覆盖点：切段最小长度、计费下限 max(10, dur)、滚动记账、P9 夹具状态机回退、开关原子写、TLS 禁止关闭验证（无 `verify=False`、无放行 delegate）、假 key 不进入日志或异常文本。 |
| **判据** | 1. pytest 0 failed；<br>2. ruff 0 errors；<br>3. swift build 退出码 0；<br>4. `rslite --selftest` 输出 `SELFTEST OK`；<br>5. 安全单测全部绿（TLS、key 掩码、权限 0600）。 |
| **留存证据** | `acceptance/crit07_quality_gates.log` |
| **预计耗时** | 3 分钟 |

---

#### Done 8：文档与隐私承诺（README 写明开关、隐私、key 配置方法）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 检查代码仓库根目录文档。 |
| **精确步骤** | 1. 审查 `README.md`、`README.zh-CN.md`、`README.de-DE.md`：<br>   - 确认隐私承诺已由「不向任何云端发送」改写为「默认完全本地；只有配置了云端 key 才会把音频发送到云端识别服务」；<br>   - 确认写明 Mac rslite key 配置命令（`--set-groq-key` / `--clear-groq-key`）及开关方法（`--cloud on|off`）；<br>   - 确认写明 Windows ⚙️ 面板密码框与使用算力节点配置说明；<br>   - 确认包含代理可见性提示（经代理时代理可见上传内容）与 Groq 数据保留条款链接。<br>2. 输出文档 diff 摘要。 |
| **判据** | 三语 README 准确同步上述隐私改写与配置说明，无自相矛盾与陈旧断言。 |
| **留存证据** | `acceptance/crit08_docs_diff.txt` |
| **预计耗时** | 1 分钟 |

---

### 005：Windows 客户端接算力节点（yzwdmsnlmm）

#### Done 9：Windows 混合档（WER ≤ 纯本机，无重复，首字不慢于本机 ±0.2s）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**yzwdmsnlmm**（Windows 11，RTX 5060）。<br>2. 节点：yzwdmsnlmm 本地 gateway:8791 或 mini2 节点正常在线。<br>3. Windows 客户端环境配好，开启「使用算力节点」开关。<br>4. 测试素材：`concat5.wav`（德语素材）与 `refs.jsonl` 已拷贝至机器。 |
| **精确步骤** | 1. 运行纯本机基线（开关关闭）：<br>`python -m realtime_subtitle.app --test-playback concat5.wav --mode local > crit09_local.jsonl`<br>2. 运行混合档模式（打开算力节点开关）：<br>`python -m realtime_subtitle.app --test-playback concat5.wav --mode hybrid > crit09_hybrid.jsonl`<br>3. 运行评分脚本：<br>`python scripts/bench/hybrid_score.py crit09_hybrid.jsonl --refs refs.jsonl --b-events crit09_local.jsonl --n 5 --json > crit09_score.json`<br>4. 检查首字时间、最终 WER、重叠重复情况。 |
| **判据** | 1. 最终可见字幕 WER **≤ 纯本机**同档 WER。<br>2. **无重复**：无「本机句与节点句同时可见」的重叠重复。<br>3. 首字时间**不慢于纯本机**（允许误差在 ±0.2s 范围内）。 |
| **留存证据** | `acceptance/crit09_win_hybrid_score.json` |
| **预计耗时** | 10 分钟 |

---

#### Done 10：停节点回退与恢复（停节点字幕不断，恢复后 ≤ 60s 回到混合档）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**yzwdmsnlmm**（客户端连远端节点，如 mini2:8791）。<br>2. 持续播放测试音频流。 |
| **精确步骤** | 1. 启动 Windows 客户端进入混合档。<br>2. 在回放进行到第 20 秒时，远程/本地停止目标节点的 gateway 服务：<br>`ssh mini2 "launchctl bootout gui/\$(id -u)/com.realtimesubtitle.node"`（或在 Windows 上停止 node gateway 进程）。<br>3. 观察客户端悬浮窗状态与字幕：字幕不中断，本机继续出字，状态栏回退至「本机」。<br>4. 重新启动节点 gateway：<br>`ssh mini2 "launchctl bootstrap gui/\$(id -u) ~/Library/LaunchAgents/com.realtimesubtitle.node.plist"`。<br>5. 记录恢复开始时刻，观察客户端重新探测建联切回混合档的时刻。 |
| **判据** | 1. 停节点期间字幕**持续不断**（由本机识别承接，不卡死、不丢句）。<br>2. 节点恢复后，**≤ 60s** 内自动重新进入混合档。 |
| **留存证据** | `acceptance/crit10_win_fallback_recovery.log` |
| **预计耗时** | 8 分钟 |

---

#### Done 11：在 yzwdmsnlmm 本机打开开关时跳过自己（日志可见理由）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**yzwdmsnlmm**（本机既是节点，又有客户端）。<br>2. 确认存在本机节点身份文件 `%LOCALAPPDATA%\rs-node\node_id`。<br>3. 客户端 `%APPDATA%\realtime_subtitle\nodes.json` 包含该本机的 node_id。 |
| **精确步骤** | 1. 打开 ⚙️ 面板的「使用算力节点」开关。<br>2. 启动字幕识别，查看路由决策模块日志：<br>`Select-String -Path "subtitle.log" -Pattern "skip.*self\|SelfNodeError\|is_self_node"`<br>3. 检查是否有向本机 8791 发起自连的行为。 |
| **判据** | 路由日志明确记录跳过本机节点并给出理由（如 `is_self_node=True` 或 `SelfNodeError`），绝不连接自己。 |
| **留存证据** | `acceptance/crit11_win_skip_self.log` |
| **预计耗时** | 3 分钟 |

---

#### Done 12：默认关与隐私安全（新装/老用户开关默认关，关时无 8791 连接，非 Tailscale 地址被拒）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**yzwdmsnlmm**。 |
| **精确步骤** | 1. **默认关测试**：新建干净配置（或模拟老用户升级，无 nodes.json 或未开开关），启动程序，检查面板开关状态与配置值。<br>2. **网络抓包/端口检查**：在开关关闭状态下运行 30 秒，执行连接检查：<br>`netstat -ano \| findstr 8791`<br>确认 0 个连接。<br>3. **非法 IP 拦截测试**：在 `nodes.json` 中配置一个非 Tailscale IP（如 `ws://192.168.1.50:8791` 或公网 IP），启动程序，触发节点连接，检查日志拦截情况。 |
| **判据** | 1. 全新安装与老用户升级后节点开关**均为关**。<br>2. 开关关时**完全没有**对 8791 端口的 TCP 连接。<br>3. 非 `100.64.0.0/10` 的 Tailscale 地址被强制拒绝并打印安全告警。 |
| **留存证据** | `acceptance/crit12_win_privacy_netstat.txt` |
| **预计耗时** | 5 分钟 |

---

#### Done 13：下发（`--client` 下发后客户端无需手工配置）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 发起机：**MBA 本机**；目标机：**yzwdmsnlmm**（通过 SSH 别名连通）。<br>2. 节点服务端在 mini2 或 Windows 上运行。 |
| **精确步骤** | 1. 在 MBA 上执行节点下发命令（参数经 stdin 传 token，无明文暴露）：<br>`bash scripts/node/install_node_windows.sh <节点名> --client yzwdmsnlmm`<br>2. 检查 yzwdmsnlmm 目标机 `%APPDATA%\realtime_subtitle\`：<br>   - `nodes.json` 包含下发的节点配置条目；<br>   - `tokens\<node_id>.token` 权限经 icacls 锁定为仅当前用户可读写。<br>3. 启动 yzwdmsnlmm 客户端程序，打开设置面板检查节点状态。 |
| **判据** | 1. 脚本执行成功退出 0。<br>2. 目标机生成合规的 nodes.json 与受保护的 token 文件。<br>3. 客户端**无需任何手工配置**即可直接开启并使用算力节点。 |
| **留存证据** | `acceptance/crit13_client_push.log` |
| **预计耗时** | 5 分钟 |

---

#### Done 14：全量 pytest（含 Windows）0 failed；单机冒烟正常

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 1. 机器：**yzwdmsnlmm**。<br>2. Python venv 环境就绪（已装 requirements-dev.txt）。 |
| **精确步骤** | 1. 运行 Windows 下全量 pytest：<br>`venv\Scripts\python -m pytest`<br>2. 运行代码风格检查：<br>`venv\Scripts\python -m ruff check .`<br>3. 单机真机冒烟测试：运行 `main.py`，测试音频播放、实时识别、翻译上屏、暂停/恢复、优雅退出（卸载显存模型）。 |
| **判据** | 1. Windows pytest 全部通过（0 failed）。<br>2. ruff 检查全绿。<br>3. 单机冒烟流程（启动、识别、翻译、退出）完全正常。 |
| **留存证据** | 1. `acceptance/crit14_win_pytest.log`<br>2. `acceptance/crit14_win_smoke.log` |
| **预计耗时** | 10 分钟 |

---

#### Done 15：版本与全局文档同步（CLAUDE.md、README、用户手册、`version.py` = 3.2.0）

| 验收要素 | 内容说明 |
|---|---|
| **前置条件** | 检查全仓库文档与版本常量。 |
| **精确步骤** | 1. 检查 `version.py`：`__version__ = "3.2.0"`，且包含次版本升级理由注释。<br>2. 检查 `CLAUDE.md`：开头承诺改写、第 4 节 Windows 避坑清单补齐、第 7 节 macOS 避坑清单补齐。<br>3. 检查 `docs/zh/user-guide-template.txt` 同步。<br>4. 执行版本与根目录清单单测验证：<br>`~/projects/rs-mac-native/venv/bin/python -m pytest tests/test_desktop_shortcuts.py tests/test_repo_root_inventory.py` |
| **判据** | 1. 版本号为 3.2.0。<br>2. CLAUDE.md、README、用户手册模板完全同步。<br>3. 静态清单测试全绿。 |
| **留存证据** | `acceptance/crit15_version_doc_check.txt` |
| **预计耗时** | 3 分钟 |

---

## 3. 并行排程与时间线（Parallel Scheduling）

### 排程约束与原则
1. **rslite 单实例约束**（CLAUDE.md §7.20）：同一台 Mac 上不能同时运行两个 rslite GUI/tap 实例。
2. **严禁 A/B 同开**（CLAUDE.md §7.21）：在同一台 Mac 上测 rslite 时，绝对不能同时运行 Python A 版，防止 GPU 争抢与温控降频污染数据。
3. **1 小时额度长测单独占一台**（RFC Done 6）：**mini2 专占**该长测任务，完全不占用 MBA 和 yzwdmsnlmm 的执行时间。
4. **跨机任务并发解耦**：
   - mini2：专注于 Done 6 的 60 分钟持续回放与账本校对；
   - MBA：串行执行 Mac 侧 Done 7、1、2、3、4、5、13(发起)、8；
   - yzwdmsnlmm：串行执行 Windows 侧 Done 12、13(接收)、11、9、10、14；
   - 最终汇合：Done 15 版本与文档汇总审查。

### 三机并行时间线甘特表（总时长 ~72 分钟）

```
时间轴 (分)  0        10       20       30       40       50       60       70   72
            |--------|--------|--------|--------|--------|--------|--------|----|

[mini2]     [准备 5m ][============== Done 6 (1 小时连续额度长测) 60m ==============][收尾 3m]
                      (R_start -> 3600s 流 -> N_local == Delta_R -> S_local 5% 核对)

[MBA]       [D7 门禁 4m]
            [D1 GUI 4m]
            [D2 WER 12m]
            [D3 延迟 3m]
            [==== Done 4 回退 6 场景 15m ====]
            [D5 恢复 5m]
            [D13 发起 5m]
            [D8 文档 4m]
            [---- MBA 空闲等待 mini2 长测结束 ----]

[yzwdmsnlmm][D12 安全 6m]
            [D13 接收 6m]
            [D11 跳过 4m]
            [D9 混合 12m]
            [D10 回退恢复 10m]
            [D14 Win pytest+冒烟 12m]
            [---- Windows 验收完成，就绪 ----]

[全流程汇合]                                                               [D15 收尾 4m]
```

### 阶段详细排程

| 时间窗口 | mini2（独立长测机） | MBA 本机（Mac 验收机） | yzwdmsnlmm（Windows 验收机） |
|---|---|---|---|
| **T+00 ~ T+05** | 写入临时 key，取基线 `R_start`，拉起 1 小时音频流回放 | 跑 **Done 7**：本地 pytest、ruff、swift build、selftest | 跑 **Done 12**：验证新老配置开关默认关、netstat 查 8791 |
| **T+05 ~ T+15** | 持续运行 **Done 6**（第 1–10 分钟） | 跑 **Done 1**：临时置入 key，验证 GUI Overlay 显示「云端」<br>启动 **Done 2**：concat5 67s 回放评测 | 验证 **Done 12** 非 Tailscale 地址拦截；<br>准备 **Done 13** 接收端环境 |
| **T+15 ~ T+25** | 持续运行 **Done 6**（第 11–20 分钟） | 完成 **Done 2**：FLEURS 60 WER 计算；<br>跑 **Done 3**：从回放事件流提取段尾到上屏中位数延迟 ≤ 2s | 接收 **Done 13** 下发并验证 nodes.json / token ACL；<br>跑 **Done 11**：本机开开关验证跳过自己理由 |
| **T+25 ~ T+40** | 持续运行 **Done 6**（第 21–35 分钟） | 跑 **Done 4**：逐一注入 6 种故障（错 key、429 连发、满额账本、关开关、人工断 Wi-Fi、节点离线）并打分 | 跑 **Done 9**：Windows 混合档 concat5 回放，对比首字时间、WER 与无重复 |
| **T+40 ~ T+55** | 持续运行 **Done 6**（第 36–50 分钟） | 跑 **Done 5**：故障消除后静音点自动切回 Groq；<br>跑 **Done 13**：执行 `install_node_windows.sh --client` 下发；<br>跑 **Done 8**：README 三语隐私与配置核对 | 跑 **Done 10**：回放中停节点验证字幕不断回本机，恢复后 ≤ 60s 回到混合档；<br>开始 **Done 14**：Windows pytest |
| **T+55 ~ T+65** | 持续运行 **Done 6**（第 51–60 分钟），完成 1 小时回放 | MBA 任务已全部完成，等待 mini2 1 小时测试落地 | 完成 **Done 14**：Windows pytest 全绿，单机启动/识别/停止冒烟测试 |
| **T+65 ~ T+68** | 收集 Done 6 账本与响应头，核对 `Delta_R` 与音频秒偏差，清理 key | 收集 mini2 产物至 `acceptance/` | Windows 验收完成，就绪 |
| **T+68 ~ T+72** | — | **三端汇合，执行 Done 15**：检查 `version.py` 3.2.0、CLAUDE.md、用户手册同步，全量证据归档入 `acceptance/` | — |

---

## 4. 证据文件归档清单（`acceptance/` 目录）

| 文件路径 | 对应标准 | 内容说明 |
|---|---|---|
| `acceptance/crit01_overlay_cloud_status.png` | Done 1 | 悬浮窗状态栏显示「云端·Groq」截图或文字描述 |
| `acceptance/crit01_route_startup.log` | Done 1 | 启动选择 Tier 0 `cloud:groq` 日志片段 |
| `acceptance/crit02_concat5_score.json` | Done 2 | 67 秒回放 WER 评分结果（≤ 2.63%） |
| `acceptance/crit02_fleurs60_score.json` | Done 2 | FLEURS 60 条实时切段 WER 评分结果（≤ 4.5%） |
| `acceptance/crit03_latency.json` | Done 3 | 段尾至上屏延迟样本分布与中位数（≤ 2.0s） |
| `acceptance/crit04_fallback_badkey.log` | Done 4 | 错 key 导致 401 降级至 mini2 日志 |
| `acceptance/crit04_fallback_429.log` | Done 4 | 连发打满触发 429 冷却降级日志 |
| `acceptance/crit04_fallback_quota.log` | Done 4 | 满额账本触发额度冷却降级日志 |
| `acceptance/crit04_fallback_switchoff.log` | Done 4 | 关闭云端开关立即取消在飞请求降级日志 |
| `acceptance/crit04_fallback_offline.log` | Done 4 | 人工断开 Wi-Fi 触发退避降级日志 |
| `acceptance/crit04_fallback_node_down.log` | Done 4 | 云端与节点均不可用时降级至本机 B 日志 |
| `acceptance/crit05_recovery_switchback.log` | Done 5 | 故障恢复后在静音点切回 Groq 日志 |
| `acceptance/crit06_1hour_ledger.json` | Done 6 | 1 小时连续测试生成的本地记账文件副本 |
| `acceptance/crit06_1hour_summary.txt` | Done 6 | 1 小时请求数、响应头差值比对与音频秒偏差核对报告 |
| `acceptance/crit07_quality_gates.log` | Done 7 | pytest, ruff, swift build, selftest 全绿输出 |
| `acceptance/crit08_docs_diff.txt` | Done 8 | README 三语隐私承诺与配置说明 diff 审查记录 |
| `acceptance/crit09_win_hybrid_score.json` | Done 9 | Windows 混合档 WER、首字延迟、无重复评分报告 |
| `acceptance/crit10_win_fallback_recovery.log` | Done 10 | Windows 停节点回退与 ≤ 60s 恢复混合档日志 |
| `acceptance/crit11_win_skip_self.log` | Done 11 | yzwdmsnlmm 打开开关跳过本机节点日志 |
| `acceptance/crit12_win_privacy_netstat.txt` | Done 12 | 节点开关关闭时无 8791 连接与非 Tailscale 拦截记录 |
| `acceptance/crit13_client_push.log` | Done 13 | `--client` 下发与权限 ACL 检查记录 |
| `acceptance/crit14_win_pytest.log` | Done 14 | Windows 下 pytest 0 failed 与 ruff 结果 |
| `acceptance/crit14_win_smoke.log` | Done 14 | Windows 单机启动/识别/翻译/停止冒烟日志 |
| `acceptance/crit15_version_doc_check.txt` | Done 15 | `version.py` = 3.2.0 与全量文档同步核验结果 |
