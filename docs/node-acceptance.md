# 节点化（remote-offload）端到端验收记录

> 状态：**Sprint-001 验收收尾（2026-10-10）**。契约来源：`.governance/sprints-001-remote-offload/RFC.md` Done criteria 1–8。
> 每条证据写命令、数字、日期/commit；没跑过的写「未验」，不填推测。证据文件在 `.governance/sprints-001-remote-offload/evidence/`。
> 用户 10-09 定「快方案」：不达标项登记为后续 TK（TK-008/009/010），不卡本 Sprint 收尾。

## 总览

| 条 | 判据 | 结论 | 遗留 |
|---|---|---|---|
| 1 一键安装 | 幂等 / 0600 / nodes.json / 回滚 / `--local` / 开机自启 | **部分通过**：幂等、token、nodes.json、回滚演练真机通过 | `--local` 只有单测；mini2 重启自启未验 |
| 2 按需加载 | 空闲无 worker、gateway <50MB、120s 回收、断开 30s 判定并回收、冷启动首条精修 ≤15s | **除冷启动外通过** | 冷启动 15.7–18.1s 或窗口内未出 → TK-008 |
| 3 自动选档 | 混合档 / 停 gateway 退回 B 字幕不断 / UDS 本机节点 | **通过**（UDS 见 TK-005b 冒烟 7/7） | — |
| 4 混合档 | 首字不慢于 B / WER ≤ C+1pp / 精修中位 ≤4s / 不重复不丢句 | **未通过** | 精修中位 5.3s；拆句不替换致重复 → TK-010；C 基线无区分度 |
| 5 多节点路由 | 静音点迁移、下线回退、不断不重复、理由可见 | **探针 23/24（真机 4 次稳定）** | 唯一 FAIL 为本机句 t0 不前进 → TK-009 |
| 6 功耗五组 | 写进 docs | **已测**：A ≈ +7W，rslite 三档 ≈ 空闲 | 三档间排序需 ≥2 轮 |
| 7 质量门 | 测试覆盖 + 评审轮次 | **通过**（见 §7） | TK-005 冒烟 21/24 为既有缺口 |
| 8 文档 | CLAUDE.md §7 / protocol-v2 | 已随 TK-006 合并 | — |

## 1. 一键安装

- 命令：`bash scripts/node/install_node.sh mini2`（用户终端执行，2026-10-10；日志 `evidence/tk007_crit1_crit3_user_run.log`、`evidence/tk007_crit1_rollback_drill.log`）
- **二次执行幂等：通过**（10:18）。前后快照：node_id `f9e6…868d` 不变；目标机 token `-rw-------`、两端 token 指纹同为 `de75e5…`；`nodes.json` 仍只有 `mini2` 一条；`~/rs-node` 更新、旧版轮换为 `~/rs-node.prev`；LaunchAgent running，`/v1/info` 200（v=2），TCP 在 Tailscale IP:8791 监听。
- **人为制造自检失败能回滚：通过**（10:28）。在 mini2 `~/.zshenv` 临时注入 `RS_INFO_WAIT=x`（`seq 1 x` 无输出 → `/v1/info` 自检零轮），安装输出 `ROLLBACK：gateway x 秒内没有通过 /v1/info 自检` / `已回滚到上一版并重新启动`，退出码 1；事后 `~/rs-node` 恢复为 10:19 版、running、`/v1/info` 200、TCP 重新监听（新 PID）；`.zshenv` 已还原。回滚消耗了 `.prev`（预期行为）。
  - 第一次注入用 `RS_INFO_WAIT=0` 无效：BSD `seq 1 0` 输出 `1 0`，自检照跑两轮通过——记录在案，别再用 0 做注入。
- **未验**：`--local` 端到端（单测 `test_dry_run_local_skips_nodes_json` 覆盖 dry-run；真机会在本机常驻 LaunchAgent + 数 GB 依赖，快方案下未做）；mini2 整机重启后 LaunchAgent 自启（需重启 mini2，未获批）。

## 2. 按需加载

- 命令：`RSLITE_BIN=<主线 rslite> bash scripts/bench/node_lifecycle_probe.sh mini2 /tmp/e2e/de.wav`（远端只读 ps/日志）
- 探针修复（cbe15e5）：`disconnect_detected` 原用 gateway 会话时长减本机启动时长（起点差一段建连时间，得 -0.472s）→ 改为 kill 到本机轮询见 `session_end` 的上界；`ok:false` 时原先仍 exit 0 → 改为 exit 1。

| 检查 | 10-09 22:21 | 22:26 | 22:47（另一会话） | 10-10 10:3x | 10-10 11:1x | 结论 |
|---|---|---|---|---|---|---|
| 空闲无 worker | PASS | PASS | PASS | PASS | PASS | 通过 |
| gateway RSS <50MB | 20.0 | 22.2 | 21.6 | 29.0 | 21.5 | 通过 |
| 冷启动首条精修 ≤15s | 16.16 | null | null（首 final 18.08s） | 15.72 | null | **未通过 → TK-008** |
| 回放后回收 100–150s | 120.9 | 120.5 | 119.8 | 120.5 | 120.8 | 通过 |
| 断开判定 ≤30s | −0.47（探针 bug） | 0.59 | 0.50 | 中断 | 0.58 | 通过（修后 0.5–0.6s） |
| 断开后回收 ≤150s | 120.2 | 119.6 | null | 中断 | 120.6 | 通过（4 次中 3 次 ~120s） |

- worker 在 2.3–3.4s 即被拉起，慢在 mini2 首次模型加载/首段出结果（TK-008）。
- 10:3x 一轮因与功耗测量撞车被协调者中止（只保留前 4 项）。
- 断开后回收：22:47 那轮 150s 内未见 worker 退出（只读 ps 见其已运行 2m54s），此后 10-10 11:1x 复跑 120.6s 通过，未再复现；记为偶发，若再现并入 TK-008 排查。证据 `/tmp/tk007/crit2_run5.log`（主线 368965c 二进制）。

## 3. 自动选档

- **停 gateway 退回本机：通过**（10-10 10:20，`evidence/tk007_crit1_crit3_user_run.log` + `evidence/tk007_crit3_stderr.log`）。`rslite --mode hybrid` 回放 concat5，第 25s `launchctl bootout` mini2 gateway：状态 25.5s 由「混合·mini2」变「本机」，stderr `route fallback failed_node=mini2 … select local reason=no_available_node`；之后本机 final 2–5 与译文持续输出至音频末 65s，rslite 退出 0，188 条事件；句间空档均为 concat5 片段间静音，无丢句。之后已重新 bootstrap，节点 running。
- 小问题：混合模式下本机 Apple 翻译一次报「翻译失败：Unable to Translate」，随后被节点译文替换，用户不可见。
- MacBook Air 自动进混合档：所有 hybrid/auto 跑分的路由日志均为 `select node=mini2`（rtt 109–317ms）。
- mini2 本机走 UDS、无 token、不经网络：TK-005b UDS 冒烟 7/7（TK-005c 合并后 22:36 复跑，含「全程 0 条 TCP 连接」「无 token 字样」）。

## 4. 混合档（concat5，67s）

- 命令：`bash scripts/bench/hybrid_e2e.sh`（10-09 22:54 采集，原始事件 `scripts/bench/results/hybrid_e2e_20261009_225418/`）；评分器修复后离线重算 `hybrid_score.py --offline-dir …`（863cca0）。
- 评分器修复要点：真实事件里节点句是 `ev=final, id=null, text="[mini2] …"` 前接 `ev=replace … ids=[..]`，旧评分器靠不存在的 `source` 字段 → C/混合 WER 被算成 1.0。

| 指标 | RFC 要求 | 实测 | 结论 |
|---|---|---|---|
| 首字时间 | 不慢于 B | 混合 5.189s / B 5.177s | 字面 FAIL（差 12ms，两者首字都来自本机识别，属测量噪声；未放宽判据） |
| 最终文本 WER | ≤ C + 1pp | 0.193 / C 0.193 | 字面 PASS，**但无意义**：rslite 无纯节点模式，C 只能用 `--mode auto`，在本机 ≈ 混合 |
| 精修到达中位数 | ≤ 4s | 5.30s（6 条节点 final） | **FAIL**（节点速度，与 TK-008 同源） |
| 不重复不丢句 | 无 | 评分器报 0/0 | **实际有重复**，见下 |

- **TK-010（产品缺陷）**：第 2 句本机识别为一整句（21.12–28.56），节点拆成两句（21.2–23.7、24.08–27.66），各自覆盖本机句 34%/48% < 50% 门槛 → `replaced=0` → 本机句与两条节点句同时可见。混合档 WER 0.193 高于 B 的 0.079 主要来自这条重复；节点识别本身更准（如第 4 句与参考一致而本机有错）。评分器的重复检测没识别这种时间重叠型重复，修 TK-010 时一并补。

## 5. 多节点路由

- 命令：`python scripts/bench/multi_node_probe.py`（用仓库 venv；两个假 gateway + 真 rslite `--mode auto`）。判定在 `scripts/bench/multi_node_verify.py`，可对录下的运行离线重放。
- 评审：grok R1（F1–F7）→ R2 request-changes（F8 blocker：墙钟 t 与音频轴 t0 混用）→ agy 修 → grok R3 request-changes（agy 删了 token 泄漏检查与 hello 清零）→ codex 修 → 协调者真机复核并修一行（`global_seg` 跨会话保留，否则 A→B→A 后节点句文字重复）。合并 336cc4e。
- **真机结果：23/24，连续 4 次一致**。通过项含：路由日志整行字段齐全、连续两次领先才迁移、迁移点（A 尾句 t1 = B 首句 t0 = 57.8）落在同一扇静音窗、旧会话 drain、下线 fallback 后重新选 A、A→B→A 时序、节点句不重叠且文字唯一、全程覆盖无 >4s 空档、token 不出现在任何输出。
- 唯一 FAIL「本机句不重叠」→ **TK-009**：本机句 t0 停在段首不前进（例：id 24/25/26 的 t0 都是 79.38，t1 为 82.7/87.5/90.4），文字各异、显示正常，但区间嵌套会影响 P5 替换判断。

## 6. 功耗（五组）

详见 `evidence/tk007_crit6_power.md`（含三轮作废记录与原因）。正式一轮（10-10 11:0x，干净）：

| 组 | 均值 mW | 标准误 | 相对空闲 |
|---|---|---|---|
| 空闲 | 940 | 185 | — |
| B（rslite 本机） | 1271 | 227 | +331 |
| C（rslite auto → mini2） | 1406 | 301 | +466 |
| A（Python 桌面字幕） | 7969 | 712 | **+7029** |
| 混合（rslite hybrid → mini2） | 336 | 73 | −604 |

- 主结论：A 版比空闲多约 7W；rslite 任一模式比 A 省 6.5–7.5W。
- B/C/混合都在空闲 ±0.6W 内（1–3 倍标准误），单轮分不出高低；要排序需整套重复 ≥2 轮。A 组用 afplay 外放，含少量播放功耗。

## 7. 质量门

- 每 TK ≥2 轮评审：TK-005c = grok R1 approve（0 finding）+ 协调者复核 `close()` 提前放行只作用于 UDS/无会话路径（big-pickle R2 引用不存在的文件，整份驳回）；TK-007 探针 = grok R1/R2/R3 + 协调者真机。
- 构建：`swift build -c release` + `rslite --selftest` = SELFTEST OK（主线 368965c 前后）。
- bench 测试：`tests/test_bench_{multi_node_probe,hybrid_score,hybrid_e2e}.py` 共 15 passed。
- **既有缺口**：TK-005 冒烟 21/24（静默断网 30s 内 fallback、fallback 原因日志、恰好一次 fallback 三项），对 TK-005c 合并前基线 `e150b92` 复跑同样 21/24——不是回归，未修。
- 全量 pytest：见 Sprint 收尾记录（CURSOR）。

## 8. 文档

- `CLAUDE.md` 第 7 节第 14–23 条（节点架构、MLX 单线程、rslite 同步 main、1005/1013、悬空软链接、LaunchAgent 需登录、单实例、A/B 互扰、Tailscale GUI 模式、pgrep 互杀）；`docs/protocol-v2.md`（TK-006）。

## 不达标项与后续

| TK | 问题 | 来源 |
|---|---|---|
| TK-008 | 冷启动首条精修 >15s；精修中位 5.3s >4s | 第 2、4 条 |
| TK-009 | 本机句 t0 不随句前进，区间嵌套 | 第 5 条 |
| TK-010 | 节点拆句时 P5 不替换 → 字幕重复；评分器补时间重叠型重复检测 | 第 4 条 |
| — | TK-005 冒烟 21/24（静默断网 fallback 三项） | 第 7 条，既有 |
| — | `--local` 真机、mini2 重启自启、功耗第 2 轮、C 纯节点基线 | 未验项 |
