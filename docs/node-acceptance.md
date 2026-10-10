# 节点化（remote-offload）端到端验收记录

> 状态：持续验收中（2026-10-09）。已补真机按需加载两轮实测、Grok 多节点 R2 失败、TK-005c 合并与双审、静态评分脚本单测；其余 `TODO` 代表尚未通过或未测，不得视为验收完成。
> 契约来源：`.governance/sprints-001-remote-offload/RFC.md` 的 Done criteria 1–8。
> 每条证据都写：命令、输出摘要、数字、日期/commit。没跑过的不要填。

## 已知基线（HANDOFF §2，同一段 67s concat5 回放）

| 档位 | WER | 译文延迟 | 整机功耗（MacBook Air，CPU+GPU+ANE） |
|---|---|---|---|
| 空闲 | — | — | 1004 mW |
| B（本机系统识别 + 系统翻译） | 7.9% | 2.2–4.5 s | 1124 mW |
| C（v1 远程 Whisper，客户端侧） | 2.6% | 5.6–12.3 s（系统翻译后 7–11 s） | 822 mW |

三者功耗差异都在噪声以内。concat5 只对应 `refs.jsonl` 前 5 句（共 60 句），评分必须按前 5 句。

## 1. 一键安装

- 命令：`scripts/node/install_node.sh mini2`（第二次执行验证幂等；`--local`；人为制造自检失败验证回滚）
- 检查项：mini2 开机自启；两端 token 权限 0600；客户端 `~/.config/rslite/nodes.json` 已写入
- 证据：TODO

## 2. 按需加载

- 检查项：无会话时 mini2 上无 worker 进程、gateway 常驻内存 < 50MB；会话结束 120s 后 worker 退出；客户端被 kill/断网后 30s 内判定断开并回收 worker；冷启动后首条精修 ≤ 15s
- 命令（控制端 MacBook 执行；远端只读 `ps` 与 gateway 日志，不停止/杀远端进程）：
  ```bash
  bash scripts/bench/node_lifecycle_probe.sh mini2 ~/projects/rs-mac-native-data/concat5.wav
  ```
- 判定：
  - `idle_no_worker`：`ssh mini2 ps -axo pid,rss,command` 中没有 `realtime_subtitle.node.worker`。
  - `idle_gateway_rss`：`realtime_subtitle.node.gateway` 的最大 RSS < 50 MB。
  - `cold_first_refine`：本机 `rslite --headless --source file:<wav> --mode hybrid` 启动后，stdout JSONL 中首条节点精修结果 ≤ 15s；脚本只保留事件类型和时间戳，不记录字幕正文。
  - `idle_reap_after_playback`：回放结束后 worker 在约 120s 保温窗口后退出，脚本判定窗口为 100–150s。
  - `disconnect_detected`：第二次启动 rslite，观察到 worker 出现后 kill 本机 rslite；用 gateway `session_end dur_s` 与本机 kill 时刻相减，要求 ≤ 30s。
  - `disconnect_worker_reap`：kill 后 worker 最迟 150s 内消失（30s 断线判定 + 120s 保温回收上限）。
  - stdout 每项输出 PASS/FAIL 与数字，最后一行是汇总 JSON；实测数字填入下方证据。
- **2026-10-09 真机首跑：FAIL（尚未验收）**。`feat/tk-007`=`2ecddc9`；控制端 MacBook Air，目标 mini2；实际命令：`RSLITE_BIN=$HOME/projects/rs-mac-native/macos-native/.build/release/rslite bash scripts/bench/node_lifecycle_probe.sh mini2 /tmp/e2e/de.wav`。保留脱敏原始检查结果 `/tmp/tk007/crit2_run.log`，细节目录 `/tmp/tk007/node_lifecycle_probe.20261009-222139.77137/`。
  - PASS：空闲 worker=0；gateway RSS=20.0 MB（限 50 MB）；冷启动 worker 首次观测 3.419s；rslite 正常退出；回放后回收 120.870s；断连后 worker 消失 120.223s（限 150s）。
  - FAIL：首次节点精修 16.159s（限 15s）；`disconnect_detected` 计算出 **-0.472s**（要求 0–30s）。后者可能受不同进程时钟起点或日志关联影响，不能据此断言节点实际断线检测迟缓，需修探针验证。
  - **质量门缺陷**：首跑汇总 JSON 为 `ok:false`，但 shell 退出码仍为 0；后续工作树已补 `overall != PASS` 时退出 1，尚未提交/独立复核。此探针远端仅只读，不做 mini2 状态变更；远端故障注入仍待用户逐项批准。
- **同日复跑（另一会话启动）：仍 FAIL**。`/tmp/tk007/crit2_run2.log`：空闲 worker=0，gateway RSS=22.2MB，worker 出现 2.270s，首次精修输出为 `null`（判定 FAIL），回放后回收 120.451s，断连检测 0.586s，断连后 worker 回收 119.632s；`ok:false`、退出码 1。需核实 `first_refine` 的采样竞争：脱敏记录 `/tmp/tk007/node_lifecycle_probe.20261009-222659.80917/cold.events.meta.jsonl` 实际包含 3 条节点 final，第一条 `t=14.007s`（进程事件时间），但脚本 15s 轮询截止前未读到，导致记录 `null`。**事件时间与检测截止的关系需先修并重跑，既不能直接算 PASS，也不能误认为节点完全没有精修。**

## 3. 自动选档

- 检查项：MacBook Air（不满足 P8 门槛）连得上 mini2 时进混合档；停掉 mini2 gateway 后自动退回本机 B、字幕不断；mini2 本机运行 rslite 时走 UDS、无 token、不经网络
- 命令：TODO（`rslite --mode auto`，观察「本机 / 混合·节点 / 精修中」显示与日志中的选择理由）
- 证据：TODO

## 4. 混合档（concat5，67s 回放）

- 命令（待 TK-005 合并后实跑）：
  ```bash
  rslite --mode hybrid --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > /tmp/tk007/hybrid.events
  rslite --mode local  --source file:$HOME/projects/rs-mac-native-data/concat5.wav --headless > /tmp/tk007/b.events
  venv/bin/python scripts/bench/hybrid_score.py /tmp/tk007/hybrid.events \
      --refs $HOME/projects/rs-mac-native-data/refs.jsonl --b-events /tmp/tk007/b.events --c-wer 0.026
  ```
  `power_compare.sh` 跑完也会留下 `hybrid.events`，可直接评分。
- 注意：`hybrid_score.py` 假设节点精修行的事件带 `source` 以 `node` 开头，并有 `t0/t1`（P4 客户端时钟）；TK-005 的 headless 输出字段若不同，先对齐脚本再取数。

| 指标 | RFC 要求 | 实测 | 结论 |
|---|---|---|---|
| 首字时间 | 不慢于 B | TODO | TODO |
| 最终文本 WER | ≤ C + 1pp（C 基线 2.6% → ≤ 3.6%） | TODO | TODO |
| 精修延迟中位数 | ≤ 4 s | TODO | TODO |
| 不重复、不丢句 | 无 | TODO | TODO |

- 评分脚本已用 B 档历史数据（`rslite_fast2.jsonl`，5 句、无节点行）验证能跑通：最终 WER 0.0789（与基线 7.9% 一致）；该数据没有节点行，所以精修延迟为 n/a。
- **2026-10-09 静态/单测门：** `bash -n scripts/bench/hybrid_e2e.sh` = 0；`~/projects/rs-mac-venv/bin/python -m pytest -q tests/test_bench_hybrid_score.py -p no:cacheprovider` = **6 passed**。这只能证明脚本解析与计分单测，不代表真实 B/C/混合跑分；真实指标仍 TODO，安排在 TK-005c 合并后主线 pytest 和全局 rslite 串行资源释放之后。

## 5. 多节点路由

- 检查项：mini2 + 本机模拟的第二节点；更优节点上线后在静音点迁移、下线后退回；字幕不断不重复；选择理由在日志可见
- 命令：
  ```bash
  # 生成带静音点的德语测试音频（约 105s）
  say -v Anna "Guten Tag. [[slnc 1000]] Dies ist ein Test. [[slnc 1000]] Für das Echtzeit-Untertitel-System. [[slnc 1000]] Wir prüfen die Mehrknoten-Routing-Funktionalität. [[slnc 1000]] Der erste Knoten hat eine langsamere Erkennung. [[slnc 1000]] Der zweite Knoten ist schneller und kommt später online. [[slnc 1000]] Danach geht der zweite Knoten wieder offline. [[slnc 1000]] Und wir fallen zurück auf den ersten Knoten. [[slnc 1000]] Das System soll nahtlos im Stille-Punkt migrieren. [[slnc 1000]] Ohne Text-Wiederholungen oder Verluste. [[slnc 1000]] Wir wiederholen den Text mehrmals. [[slnc 1000]] Guten Tag. [[slnc 1000]] Dies ist ein Test. [[slnc 1000]] Für das Echtzeit-Untertitel-System. [[slnc 1000]] Wir prüfen die Mehrknoten-Routing-Funktionalität. [[slnc 1000]] Der erste Knoten hat eine langsamere Erkennung. [[slnc 1000]] Der zweite Knoten ist schneller. [[slnc 1000]] Und kommt später online. [[slnc 1000]] Danach geht der zweite Knoten wieder offline. [[slnc 1000]] Und wir fallen zurück. [[slnc 1000]] Auf den ersten Knoten. [[slnc 1000]] Das System soll nahtlos migrieren. [[slnc 1000]] Ohne Wiederholungen. [[slnc 1000]] Oder Verluste. [[slnc 1000]] Noch eine Runde. [[slnc 1000]] Guten Tag. [[slnc 1000]] Dies ist ein Test. [[slnc 1000]] Für das System. [[slnc 1000]] Wir prüfen das Routing. [[slnc 1000]] Erster Knoten langsam. [[slnc 1000]] Zweiter Knoten schnell. [[slnc 1000]] Zweiter geht offline. [[slnc 1000]] Zurück zum ersten. [[slnc 1000]] Migriert im Stille-Punkt. [[slnc 1000]] Keine Wiederholungen. [[slnc 1000]] Keine Verluste." -o /tmp/german_test.aiff
  afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/german_test.aiff /tmp/german_test.wav
  
  # 运行验收探针（自动起两个假网关、模拟节点上下线、跑 rslite --mode auto）
  uv run python scripts/bench/multi_node_probe.py
  ```
- 判定（脚本自动输出 PASS/FAIL 与汇总 JSON）：
  - `rslite exit code 0`：rslite 正常跑完整个回放
  - `Token A not in output` / `Token B not in output`：token 未泄露到 stdout/stderr/gateway 日志
  - `Routing select logs fully match regex with all fields`：整行正则匹配 `rslite.route select node=<id> score=<n> quality=<n> speed=<n> penalties=<n> bonus=<n> rtt_ms=<n>`
  - `Migration preceded by at least two consecutive select node=node-b`：连续两次打分领先才触发迁移
  - `stdout contains status '混合·node-b'` 与静音窗判定：状态事件 t 落在 WAV 中 ≥0.6s 静音窗
  - `Old session (Node A) received drain and emitted drained`：旧会话在静音点完成 drain 并回 drained
  - `Node B first final t0 is not earlier than silence point start`：新会话首句不早于该静音点
  - `Fallback failed_node=node-b is followed by select node=node-a`：节点下线后出现 fallback 并在其后重新 select node-a
  - `Node A final after fallback has t0 later than last Node B final`：回退后有 t0 晚于最后一条 B 句的 A final
  - `Node B finals count corresponds to active window`：B 的条数与在线窗口相符
  - `Node sentences follow time order Phase A -> Phase B -> Phase A`：节点句严格遵循 A 段→B 段→A 段时序
  - `Visible subtitle track not empty and no replaced local sentences remain`：可见字幕轨非空，读 ev=replace 被替换本机句不得保留在可见轨
  - `Visible subtitle track has strictly unique text` / `Visible local IDs strictly increasing` / `Node sentences do not overlap in t0/t1`：字幕严格无重复
  - `Timeline coverage has no gap exceeding silence threshold + switch budget`：整段音频无超预算间隙
  - `No switch_failed in stderr`：无切换失败
  - 末尾汇总 JSON 含 `checks_passed`/`checks_failed`/`node_a_finals`/`node_b_finals`/`visible_lines` 等字段
- **2026-10-09 Grok R2：request-changes，未通过验收。**评审结果 `/tmp/tk007/grok_multinode_r2.md`（末行 `GROKDONE`）；虽然旧实现自报 20/20，F1/F2/F3/F5 未修好、F4/F6/F7 已修好。新增 F8 blocker（`t` 进程墙钟与 `t0` 音频轴混用，且迁移窗、首句窗是两个独立搜索）及 F9–F13 major（在线窗口自证、片头片尾丢句漏测、A→B→A 到达序混淆、可见轨不遵守 `LineStore`、drain/selection 因果次序无证据），F14/F15 minor。已创建隔离 lane `feat/tk-007-r2fix` 修验收探针，**修复与第三方复审前不能认定多节点迁移已通过**。

## 6. 功耗（五组）

- 命令（用户 sudo 执行；测前停掉 A 版和浏览器视频，脚本会检查并拒绝在 A 版/rslite 已运行时开始）：
  ```bash
  sudo bash scripts/bench/power_compare.sh ~/projects/rs-mac-native-data/concat5.wav
  ```
- 每组约 76 秒（含 8s 收尾），组间冷却 30s；A 版另加 30s 模型预热。建议整套至少重复 2 轮估计噪声。

| 组 | 平均功耗 (mW) 第 1 轮 | 第 2 轮 | 备注 |
|---|---|---|---|
| 空闲 | TODO | TODO | 基线 1004 |
| B（rslite 本机） | TODO | TODO | 基线 1124 |
| C（远程精修，v2 节点） | TODO | TODO | 基线 822（v1）；脚本里用 `--mode auto`，若客户端上与混合无法区分则改为节点侧采样 |
| A（Python 桌面字幕） | TODO | TODO | 用 afplay 放音频，A 版抓系统声音；afplay 自身有少量播放功耗，其它组不出声 |
| 混合（rslite --mode hybrid） | TODO | TODO | |

- 噪声说明：TODO（两轮之差；已知空闲与 B 仅差 120mW，在噪声以内——差距小于重复测量波动时不下结论）

## 7. 质量门

- 命令：`QT_QPA_PLATFORM=offscreen venv/bin/python -m pytest -q -p no:cacheprovider`；`venv/bin/ruff check .`；`cd macos-native && swift build -c release --product rslite`
- 检查项：P2 各项上限、S3、S7 有测试覆盖；每个 TK 至少 2 轮评审（鉴权/生命周期/状态机类 3 轮）
- **2026-10-09 TK-005c 已合入 `feat/macos-native`：merge `938240b`（仅特性分支，未碰 master、未 push）。** Coordinator release build 成功（`/tmp/tk005c/coordinator_build.log`），`rslite --selftest` = `SELFTEST OK`，UDS 冒烟 **7/7 PASS**（`/tmp/tk005c/coordinator_uds_smoke.log`）；Grok R1 和 Codex R2 均审 `approve` / 0 finding（`/tmp/tk005c/{grok_review_r1,codex_review_r2}.md`）。TK-005 24 项冒烟是 **21/24**，静默断网缺 30s 内 fallback、fallback 原因日志和恰好一次 fallback 证明；**同一脚本对未修改基线 `e150b92` 复跑同样 21/24、同样三项 FAIL**（`/tmp/tk005c/coordinator_tk005_baseline.log`），属于已知共同验收缺口而非已证明的 TK-005c 回归，保留后续诊断/修复；不宣称 24/24 PASS。主线合并后全量 pytest 和 ruff 复跑进行中（结果另行补录）。

## 8. 文档

- 检查项：`CLAUDE.md` 第 7 节新增避坑条目；README 说明用法；`docs/protocol-v2.md` 写清与 v1 的差异（TK-006）
- 证据：TODO

## 不达标项与后续

TODO（不达标的要么回对应 TK 修，要么在此写明原因与后续计划）
